"""API web de LemonPDF."""

from __future__ import annotations

import io
import re
from pathlib import Path
from typing import Optional

from fastapi import FastAPI, File, Form, HTTPException, UploadFile
from fastapi.responses import FileResponse, Response, StreamingResponse
from fastapi.staticfiles import StaticFiles
from pydantic import BaseModel, Field

from . import fonts, pdf_ops
from .config import MAX_UPLOAD_MB
from .store import Session, store

BASE_DIR = Path(__file__).resolve().parent
STATIC_DIR = BASE_DIR / "static"

app = FastAPI(title="LemonPDF", docs_url="/api/docs", openapi_url="/api/openapi.json")


def _session(doc_id: str) -> Session:
    session = store.get(doc_id)
    if session is None:
        raise HTTPException(404, "Document introuvable ou session expirée.")
    return session


def _state(session: Session) -> dict:
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
    offset: int = -1   # -1 monte la page, +1 la descend


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


@app.get("/api/{doc_id}/state")
def state(doc_id: str) -> dict:
    session = _session(doc_id)
    return {"doc_id": doc_id, "name": session.name, **_state(session)}


@app.get("/api/{doc_id}/page/{pno}.png")
def page_image(doc_id: str, pno: int, scale: float = 2.0, v: int = 0) -> Response:
    session = _session(doc_id)
    if not 0 <= pno < session.doc.page_count:
        raise HTTPException(404, "Page inexistante.")
    scale = min(max(scale, 0.5), 4.0)
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
    position = pdf_ops.move_page(session.doc, pno, payload.offset)
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
