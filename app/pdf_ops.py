"""Lecture et réécriture du texte d'un PDF avec PyMuPDF.

Principe : on extrait les « fragments » de texte (spans regroupés par style au
sein d'une ligne). Pour modifier un fragment, on efface la zone d'origine par
caviardage (redaction) puis on réécrit le nouveau texte sur la même ligne de
base, à la même taille, et avec la police d'origine du document lorsque c'est
possible (voir `fonts.py`).
"""

from __future__ import annotations

from dataclasses import dataclass, asdict
from typing import Iterable

import fitz  # PyMuPDF

from . import fonts
from .fonts import FontResolver, base14_font

# Bits du champ `flags` d'un span PyMuPDF.
FLAG_ITALIC = 1 << 1
FLAG_SERIF = 1 << 2
FLAG_MONO = 1 << 3
FLAG_BOLD = 1 << 4

# Noms de polices à empattement les plus courants.
SERIF_NAMES = (
    "times", "serif", "roman", "georgia", "garamond", "minion", "book", "cambria",
    "palatino", "baskerville", "caslon", "didot", "charter", "utopia", "century",
    "constantia", "crimson", "merriweather", "spectral", "lora",
)

# Familles Base-14 : (normal, gras, italique, gras-italique)
_SANS, _SERIF, _MONO = fonts.SANS, fonts.SERIF, fonts.MONO


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
    """Déduit le style d'un span. Le bit « serif » du descripteur PDF est
    inexploitable — beaucoup de générateurs le positionnent systématiquement, y
    compris pour des polices sans empattement — donc on se fie au seul nom, avec
    sans-serif par défaut, qui est le cas le plus fréquent."""
    low = (fontname or "").lower()
    bold = bool(flags & FLAG_BOLD) or any(
        k in low for k in ("bold", "black", "heavy", "semib", "demi")
    )
    italic = bool(flags & FLAG_ITALIC) or any(k in low for k in ("italic", "oblique"))
    mono = bool(flags & FLAG_MONO) or any(k in low for k in ("mono", "courier", "consol"))
    serif = any(k in low for k in SERIF_NAMES)
    return bold, italic, mono, serif


def base14_alias(fontname: str, flags: int) -> str:
    """Choisit la police Base-14 la plus proche de la police d'origine."""
    bold, italic, mono, serif = _style_flags(fontname, flags)
    family = _MONO if mono else (_SERIF if serif else _SANS)
    return family[(1 if bold else 0) + (2 if italic else 0)]


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


def export_pdf(doc: fitz.Document) -> bytes:
    """Sérialise le document pour téléchargement.

    Écrire du texte embarque la police entière dans le PDF, ce qui peut ajouter
    plusieurs mégaoctets. On réduit donc les polices aux seuls glyphes utilisés,
    sur une copie : le document de travail garde ses polices complètes, dont les
    modifications suivantes ont besoin.
    """
    data = doc.tobytes(garbage=3, deflate=True)
    export = fitz.open("pdf", data)
    try:
        export.subset_fonts()
        data = export.tobytes(garbage=4, deflate=True, use_objstms=True)
    except Exception:
        pass        # sous-ensemblage impossible : on exporte tel quel
    finally:
        export.close()
    return data


# Au-delà de cette dimension, une image est redimensionnée avant réencodage :
# suffisant pour remplir un écran, inutile pour un PDF destiné à l'impression bureau.
COMPRESS_MAX_DIM = 1600
COMPRESS_JPEG_QUALITY = 60


