"""Lecture et réécriture du texte d'un PDF avec PyMuPDF.

Principe : on extrait les « fragments » de texte (spans regroupés par style au
sein d'une ligne). Pour modifier un fragment, on efface la zone d'origine par
caviardage (redaction) puis on réécrit le nouveau texte sur la même ligne de
base, avec une police et une taille aussi proches que possible de l'original.
"""

from __future__ import annotations

import os
from dataclasses import dataclass, asdict
from typing import Iterable

import fitz  # PyMuPDF

# Bits du champ `flags` d'un span PyMuPDF.
FLAG_ITALIC = 1 << 1
FLAG_SERIF = 1 << 2
FLAG_MONO = 1 << 3
FLAG_BOLD = 1 << 4

# Familles Base-14 : (normal, gras, italique, gras-italique)
_SANS = ("helv", "hebo", "heit", "hebi")
_SERIF = ("tiro", "tibo", "tiit", "tibi")
_MONO = ("cour", "cobo", "coit", "cobi")

# Polices système utilisées en dernier recours pour les alphabets non latin-1.
_FALLBACK_FONT_FILES = (
    "/System/Library/Fonts/Supplemental/Arial Unicode.ttf",
    "/Library/Fonts/Arial Unicode.ttf",
    "/usr/share/fonts/truetype/dejavu/DejaVuSans.ttf",
    "/usr/share/fonts/TTF/DejaVuSans.ttf",
    "C:/Windows/Fonts/arial.ttf",
)

# Marge horizontale minimale conservée à droite d'un texte étendu.
PAGE_MARGIN = 20.0
# Réduction maximale de la police quand le nouveau texte est trop long.
MIN_SHRINK = 0.75


@dataclass
class TextItem:
    """Un fragment de texte éditable de la page."""

    id: str
    page: int
    text: str
    bbox: tuple[float, float, float, float]
    baseline: float
    size: float
    font: str
    fontname: str
    color: str
    bold: bool
    italic: bool
    max_x: float

    def to_dict(self) -> dict:
        return asdict(self)


# --------------------------------------------------------------------------
# Polices
# --------------------------------------------------------------------------

def _style_flags(fontname: str, flags: int) -> tuple[bool, bool, bool, bool]:
    low = (fontname or "").lower()
    bold = bool(flags & FLAG_BOLD) or any(
        k in low for k in ("bold", "black", "heavy", "semib", "demi")
    )
    italic = bool(flags & FLAG_ITALIC) or any(k in low for k in ("italic", "oblique"))
    mono = bool(flags & FLAG_MONO) or any(k in low for k in ("mono", "courier", "consol"))
    serif = bool(flags & FLAG_SERIF) or any(
        k in low for k in ("times", "serif", "roman", "georgia", "garamond", "minion", "book")
    )
    return bold, italic, mono, serif


def base14_alias(fontname: str, flags: int) -> str:
    """Choisit la police Base-14 la plus proche de la police d'origine."""
    bold, italic, mono, serif = _style_flags(fontname, flags)
    family = _MONO if mono else (_SERIF if serif else _SANS)
    return family[(1 if bold else 0) + (2 if italic else 0)]


def _has_cjk(text: str) -> bool:
    return any(
        "\u3000" <= ch <= "\u9fff" or "\uac00" <= ch <= "\ud7af" or "\uff00" <= ch <= "\uffef"
        for ch in text
    )


_fallback_cache: str | None | bool = False


def _fallback_font_file() -> str | None:
    global _fallback_cache
    if _fallback_cache is False:
        _fallback_cache = next((p for p in _FALLBACK_FONT_FILES if os.path.exists(p)), None)
    return _fallback_cache  # type: ignore[return-value]


def resolve_font(alias: str, text: str) -> tuple[str, str | None]:
    """Renvoie (fontname, fontfile) capable d'écrire `text`.

    Les polices Base-14 ne couvrent que le latin-1 (suffisant pour le français).
    Au-delà, on bascule sur une police CJK intégrée ou une police système.
    """
    try:
        text.encode("latin-1")
        return alias, None
    except UnicodeEncodeError:
        pass
    if _has_cjk(text):
        return "china-s", None
    path = _fallback_font_file()
    if path:
        return "F-uni", path
    return alias, None  # les glyphes manquants seront remplacés par des puces


def _text_length(text: str, fontname: str, fontfile: str | None, fontsize: float) -> float:
    if fontfile:
        return fitz.Font(fontfile=fontfile).text_length(text, fontsize=fontsize)
    return fitz.get_text_length(text, fontname=fontname, fontsize=fontsize)


# --------------------------------------------------------------------------
# Extraction
# --------------------------------------------------------------------------

def _color_hex(value: int) -> str:
    return "#%06x" % (int(value) & 0xFFFFFF)


