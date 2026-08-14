"""API web de LemonPDF."""

from __future__ import annotations

import io
import re
from pathlib import Path
from typing import Optional

from fastapi import FastAPI, File, Form, HTTPException, Request, UploadFile
from fastapi.responses import FileResponse, Response, StreamingResponse
from fastapi.staticfiles import StaticFiles
from pydantic import BaseModel, Field

from . import fonts, keystore, llm, pdf_ops, persist
from .config import MAX_UPLOAD_MB
from .store import Session, store

BASE_DIR = Path(__file__).resolve().parent
STATIC_DIR = BASE_DIR / "static"

app = FastAPI(title="LemonPDF", docs_url="/api/docs", openapi_url="/api/openapi.json")


LOCAL_HOSTS = {"127.0.0.1", "::1", "localhost"}


def _local_only(request: Request) -> None:
    """Interdit l'accès aux clés enregistrées depuis une autre machine.

    Une clé conservée par le serveur appartient à la personne assise devant lui.
    Si l'instance est exposée à d'autres, la laisser servir à un visiteur
    reviendrait à lui prêter la clé — et à lui en faire payer l'usage.
    """
    host = request.client.host if request.client else ""
    if host not in LOCAL_HOSTS:
        raise HTTPException(
            403,
            "Les clés enregistrées ne sont accessibles que depuis la machine qui "
            "héberge l'application. Saisissez votre clé pour cette analyse.",
        )


def _session(doc_id: str) -> Session:
    session = store.get(doc_id)
    if session is None:
        raise HTTPException(404, "Document introuvable ou session expirée.")
    return session


def _state(session: Session) -> dict:
    # Toutes les routes qui modifient le document terminent ici : c'est donc le
    # point de passage naturel pour écrire la sauvegarde, sans avoir à l'appeler
    # depuis chacune d'elles. Sans changement de version, l'appel ne fait rien.
    session.autosave()
    return {
        "version": session.version,
        "can_undo": session.can_undo,
        "can_redo": session.can_redo,
        "pages": pdf_ops.page_info(session.doc),
    }


# --------------------------------------------------------------------------
# Modèles de requête
# --------------------------------------------------------------------------

class StylePayload(BaseModel):
    family: Optional[str] = None   # clé de fonts.CHOICES, None = police du document
    bold: bool = False
    italic: bool = False


class EditItem(BaseModel):
    id: str
    text: str
    # Texte affiché au moment de la modification : sert à vérifier que l'on
    # réécrit bien le fragment que l'utilisateur avait sous les yeux.
    original: Optional[str] = None
    style: Optional[StylePayload] = None


class EditPayload(BaseModel):
    edits: list[EditItem] = Field(default_factory=list)


class TextBoxPayload(BaseModel):
    page: int
    x: float
    y: float
    text: str
    size: float = 12.0
    color: str = "#000000"
    bold: bool = False
    italic: bool = False


class ReplacePayload(BaseModel):
    search: str
    replace: str = ""
    case_sensitive: bool = False


class HighlightPayload(BaseModel):
    id: str
    original: Optional[str] = None


class RotatePayload(BaseModel):
    delta: int = 90


class MovePayload(BaseModel):
    offset: int = -1              # -1 monte la page, +1 la descend
    to: Optional[int] = None      # position absolue (glisser-déposer des vignettes)


class FieldsPayload(BaseModel):
    values: dict[str, str] = Field(default_factory=dict)


class AssistPayload(BaseModel):
    instruction: str
    provider: str
    # Clé saisie pour cette analyse seulement. Vide, on se rabat sur celle
    # enregistrée sur la machine. Elle n'est ni journalisée ni renvoyée.
    api_key: str = ""
    remember: bool = False       # enregistrer la clé saisie pour les fois suivantes
    model: str = ""
    page: Optional[int] = None   # None = tout le document


class KeyPayload(BaseModel):
    provider: str
    api_key: str


class RedactPayload(BaseModel):
    x0: float
    y0: float
    x1: float
    y1: float
    blackout: bool = False   # True : rectangle noir par-dessus la zone vidée


# --------------------------------------------------------------------------
# Routes
# --------------------------------------------------------------------------

@app.get("/healthz")
def healthz() -> dict:
    """Sonde utilisée par Docker et les hébergeurs pour savoir si l'app répond."""
    return {"status": "ok"}