def compress_pdf(doc: fitz.Document) -> tuple[bytes, int, int]:
    """Réduit le poids du PDF en réencodant ses images, sans toucher au texte.

    Renvoie (données du PDF compressé, taille avant, taille après).

    Les images sans transparence sont réencodées en JPEG à qualité 60 et
    redimensionnées si elles dépassent COMPRESS_MAX_DIM ; le résultat n'est
    conservé que s'il est réellement plus léger. Les images avec transparence
    sont laissées intactes : les reconvertir en PNG après extraction du canal
    alpha produit souvent un fichier plus gros que l'original, pas plus petit.

    `fitz.Page.replace_image` attache par défaut un profil ICC complet à
    chaque image réencodée (quelques Ko chacun, là où l'original n'utilisait
    souvent qu'un `/DeviceRGB` sans profil) : sur un document à dizaines
    d'images, cela peut faire gagner l'image et perdre le fichier. On écrit
    donc le flux directement via `update_stream`, avec un dictionnaire minimal.
    """
    before = len(doc.tobytes(garbage=3, deflate=True))

    seen: set[int] = set()
    for pno in range(doc.page_count):
        page = doc[pno]
        for image in page.get_images(full=True):
            xref = image[0]
            if xref in seen:
                continue
            seen.add(xref)
            _compress_image(doc, xref)

    doc.subset_fonts()
    data = doc.tobytes(garbage=4, deflate=True, use_objstms=True)
    return data, before, len(data)


def _compress_image(doc: fitz.Document, xref: int) -> None:
    try:
        pix = fitz.Pixmap(doc, xref)
    except Exception:
        return   # image illisible (CMYK exotique, flux corrompu…) : on la laisse
    if pix.alpha:
        return   # transparence : la reconversion grossirait plutôt qu'elle n'allège

    grayscale = pix.n == 1
    if not grayscale and pix.colorspace and pix.colorspace.name != "DeviceRGB":
        pix = fitz.Pixmap(fitz.csRGB, pix)

    if max(pix.width, pix.height) > COMPRESS_MAX_DIM:
        scale = COMPRESS_MAX_DIM / max(pix.width, pix.height)
        pix = fitz.Pixmap(pix, max(1, round(pix.width * scale)), max(1, round(pix.height * scale)), None)

    try:
        before_bytes = len(doc.extract_image(xref)["image"])
        data = pix.tobytes("jpeg", jpg_quality=COMPRESS_JPEG_QUALITY)
    except Exception:
        return
    if len(data) >= before_bytes:
        return   # déjà mieux compressée que ce qu'on proposerait : on n'y touche pas

    doc.update_stream(xref, data, new=0, compress=0)
    doc.xref_set_key(xref, "Filter", "/DCTDecode")
    doc.xref_set_key(xref, "ColorSpace", "/DeviceGray" if grayscale else "/DeviceRGB")
    doc.xref_set_key(xref, "Width", str(pix.width))
    doc.xref_set_key(xref, "Height", str(pix.height))
    doc.xref_set_key(xref, "BitsPerComponent", "8")
    doc.xref_set_key(xref, "DecodeParms", "null")


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


def _write(
    page: fitz.Page,
    item: TextItem,
    text: str,
    resolver: FontResolver,
    style: Style | None = None,
) -> bool:
    """Écrit `text` à l'emplacement de `item`, en réduisant la taille si besoin.

    `style` impose une police choisie dans l'interface ; sans lui, on reprend
    automatiquement celle du document. Renvoie True si la typographie obtenue
    est bien celle attendue (police d'origine, ou police demandée sans substitut).
    """
    text = text.replace("\n", " ").rstrip()
    if not text:
        return True
    if style is not None and style.family:
        font, exact = fonts.choice_font(style.family, style.bold, style.italic)
    else:
        font, exact = resolver.resolve(page.number, item.fontname, item.font, text)
    size = item.size
    available = max(item.max_x - item.bbox[0], 1.0)
    width = font.text_length(text, fontsize=size)
    if width > available:
        size = max(item.size * MIN_SHRINK, size * available / width)
    _write_line(page, fitz.Point(item.bbox[0], item.baseline), text, font, size, item.color)
    return exact