def _hex_to_rgb(value: str) -> tuple[float, float, float]:
    value = (value or "#000000").lstrip("#")
    if len(value) == 3:
        value = "".join(c * 2 for c in value)
    try:
        n = int(value, 16)
    except ValueError:
        n = 0
    return ((n >> 16 & 255) / 255, (n >> 8 & 255) / 255, (n & 255) / 255)


def extract_items(doc: fitz.Document, pno: int) -> list[TextItem]:
    """Extrait les fragments éditables d'une page.

    Les spans consécutifs d'une même ligne partageant police/taille/couleur sont
    fusionnés : on édite ainsi des morceaux de phrase plutôt que des bouts de mots.
    """
    page = doc[pno]
    flags = fitz.TEXTFLAGS_DICT & ~fitz.TEXT_PRESERVE_LIGATURES
    raw = page.get_text("dict", flags=flags)
    page_right = page.rect.x1 - PAGE_MARGIN

    items: list[TextItem] = []
    for bi, block in enumerate(raw.get("blocks", [])):
        if block.get("type") != 0:
            continue
        lines = block.get("lines", [])
        # Un paragraphe multi-lignes donne la vraie largeur de colonne ; pour une
        # ligne isolée on autorise l'écriture jusqu'à la marge de la page.
        block_right = page_right if len(lines) < 2 else min(block["bbox"][2], page_right)
        for li, line in enumerate(lines):
            # On ignore le texte pivoté : le réécrire horizontalement le casserait.
            if abs(line.get("dir", (1, 0))[1]) > 0.01:
                continue
            groups: list[dict] = []
            for span in line.get("spans", []):
                if not span["text"].strip():
                    continue
                style = (span["font"], round(span["size"], 2), span["color"])
                if groups and groups[-1]["style"] == style:
                    prev = groups[-1]
                    prev["text"] += span["text"]
                    prev["bbox"] = tuple(fitz.Rect(prev["bbox"]) | fitz.Rect(span["bbox"]))
                else:
                    groups.append(
                        {
                            "style": style,
                            "text": span["text"],
                            "bbox": tuple(span["bbox"]),
                            "baseline": span["origin"][1],
                            "flags": span["flags"],
                        }
                    )
            for gi, g in enumerate(groups):
                bold, italic, _, _ = _style_flags(g["style"][0], g["flags"])
                # Place disponible : jusqu'au fragment suivant, sinon marge droite.
                nxt = groups[gi + 1]["bbox"][0] if gi + 1 < len(groups) else None
                max_x = nxt if nxt is not None else max(block_right, g["bbox"][2])
                items.append(
                    TextItem(
                        id=f"{pno}-{bi}-{li}-{gi}",
                        page=pno,
                        text=g["text"],
                        bbox=g["bbox"],
                        baseline=g["baseline"],
                        size=g["style"][1],
                        font=base14_alias(g["style"][0], g["flags"]),
                        fontname=g["style"][0],
                        color=_color_hex(g["style"][2]),
                        bold=bold,
                        italic=italic,
                        max_x=max_x,
                    )
                )
    return items


def page_info(doc: fitz.Document) -> list[dict]:
    return [
        {"number": i, "width": round(p.rect.width, 2), "height": round(p.rect.height, 2)}
        for i, p in enumerate(doc)
    ]


def render_page(doc: fitz.Document, pno: int, scale: float = 2.0) -> bytes:
    pix = doc[pno].get_pixmap(matrix=fitz.Matrix(scale, scale), alpha=False)
    return pix.tobytes("png")


# --------------------------------------------------------------------------
# Écriture
# --------------------------------------------------------------------------

def _erase(page: fitz.Page, rects: Iterable[fitz.Rect]) -> None:
    """Supprime le texte des zones données sans toucher images ni traits."""
    used = False
    for rect in rects:
        # Léger retrait horizontal pour ne pas happer les caractères voisins.
        r = fitz.Rect(rect.x0 + 0.3, rect.y0 - 0.5, rect.x1 - 0.3, rect.y1 + 0.5)
        if r.is_empty:
            continue
        page.add_redact_annot(r, fill=None)
        used = True
    if used:
        page.apply_redactions(
            images=fitz.PDF_REDACT_IMAGE_NONE,
            graphics=fitz.PDF_REDACT_LINE_ART_NONE,
            text=fitz.PDF_REDACT_TEXT_REMOVE,
        )


def _write(page: fitz.Page, item: TextItem, text: str) -> None:
    """Écrit `text` à l'emplacement de `item`, en réduisant la taille si besoin."""
    text = text.replace("\n", " ").rstrip()
    if not text:
        return
    fontname, fontfile = resolve_font(item.font, text)
    size = item.size
    available = max(item.max_x - item.bbox[0], 1.0)
    width = _text_length(text, fontname, fontfile, size)
    if width > available:
        size = max(item.size * MIN_SHRINK, size * available / width)
    page.insert_text(
        fitz.Point(item.bbox[0], item.baseline),
        text,
        fontname=fontname,
        fontfile=fontfile,
        fontsize=size,
        color=_hex_to_rgb(item.color),
        overlay=True,
    )