@app.get("/api/fonts")
def font_catalogue() -> dict:
    """Polices proposées dans l'interface, avec leur disponibilité sur la machine."""
    return {"fonts": fonts.catalogue()}


@app.post("/api/upload")
async def upload(file: UploadFile = File(...)) -> dict:
    data = await file.read()
    if not data:
        raise HTTPException(400, "Fichier vide.")
    if len(data) > MAX_UPLOAD_MB * 1024 * 1024:
        raise HTTPException(413, f"Fichier trop volumineux (max {MAX_UPLOAD_MB} Mo).")
    if not data.lstrip()[:5].startswith(b"%PDF"):
        raise HTTPException(400, "Ce fichier n'est pas un PDF.")
    try:
        doc_id, session = store.create(file.filename or "document.pdf", data)
    except Exception as exc:  # PDF corrompu ou chiffré
        raise HTTPException(400, f"PDF illisible : {exc}") from exc
    if session.doc.needs_pass:
        store.drop(doc_id)
        raise HTTPException(400, "PDF protégé par mot de passe : déverrouillez-le d'abord.")
    return {"doc_id": doc_id, "name": session.name, **_state(session)}


@app.get("/api/sessions")
def sessions() -> dict:
    """Documents sauvegardés, proposés à la reprise sur l'écran d'accueil."""
    return {"sessions": persist.index(), "autosave": persist.enabled()}


@app.get("/api/{doc_id}/state")
def state(doc_id: str) -> dict:
    session = _session(doc_id)
    return {"doc_id": doc_id, "name": session.name, **_state(session)}


@app.get("/api/{doc_id}/scan")
def scan_report(doc_id: str) -> dict:
    """Pages dépourvues de texte extractible (scans).

    Volontairement hors de l'état renvoyé par les autres routes : le calcul relit
    tout le document, ce serait gâché à chaque modification. Le client l'appelle
    à l'ouverture et après les opérations qui changent la pagination.
    """
    session = _session(doc_id)
    pages = pdf_ops.pages_without_text(session.doc)
    return {
        "scan_pages": pages,
        "page_count": session.doc.page_count,
        "fully_scanned": len(pages) == session.doc.page_count,
    }


@app.get("/api/{doc_id}/fields")
def fields(doc_id: str) -> dict:
    session = _session(doc_id)
    return {"fields": pdf_ops.list_fields(session.doc)}


@app.post("/api/{doc_id}/fields")
def fill(doc_id: str, payload: FieldsPayload) -> dict:
    session = _session(doc_id)
    if not payload.values:
        raise HTTPException(400, "Aucune valeur à écrire.")
    session.snapshot()
    filled = pdf_ops.fill_fields(session.doc, payload.values)
    if filled:
        session.version += 1
    else:
        session.undo_stack.pop()
    return {"filled": filled, **_state(session)}


@app.get("/api/{doc_id}/page/{pno}.png")
def page_image(doc_id: str, pno: int, scale: float = 2.0, v: int = 0) -> Response:
    session = _session(doc_id)
    if not 0 <= pno < session.doc.page_count:
        raise HTTPException(404, "Page inexistante.")
    # Le plancher descend à 0,1 pour les vignettes du menu latéral : à 0,5 le
    # serveur rendrait des images cinq fois trop grandes pour la place qu'elles
    # occupent, sur toutes les pages du document.
    scale = min(max(scale, 0.1), 4.0)
    png = pdf_ops.render_page(session.doc, pno, scale)
    # L'URL porte le numéro de version du document : le contenu d'une adresse
    # donnée ne change donc jamais, et on peut laisser le navigateur la garder.
    # Sans cela, la loupe — qui repeint l'image à chaque mouvement de souris —
    # relancerait un rendu serveur en continu.
    return Response(
        png, media_type="image/png", headers={"Cache-Control": "private, max-age=600"}
    )


@app.get("/api/{doc_id}/page/{pno}/items")
def page_items(doc_id: str, pno: int) -> dict:
    session = _session(doc_id)
    if not 0 <= pno < session.doc.page_count:
        raise HTTPException(404, "Page inexistante.")
    items = pdf_ops.extract_items(session.doc, pno)
    return {"version": session.version, "items": [i.to_dict() for i in items]}