def _write_line(
    page: fitz.Page, point: fitz.Point, text: str, font: fitz.Font, size: float, color: str
) -> None:
    """Pose une ligne de texte sur sa ligne de base, avec une police quelconque."""
    writer = fitz.TextWriter(page.rect, color=_hex_to_rgb(color))
    writer.append(point, text, font=font, fontsize=size)
    writer.write_text(page, overlay=True)


def resolve_edits(
    doc: fitz.Document, requests: list[dict]
) -> tuple[dict[str, EditSpec], list[dict]]:
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
            spec = EditSpec(req["text"], _style_from(req.get("style")))
            target = index.get(req["id"])
            if target is not None and (original is None or target.text == original):
                resolved[target.id] = spec
                continue
            twins = [i for i in items if i.text == original] if original else []
            if len(twins) == 1:
                resolved[twins[0].id] = spec
            else:
                unresolved.append(req)
    return resolved, unresolved


def _style_from(raw: dict | None) -> Style | None:
    if not raw or not raw.get("family"):
        return None
    return Style(str(raw["family"]), bool(raw.get("bold")), bool(raw.get("italic")))


@dataclass
class Style:
    """Police imposée depuis l'interface pour un fragment."""

    family: str | None = None   # clé de fonts.CHOICES, ou None pour « automatique »
    bold: bool = False
    italic: bool = False


@dataclass
class EditSpec:
    """Ce qu'il faut écrire à la place d'un fragment."""

    text: str
    style: Style | None = None


@dataclass
class EditResult:
    """Bilan d'une série de modifications."""

    changed: int = 0
    approximated: int = 0   # fragments dont la typographie a dû être approchée

    def __bool__(self) -> bool:
        return bool(self.changed)


def apply_edits(doc: fitz.Document, edits: dict[str, EditSpec | str]) -> EditResult:
    """Applique {item_id: texte ou EditSpec}. Un texte vide supprime le fragment."""
    specs = {
        key: value if isinstance(value, EditSpec) else EditSpec(value)
        for key, value in edits.items()
    }
    by_page: dict[int, list[tuple[TextItem, EditSpec]]] = {}
    for pno in {int(k.split("-", 1)[0]) for k in specs}:
        index = {it.id: it for it in extract_items(doc, pno)}
        for item_id, spec in specs.items():
            item = index.get(item_id)
            if item is None:
                continue
            # Un simple changement de police justifie de réécrire un texte identique.
            if spec.text != item.text or spec.style is not None:
                by_page.setdefault(pno, []).append((item, spec))

    resolver = FontResolver(doc)
    result = EditResult()
    for pno, pairs in by_page.items():
        page = doc[pno]
        _erase(page, (fitz.Rect(item.bbox) for item, _ in pairs))
        for item, spec in pairs:
            if not _write(page, item, spec.text, resolver, spec.style):
                result.approximated += 1
            result.changed += 1
    return result


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
    font = base14_font(alias, text)
    for i, line in enumerate(text.split("\n")):
        if line:
            _write_line(page, fitz.Point(x, y + size * (i + 1)), line, font, size, color)


def replace_all(
    doc: fitz.Document, search: str, replace: str, case_sensitive: bool
) -> EditResult:
    """Remplace toutes les occurrences de `search` dans tout le document."""
    if not search:
        return EditResult()
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


def find_occurrences(
    doc: fitz.Document, search: str, case_sensitive: bool
) -> list[dict]:
    """Localise chaque occurrence de `search`, pour pouvoir sauter de l'une à l'autre.

    `Page.search_for` ignore la casse et ne sait pas la respecter ; quand
    l'utilisateur la demande, on relit donc le texte sous chaque rectangle
    trouvé pour ne garder que les correspondances exactes.
    """
    if not search:
        return []
    hits: list[dict] = []
    for pno in range(doc.page_count):
        page = doc[pno]
        try:
            rects = page.search_for(search)
        except Exception:
            continue
        for rect in rects:
            if case_sensitive and search not in page.get_textbox(rect):
                continue
            hits.append({
                "page": pno,
                "bbox": [round(v, 2) for v in (rect.x0, rect.y0, rect.x1, rect.y1)],
            })
    return hits


