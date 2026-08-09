"""API web de l'éditeur PDF."""

from __future__ import annotations

import io
import re
from pathlib import Path
from typing import Optional

from fastapi import FastAPI, File, HTTPException, UploadFile
from fastapi.responses import FileResponse, Response, StreamingResponse
from fastapi.staticfiles import StaticFiles
from pydantic import BaseModel, Field

from . import pdf_ops
from .store import Session, store

MAX_UPLOAD_MB = 40

BASE_DIR = Path(__file__).resolve().parent
STATIC_DIR = BASE_DIR / "static"

app = FastAPI(title="Éditeur PDF", docs_url="/api/docs", openapi_url="/api/openapi.json")


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

class EditItem(BaseModel):
    id: str
    text: str
    # Texte affiché au moment de la modification : sert à vérifier que l'on
    # réécrit bien le fragment que l'utilisateur avait sous les yeux.
    original: Optional[str] = None


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


# --------------------------------------------------------------------------
# Routes
# --------------------------------------------------------------------------

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
    return Response(png, media_type="image/png", headers={"Cache-Control": "no-store"})


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
    changed = pdf_ops.apply_edits(session.doc, resolved)
    if changed:
        session.version += 1
    else:
        session.undo_stack.pop()
    return {"changed": changed, "skipped": len(unresolved), **_state(session)}


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


@app.post("/api/{doc_id}/replace")
def replace(doc_id: str, payload: ReplacePayload) -> dict:
    session = _session(doc_id)
    if not payload.search:
        raise HTTPException(400, "Terme de recherche vide.")
    session.snapshot()
    changed = pdf_ops.replace_all(
        session.doc, payload.search, payload.replace, payload.case_sensitive
    )
    if changed:
        session.version += 1
    else:
        session.undo_stack.pop()
    return {"changed": changed, **_state(session)}


@app.get("/api/{doc_id}/search")
def search(doc_id: str, q: str, case_sensitive: bool = False) -> dict:
    session = _session(doc_id)
    return {"count": pdf_ops.count_matches(session.doc, q, case_sensitive)}


@app.post("/api/{doc_id}/undo")
def undo(doc_id: str) -> dict:
    session = _session(doc_id)
    return {"ok": session.undo(), **_state(session)}


@app.post("/api/{doc_id}/redo")
def redo(doc_id: str) -> dict:
    session = _session(doc_id)
    return {"ok": session.redo(), **_state(session)}


@app.get("/api/{doc_id}/download")
def download(doc_id: str) -> StreamingResponse:
    session = _session(doc_id)
    data = session.doc.tobytes(garbage=3, deflate=True)
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
