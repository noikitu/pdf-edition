"""Sauvegarde sur disque des documents en cours, pour survivre à un redémarrage.

Jusqu'ici tout vivait en mémoire : arrêter le serveur, ou le voir planter,
perdait les corrections non téléchargées. On écrit donc l'état courant de chaque
document à côté d'un petit fichier de description, et on le relit à la demande.

Ce que l'on ne conserve pas : la pile d'annulation. Garder vingt-cinq versions
de chaque document sur disque coûterait cher pour un service rendu marginal —
après reprise, le document est là mais son historique repart de zéro, et c'est
dit à l'utilisateur.

L'écriture est atomique (fichier temporaire puis renommage) : une coupure de
courant au mauvais moment ne peut pas laisser un PDF tronqué à la place d'un
PDF valide.
"""

from __future__ import annotations

import json
import os
import time
from pathlib import Path

from .config import AUTOSAVE, DATA_DIR, SESSION_TTL

DIR = Path(DATA_DIR).expanduser() / "sessions"


def enabled() -> bool:
    return AUTOSAVE


def _paths(doc_id: str) -> tuple[Path, Path]:
    return DIR / f"{doc_id}.pdf", DIR / f"{doc_id}.json"


def _safe(doc_id: str) -> bool:
    """Un identifiant est un hexadécimal produit par uuid4 ; tout le reste est
    refusé, pour qu'aucun chemin ne puisse sortir du dossier."""
    return bool(doc_id) and all(c in "0123456789abcdef" for c in doc_id)


def save(doc_id: str, name: str, data: bytes, version: int) -> None:
    if not AUTOSAVE or not _safe(doc_id):
        return
    try:
        DIR.mkdir(parents=True, exist_ok=True)
        pdf, meta = _paths(doc_id)
        tmp = pdf.with_suffix(".part")
        tmp.write_bytes(data)
        os.replace(tmp, pdf)
        meta.write_text(
            json.dumps({"name": name, "version": version, "saved_at": time.time()}),
            encoding="utf-8",
        )
    except OSError:
        pass      # disque plein ou dossier non inscriptible : l'app continue


def load(doc_id: str) -> tuple[str, bytes] | None:
    """Renvoie (nom d'origine, contenu) d'un document sauvegardé, ou None."""
    if not AUTOSAVE or not _safe(doc_id):
        return None
    pdf, meta = _paths(doc_id)
    try:
        data = pdf.read_bytes()
    except OSError:
        return None
    name = "document.pdf"
    try:
        name = json.loads(meta.read_text(encoding="utf-8")).get("name") or name
    except (OSError, ValueError):
        pass
    return name, data


def index() -> list[dict]:
    """Documents disponibles pour reprise, du plus récent au plus ancien."""
    if not AUTOSAVE:
        return []
    entries: list[dict] = []
    try:
        files = list(DIR.glob("*.pdf"))
    except OSError:
        return []
    for pdf in files:
        doc_id = pdf.stem
        if not _safe(doc_id):
            continue
        try:
            size = pdf.stat().st_size
            saved_at = pdf.stat().st_mtime
        except OSError:
            continue
        name = "document.pdf"
        try:
            meta = json.loads((DIR / f"{doc_id}.json").read_text(encoding="utf-8"))
            name = meta.get("name") or name
            saved_at = meta.get("saved_at", saved_at)
        except (OSError, ValueError):
            pass
        entries.append({"doc_id": doc_id, "name": name, "size": size, "saved_at": saved_at})
    entries.sort(key=lambda e: e["saved_at"], reverse=True)
    return entries


def drop(doc_id: str) -> None:
    """Efface la sauvegarde : l'utilisateur a fermé le document volontairement."""
    if not _safe(doc_id):
        return
    for path in _paths(doc_id):
        try:
            path.unlink()
        except OSError:
            pass


def purge() -> None:
    """Supprime les sauvegardes plus vieilles que la durée de vie des sessions."""
    if not AUTOSAVE:
        return
    cutoff = time.time() - SESSION_TTL
    for entry in index():
        if entry["saved_at"] < cutoff:
            drop(entry["doc_id"])