# --------------------------------------------------------------------------
# Surlignage
# --------------------------------------------------------------------------

HIGHLIGHT_COLOR = (1, 0.92, 0.35)   # jaune citron, cohérent avec l'interface


def highlight_item(doc: fitz.Document, item: TextItem) -> None:
    """Pose une vraie annotation de surlignage sur un fragment.

    On passe par une annotation plutôt qu'un rectangle dessiné : elle reste
    sélectionnable et supprimable dans n'importe quel lecteur PDF, et elle
    n'altère pas le contenu de la page.
    """
    page = doc[item.page]
    annot = page.add_highlight_annot(fitz.Rect(item.bbox))
    annot.set_colors(stroke=HIGHLIGHT_COLOR)
    annot.update()


def clear_highlights(doc: fitz.Document, pno: int) -> int:
    """Retire tous les surlignages d'une page. Renvoie le nombre supprimé."""
    page = doc[pno]
    removed = 0
    for annot in list(page.annots(types=[fitz.PDF_ANNOT_HIGHLIGHT])):
        page.delete_annot(annot)
        removed += 1
    return removed


# --------------------------------------------------------------------------
# Gestion des pages
# --------------------------------------------------------------------------

def rotate_page(doc: fitz.Document, pno: int, delta: int) -> None:
    """Pivote une page d'un multiple de 90°, en cumulant avec sa rotation actuelle."""
    page = doc[pno]
    page.set_rotation((page.rotation + delta) % 360)


def delete_page(doc: fitz.Document, pno: int) -> None:
    doc.delete_page(pno)


def move_page(doc: fitz.Document, pno: int, offset: int) -> int:
    """Déplace une page d'un cran vers le haut (-1) ou vers le bas (+1).

    Renvoie sa nouvelle position. `move_page` insère *avant* la position
    donnée : descendre d'un cran revient donc à viser `pno + 2`, la place
    libérée par le retrait de la page décalant tout ce qui suit.
    """
    target = pno + offset
    if not 0 <= target < doc.page_count:
        return pno
    doc.move_page(pno, target if offset < 0 else target + 1)
    return target


def extract_pages(doc: fitz.Document, numbers: list[int]) -> bytes:
    """Construit un nouveau PDF ne contenant que les pages demandées, dans l'ordre."""
    out = fitz.open()
    for pno in numbers:
        if 0 <= pno < doc.page_count:
            out.insert_pdf(doc, from_page=pno, to_page=pno)
    data = out.tobytes(garbage=4, deflate=True, use_objstms=True)
    out.close()
    return data


def parse_page_spec(spec: str, page_count: int) -> list[int]:
    """Interprète « 1-3, 5, 8- » en indices de pages (l'utilisateur compte à partir de 1)."""
    numbers: list[int] = []
    for chunk in spec.replace(" ", "").split(","):
        if not chunk:
            continue
        if "-" in chunk:
            start, _, end = chunk.partition("-")
            first = int(start) if start.isdigit() else 1
            last = int(end) if end.isdigit() else page_count
        elif chunk.isdigit():
            first = last = int(chunk)
        else:
            continue
        for human in range(max(first, 1), min(last, page_count) + 1):
            index = human - 1
            if index not in numbers:
                numbers.append(index)
    return numbers


def merge_pdf(doc: fitz.Document, data: bytes) -> int:
    """Ajoute à la fin du document les pages d'un autre PDF. Renvoie leur nombre."""
    other = fitz.open(stream=data, filetype="pdf")
    try:
        if other.needs_pass:
            raise ValueError("Ce PDF est protégé par mot de passe.")
        added = other.page_count
        doc.insert_pdf(other)
        return added
    finally:
        other.close()