@app.post("/api/{doc_id}/edit")
def edit(doc_id: str, payload: EditPayload) -> dict:
    session = _session(doc_id)
    if not payload.edits:
        return {"changed": 0, "skipped": 0, **_state(session)}

    resolved, unresolved = pdf_ops.resolve_edits(
        session.doc, [e.model_dump() for e in payload.edits]
    )
    if unresolved and not resolved:
        raise HTTPException(
            409, "Le document a changé entre-temps : la page a été rechargée, réessayez."
        )

    session.snapshot()
    result = pdf_ops.apply_edits(session.doc, resolved)
    if result:
        session.version += 1
    else:
        session.undo_stack.pop()
    return {
        "changed": result.changed,
        "approximated": result.approximated,
        "reflowed": result.reflowed,
        "skipped": len(unresolved),
        **_state(session),
    }


@app.post("/api/{doc_id}/textbox")
def textbox(doc_id: str, payload: TextBoxPayload) -> dict:
    session = _session(doc_id)
    if not 0 <= payload.page < session.doc.page_count:
        raise HTTPException(404, "Page inexistante.")
    if not payload.text.strip():
        raise HTTPException(400, "Texte vide.")
    session.snapshot()
    pdf_ops.add_textbox(
        session.doc,
        payload.page,
        payload.x,
        payload.y,
        payload.text,
        size=payload.size,
        color=payload.color,
        bold=payload.bold,
        italic=payload.italic,
    )
    session.version += 1
    return {"changed": 1, **_state(session)}


@app.get("/api/llm/providers")
def llm_providers() -> dict:
    """Fournisseurs proposés, avec ceux dont le paquet est réellement installé."""
    return {"providers": llm.catalogue()}


@app.get("/api/llm/keys")
def key_status(request: Request) -> dict:
    """Quels fournisseurs disposent d'une clé — jamais sa valeur."""
    _local_only(request)
    return keystore.status()


@app.post("/api/llm/keys")
def key_save(request: Request, payload: KeyPayload) -> dict:
    _local_only(request)
    llm.get_provider(payload.provider)          # refuse un fournisseur inconnu
    if not payload.api_key.strip():
        raise HTTPException(400, "Clé vide.")
    if keystore.from_env(payload.provider):
        raise HTTPException(
            400, "Une clé est déjà fournie par l'environnement pour ce fournisseur."
        )
    where = keystore.save(payload.provider, payload.api_key.strip())
    return {"saved": True, "where": where, **keystore.status()}


@app.delete("/api/llm/keys/{provider}")
def key_forget(request: Request, provider: str) -> dict:
    _local_only(request)
    keystore.forget(provider)
    return {"forgotten": True, **keystore.status()}


@app.post("/api/{doc_id}/assist")
def assist(doc_id: str, request: Request, payload: AssistPayload) -> dict:
    """Demande des corrections au LLM. N'applique rien : l'utilisateur valide.

    Le PDF n'est pas transmis au fournisseur, seulement le texte des fragments.
    """
    session = _session(doc_id)
    if not payload.instruction.strip():
        raise HTTPException(400, "Indiquez ce que le modèle doit corriger.")

    api_key = payload.api_key.strip()
    is_local = (request.client.host if request.client else "") in LOCAL_HOSTS
    if api_key and payload.remember and is_local:
        keystore.save(payload.provider, api_key)
    if not api_key and is_local:
        api_key = keystore.get(payload.provider)
    if not api_key:
        raise HTTPException(400, "Clé API manquante.")

    if payload.page is None:
        pages = list(range(session.doc.page_count))
    elif 0 <= payload.page < session.doc.page_count:
        pages = [payload.page]
    else:
        raise HTTPException(404, "Page inexistante.")

    try:
        result = llm.suggest(
            session.doc,
            pages,
            payload.instruction.strip(),
            payload.provider,
            api_key,
            payload.model.strip(),
        )
    except ValueError as exc:            # fournisseur inconnu
        raise HTTPException(400, str(exc)) from exc
    except RuntimeError as exc:          # paquet manquant
        raise HTTPException(503, str(exc)) from exc
    except Exception as exc:             # clé refusée, quota, modèle inconnu, réseau…
        # On renvoie le message du fournisseur : « invalid api key », « model not
        # found »… c'est presque toujours l'information utile.
        raise HTTPException(502, f"Le fournisseur a refusé la demande : {exc}") from exc
    return result