def resolve_edits(
    doc: fitz.Document, requests: list[dict]
) -> tuple[dict[str, str], list[dict]]:
    """Fait correspondre des demandes du client aux fragments actuels du document.

    Les identifiants sont positionnels : ils changent dès qu'une page est
    modifiée. On vérifie donc systématiquement que le fragment visé contient
    toujours le texte que l'utilisateur avait sous les yeux, faute de quoi on le
    retrouve par son contenu — ou on renonce, plutôt que d'écrire au mauvais
    endroit. Renvoie ({id: nouveau_texte}, demandes non résolues).
    """
    resolved: dict[str, str] = {}
    unresolved: list[dict] = []

    by_page: dict[int, list[dict]] = {}
    for req in requests:
        try:
            pno = int(str(req.get("id", "")).split("-", 1)[0])
        except ValueError:
            unresolved.append(req)
            continue
        by_page.setdefault(pno, []).append(req)

    for pno, reqs in by_page.items():
        if not 0 <= pno < doc.page_count:
            unresolved.extend(reqs)
            continue
        items = extract_items(doc, pno)
        index = {item.id: item for item in items}
        for req in reqs:
            original = req.get("original")
            target = index.get(req["id"])
            if target is not None and (original is None or target.text == original):
                resolved[target.id] = req["text"]
                continue
            twins = [i for i in items if i.text == original] if original else []
            if len(twins) == 1:
                resolved[twins[0].id] = req["text"]
            else:
                unresolved.append(req)
    return resolved, unresolved


def apply_edits(doc: fitz.Document, edits: dict[str, str]) -> int:
    """Applique {item_id: nouveau_texte}. Un texte vide supprime le fragment."""
    by_page: dict[int, list[tuple[TextItem, str]]] = {}
    for pno in {int(k.split("-", 1)[0]) for k in edits}:
        index = {it.id: it for it in extract_items(doc, pno)}
        for item_id, new_text in edits.items():
            item = index.get(item_id)
            if item is not None and new_text != item.text:
                by_page.setdefault(pno, []).append((item, new_text))

    changed = 0
    for pno, pairs in by_page.items():
        page = doc[pno]
        _erase(page, (fitz.Rect(item.bbox) for item, _ in pairs))
        for item, new_text in pairs:
            _write(page, item, new_text)
            changed += 1
    return changed


def add_textbox(
    doc: fitz.Document,
    pno: int,
    x: float,
    y: float,
    text: str,
    size: float = 12.0,
    color: str = "#000000",
    bold: bool = False,
    italic: bool = False,
) -> None:
    """Ajoute un nouveau texte libre ; (x, y) est le coin haut-gauche."""
    page = doc[pno]
    alias = _SANS[(1 if bold else 0) + (2 if italic else 0)]
    fontname, fontfile = resolve_font(alias, text)
    rgb = _hex_to_rgb(color)
    for i, line in enumerate(text.split("\n")):
        if not line:
            continue
        page.insert_text(
            fitz.Point(x, y + size * (i + 1)),
            line,
            fontname=fontname,
            fontfile=fontfile,
            fontsize=size,
            color=rgb,
            overlay=True,
        )


def replace_all(doc: fitz.Document, search: str, replace: str, case_sensitive: bool) -> int:
    """Remplace toutes les occurrences de `search` dans tout le document."""
    if not search:
        return 0
    needle = search if case_sensitive else search.lower()
    edits: dict[str, str] = {}
    for pno in range(doc.page_count):
        for item in extract_items(doc, pno):
            haystack = item.text if case_sensitive else item.text.lower()
            if needle not in haystack:
                continue
            if case_sensitive:
                edits[item.id] = item.text.replace(search, replace)
            else:
                edits[item.id] = _ireplace(item.text, search, replace)
    return apply_edits(doc, edits)


def _match_case(source: str, replace: str) -> str:
    """Reproduit la casse du texte remplacé (« Ortografe » -> « Orthographe »)."""
    if not replace or not source:
        return replace
    if source.isupper() and len(source) > 1:
        return replace.upper()
    if source[0].isupper() and source[1:].islower():
        return replace[0].upper() + replace[1:]
    return replace


def _ireplace(text: str, search: str, replace: str) -> str:
    """Remplacement insensible à la casse, en conservant la casse d'origine."""
    low, needle = text.lower(), search.lower()
    out, start = [], 0
    while True:
        idx = low.find(needle, start)
        if idx < 0:
            out.append(text[start:])
            return "".join(out)
        out.append(text[start:idx])
        out.append(_match_case(text[idx:idx + len(needle)], replace))
        start = idx + len(needle)


def count_matches(doc: fitz.Document, search: str, case_sensitive: bool) -> int:
    if not search:
        return 0
    needle = search if case_sensitive else search.lower()
    total = 0
    for pno in range(doc.page_count):
        for item in extract_items(doc, pno):
            hay = item.text if case_sensitive else item.text.lower()
            total += hay.count(needle)
    return total