@app.post("/api/{doc_id}/image")
async def add_image(
    doc_id: str,
    page: int = Form(...),
    x: float = Form(...),
    y: float = Form(...),
    width: float = Form(...),
    height: float = Form(...),
    file: UploadFile = File(...),
) -> dict:
    """Insère une image ou une signature à l'endroit indiqué (coordonnées PDF)."""
    session = _session(doc_id)
    if not 0 <= page < session.doc.page_count:
        raise HTTPException(404, "Page inexistante.")
    data = await file.read()
    if not data:
        raise HTTPException(400, "Fichier vide.")
    if len(data) > MAX_UPLOAD_MB * 1024 * 1024:
        raise HTTPException(413, f"Image trop volumineuse (max {MAX_UPLOAD_MB} Mo).")

    session.snapshot()
    try:
        rect = pdf_ops.insert_image(session.doc, page, x, y, width, height, data)
    except Exception as exc:
        session.undo_stack.pop()
        raise HTTPException(400, f"Image inutilisable : {exc}") from exc
    session.version += 1
    return {"rect": [round(v, 2) for v in rect], **_state(session)}


@app.post("/api/{doc_id}/page/{pno}/redact")
def redact(doc_id: str, pno: int, payload: RedactPayload) -> dict:
    """Efface (ou noircit) définitivement une zone de la page."""
    session = _session(doc_id)
    if not 0 <= pno < session.doc.page_count:
        raise HTTPException(404, "Page inexistante.")
    session.snapshot()
    done = pdf_ops.redact_area(
        session.doc, pno, payload.x0, payload.y0, payload.x1, payload.y1, payload.blackout
    )
    if done:
        session.version += 1
    else:
        session.undo_stack.pop()
        raise HTTPException(400, "Zone trop petite : tracez un rectangle plus grand.")
    return {"redacted": 1, **_state(session)}


@app.post("/api/{doc_id}/replace")
def replace(doc_id: str, payload: ReplacePayload) -> dict:
    session = _session(doc_id)
    if not payload.search:
        raise HTTPException(400, "Terme de recherche vide.")
    session.snapshot()
    result = pdf_ops.replace_all(
        session.doc, payload.search, payload.replace, payload.case_sensitive
    )
    if result:
        session.version += 1
    else:
        session.undo_stack.pop()
    return {
        "changed": result.changed,
        "approximated": result.approximated,
        "reflowed": result.reflowed,
        **_state(session),
    }


@app.get("/api/{doc_id}/search")
def search(doc_id: str, q: str, case_sensitive: bool = False) -> dict:
    session = _session(doc_id)
    hits = pdf_ops.find_occurrences(session.doc, q, case_sensitive)
    return {
        "count": pdf_ops.count_matches(session.doc, q, case_sensitive),
        "hits": hits,
    }


@app.post("/api/{doc_id}/highlight")
def highlight(doc_id: str, payload: HighlightPayload) -> dict:
    session = _session(doc_id)
    resolved, unresolved = pdf_ops.resolve_edits(
        session.doc,
        # `resolve_edits` sert ici uniquement à retrouver le fragment visé, y
        # compris si les index ont bougé ; le texte reste celui d'origine.
        [{"id": payload.id, "text": payload.original or "", "original": payload.original}],
    )
    if not resolved:
        raise HTTPException(409, "Texte introuvable : la page a été rechargée, réessayez.")

    item_id = next(iter(resolved))
    pno = int(item_id.split("-", 1)[0])
    item = next((i for i in pdf_ops.extract_items(session.doc, pno) if i.id == item_id), None)
    if item is None:
        raise HTTPException(409, "Texte introuvable : la page a été rechargée, réessayez.")

    session.snapshot()
    pdf_ops.highlight_item(session.doc, item)
    session.version += 1
    return {"highlighted": 1, **_state(session)}


@app.delete("/api/{doc_id}/page/{pno}/highlights")
def remove_highlights(doc_id: str, pno: int) -> dict:
    session = _session(doc_id)
    if not 0 <= pno < session.doc.page_count:
        raise HTTPException(404, "Page inexistante.")
    session.snapshot()
    removed = pdf_ops.clear_highlights(session.doc, pno)
    if removed:
        session.version += 1
    else:
        session.undo_stack.pop()
    return {"removed": removed, **_state(session)}


# --------------------------------------------------------------------------
# Pages : pivoter, supprimer, réordonner, extraire, fusionner
# --------------------------------------------------------------------------

@app.post("/api/{doc_id}/page/{pno}/rotate")
def rotate(doc_id: str, pno: int, payload: RotatePayload) -> dict:
    session = _session(doc_id)
    if not 0 <= pno < session.doc.page_count:
        raise HTTPException(404, "Page inexistante.")
    session.snapshot()
    pdf_ops.rotate_page(session.doc, pno, payload.delta)
    session.version += 1
    return {"page": pno, **_state(session)}


@app.delete("/api/{doc_id}/page/{pno}")
def remove_page(doc_id: str, pno: int) -> dict:
    session = _session(doc_id)
    if not 0 <= pno < session.doc.page_count:
        raise HTTPException(404, "Page inexistante.")
    if session.doc.page_count == 1:
        raise HTTPException(400, "Impossible de supprimer la dernière page du document.")
    session.snapshot()
    pdf_ops.delete_page(session.doc, pno)
    session.version += 1
    return {"deleted": pno, **_state(session)}


@app.post("/api/{doc_id}/page/{pno}/move")
def reorder_page(doc_id: str, pno: int, payload: MovePayload) -> dict:
    session = _session(doc_id)
    if not 0 <= pno < session.doc.page_count:
        raise HTTPException(404, "Page inexistante.")
    session.snapshot()
    if payload.to is None:
        position = pdf_ops.move_page(session.doc, pno, payload.offset)
    else:
        position = pdf_ops.move_page_to(session.doc, pno, payload.to)
    if position != pno:
        session.version += 1
    else:
        session.undo_stack.pop()
    return {"page": position, **_state(session)}


@app.get("/api/{doc_id}/extract")
def extract(doc_id: str, pages: str) -> StreamingResponse:
    session = _session(doc_id)
    numbers = pdf_ops.parse_page_spec(pages, session.doc.page_count)
    if not numbers:
        raise HTTPException(400, "Aucune page valide dans la sélection.")
    data = pdf_ops.extract_pages(session.doc, numbers)
    stem = re.sub(r"[^A-Za-z0-9._-]+", "_", Path(session.name).stem) or "document"
    return StreamingResponse(
        io.BytesIO(data),
        media_type="application/pdf",
        headers={"Content-Disposition": f'attachment; filename="{stem}-extrait.pdf"'},
    )


@app.post("/api/{doc_id}/merge")
async def merge(doc_id: str, file: UploadFile = File(...)) -> dict:
    session = _session(doc_id)
    data = await file.read()
    if not data:
        raise HTTPException(400, "Fichier vide.")
    if len(data) > MAX_UPLOAD_MB * 1024 * 1024:
        raise HTTPException(413, f"Fichier trop volumineux (max {MAX_UPLOAD_MB} Mo).")
    if not data.lstrip()[:5].startswith(b"%PDF"):
        raise HTTPException(400, "Ce fichier n'est pas un PDF.")
    session.snapshot()
    try:
        added = pdf_ops.merge_pdf(session.doc, data)
    except Exception as exc:
        session.undo_stack.pop()
        raise HTTPException(400, f"Fusion impossible : {exc}") from exc
    session.version += 1
    return {"added": added, **_state(session)}


@app.post("/api/{doc_id}/undo")
def undo(doc_id: str) -> dict:
    session = _session(doc_id)
    return {"ok": session.undo(), **_state(session)}


@app.post("/api/{doc_id}/redo")
def redo(doc_id: str) -> dict:
    session = _session(doc_id)
    return {"ok": session.redo(), **_state(session)}


@app.post("/api/{doc_id}/compress")
def compress(doc_id: str) -> dict:
    session = _session(doc_id)
    session.snapshot()
    data, before, after = pdf_ops.compress_pdf(session.doc)
    if after < before:
        session.replace_doc(data)
    else:
        session.undo_stack.pop()
    return {"before": before, "after": after, **_state(session)}


@app.get("/api/{doc_id}/download")
def download(doc_id: str) -> StreamingResponse:
    session = _session(doc_id)
    data = pdf_ops.export_pdf(session.doc)
    stem = re.sub(r"[^A-Za-z0-9._-]+", "_", Path(session.name).stem) or "document"
    filename = f"{stem}-modifie.pdf"
    return StreamingResponse(
        io.BytesIO(data),
        media_type="application/pdf",
        headers={"Content-Disposition": f'attachment; filename="{filename}"'},
    )


@app.delete("/api/{doc_id}")
def close_doc(doc_id: str) -> dict:
    store.drop(doc_id)
    return {"ok": True}


@app.get("/")
def index() -> FileResponse:
    return FileResponse(STATIC_DIR / "index.html")


app.mount("/", StaticFiles(directory=STATIC_DIR), name="static")
