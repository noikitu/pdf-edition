"""Lecture et réécriture du texte d'un PDF avec PyMuPDF.

Principe : on extrait les « fragments » de texte (spans regroupés par style au
sein d'une ligne). Pour modifier un fragment, on efface la zone d'origine par
caviardage (redaction) puis on réécrit le nouveau texte sur la même ligne de
base, à la même taille, et avec la police d'origine du document lorsque c'est
possible (voir `fonts.py`).
"""

from __future__ import annotations

import re
from dataclasses import dataclass, asdict, replace
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


def pages_without_text(doc: fitz.Document) -> list[int]:
    """Pages ne contenant aucun texte extractible — typiquement des scans.

    Sur ces pages l'application n'a rien à proposer : le texte n'existe pas dans
    le PDF, ce sont des pixels. Autant le dire clairement plutôt que de laisser
    l'utilisateur cliquer sans effet.
    """
    return [pno for pno, page in enumerate(doc) if not page.get_text("text").strip()]


def _pdf_date(value: str) -> str:
    """Convertit une date PDF (« D:20240115103000+01'00' ») en ISO 8601.

    Le format est celui d'ISO 32000 : préfixe optionnel, champs facultatifs à
    partir du mois, et décalage horaire noté avec des apostrophes. Une date
    illisible est renvoyée telle quelle plutôt que masquée — mieux vaut afficher
    une chaîne étrange que prétendre qu'il n'y a pas de date.
    """
    if not value:
        return ""
    m = re.match(
        r"D?:?(\d{4})(\d{2})?(\d{2})?(\d{2})?(\d{2})?(\d{2})?"
        # Le temps universel s'écrit d'un simple « Z », sans chiffres derrière :
        # il lui faut sa propre alternative, sans quoi tout le groupe échoue et
        # le fuseau disparaît silencieusement.
        r"(?:([Zz])|([+-])(\d{2})'?(\d{2})?)?",
        value.strip(),
    )
    if not m:
        return value
    year, month, day, hour, minute, second, zulu, sign, tzh, tzm = m.groups()
    stamp = f"{year}-{month or '01'}-{day or '01'}T{hour or '00'}:{minute or '00'}:{second or '00'}"
    if zulu:
        stamp += "+00:00"
    elif sign:
        stamp += f"{sign}{tzh or '00'}:{tzm or '00'}"
    return stamp


def document_info(doc: fitz.Document) -> dict:
    """Fiche d'identité du document : métadonnées, dimensions, contenus annexes."""
    meta = doc.metadata or {}
    first = doc[0].rect if doc.page_count else fitz.Rect(0, 0, 0, 0)
    annots = sum(1 for pno in range(doc.page_count) for _ in doc[pno].annots())
    return {
        "title": meta.get("title") or "",
        "author": meta.get("author") or "",
        "subject": meta.get("subject") or "",
        "keywords": meta.get("keywords") or "",
        "creator": meta.get("creator") or "",       # logiciel d'origine
        "producer": meta.get("producer") or "",     # logiciel ayant écrit le PDF
        "format": meta.get("format") or "",
        "created": _pdf_date(meta.get("creationDate") or ""),
        "modified": _pdf_date(meta.get("modDate") or ""),
        "encrypted": bool(meta.get("encryption")),
        "pages": doc.page_count,
        "width_pt": round(first.width, 1),
        "height_pt": round(first.height, 1),
        # Les dimensions en millimètres parlent davantage que les points.
        "width_mm": round(first.width * 25.4 / 72, 1),
        "height_mm": round(first.height * 25.4 / 72, 1),
        "annotations": annots,
        "fields": len(list_fields(doc)),
    }


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


def _probe(item: TextItem):
    """Témoin de contrôle d'une police : ce que le document a réellement rendu."""
    return (item.text, item.size, item.bbox[2] - item.bbox[0])


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
        # La largeur réellement occupée par le texte d'origine sert de témoin :
        # une police qui ne la reproduit pas n'écrit pas ce qu'on croit.
        font, exact = resolver.resolve(
            page.number, item.fontname, item.font, text, _probe(item)
        )
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


# --------------------------------------------------------------------------
# Reflux des paragraphes
# --------------------------------------------------------------------------
#
# Quand le texte saisi ne tient plus sur sa ligne, la réponse historique était de
# réduire la police. On préfère désormais recomposer le paragraphe entier :
# recalculer ses retours à la ligne, et le laisser gagner une ligne s'il y a de
# la place en dessous.
#
# Le reflux est refusé — et l'on retombe alors sur la réduction — dès que
# recomposer ferait perdre de l'information :
#   * plusieurs styles dans le paragraphe (un mot en gras serait aplati) ;
#   * une césure en fin de ligne (on ne sait pas si « bien- » et « être »
#     formaient un mot coupé ou un mot composé) ;
#   * du texte pivoté, ou un bloc d'une seule ligne ;
#   * pas assez de place sous le paragraphe pour la ligne supplémentaire ;
#   * deux modifications dans le même paragraphe au cours du même appel.

# Compression maximale de l'interligne avant de renoncer au reflux.
MIN_LEADING_SQUEEZE = 0.9
# Au-delà de cet écart de corps dans un même bloc, on n'a pas affaire à un
# paragraphe mais à des éléments de nature différente réunis par erreur.
MAX_SIZE_SPREAD = 1.4


@dataclass
class Run:
    """Portion homogène d'un paragraphe : un style, un morceau de texte.

    `line` et `group` reprennent les index qui composent l'identifiant d'un
    fragment, ce qui permet de retrouver exactement la portion éditée.
    """

    text: str
    fontname: str
    size: float
    color: str
    alias: str
    line: int
    group: int


@dataclass
class Paragraph:
    """Un paragraphe recomposable : lignes cohérentes, place connue autour."""

    page: int
    line_bboxes: list[tuple[float, float, float, float]]
    baselines: list[float]
    runs: list[Run]
    first_left: float    # bord gauche de la première ligne (alinéa éventuel)
    left: float          # bord gauche des lignes suivantes
    right: float         # bord droit de la colonne
    leading: float       # interligne mesuré
    room_below: float    # espace vertical libre sous le paragraphe
    dominant: Run        # style majoritaire, qui sert de référence

    def runs_with(self, line: int, group: int, replacement: str) -> list[Run]:
        """Les mêmes portions, celle visée portant le nouveau texte."""
        out = []
        for run in self.runs:
            if run.line == line and run.group == group:
                out.append(replace(run, text=replacement))
            else:
                out.append(run)
        return out


@dataclass
class ReflowPlan:
    """Recomposition validée d'avance : on ne caviarde qu'une fois sûr d'écrire."""

    para: Paragraph
    lines: list                # lignes, chacune une liste de segments à écrire
    leading: float
    space: float               # largeur d'une espace, au style dominant


def _page_dict(page: fitz.Page) -> dict:
    """Même extraction que `extract_items`, pour que les index de blocs et de
    lignes désignent bien les mêmes éléments."""
    flags = fitz.TEXTFLAGS_DICT & ~fitz.TEXT_PRESERVE_LIGATURES
    return page.get_text("dict", flags=flags)


def paragraph_at(page: fitz.Page, raw: dict, bi: int) -> Paragraph | None:
    """Décrit le bloc `bi` s'il est recomposable, sinon None.

    Les styles multiples sont admis : un mot en gras au milieu d'un paragraphe
    est conservé tel quel à travers la recomposition. Ce sont les ruptures de
    *structure* qui font renoncer, pas les changements de typographie.
    """
    blocks = raw.get("blocks", [])
    if not 0 <= bi < len(blocks):
        return None
    block = blocks[bi]
    if block.get("type") != 0:
        return None
    lines = block.get("lines", [])
    if len(lines) < 2:
        return None

    runs: list[Run] = []
    texts: list[str] = []
    boxes: list[tuple[float, float, float, float]] = []
    baselines: list[float] = []

    for li, line in enumerate(lines):
        if abs(line.get("dir", (1, 0))[1]) > 0.01:
            return None
        spans = [s for s in line.get("spans", []) if s["text"].strip()]
        if not spans:
            return None

        # Regroupement identique à celui de `extract_items` : les index de
        # groupe doivent désigner les mêmes portions de part et d'autre.
        groups: list[dict] = []
        for span in spans:
            style = (span["font"], round(span["size"], 2), span["color"])
            if groups and groups[-1]["style"] == style:
                groups[-1]["text"] += span["text"]
            else:
                groups.append({"style": style, "text": span["text"], "flags": span["flags"]})

        for gi, group in enumerate(groups):
            fontname, size, color = group["style"]
            runs.append(Run(
                text=group["text"],
                fontname=fontname,
                size=size,
                color=_color_hex(color),
                alias=base14_alias(fontname, group["flags"]),
                line=li,
                group=gi,
            ))
        texts.append("".join(g["text"] for g in groups).strip())
        boxes.append(tuple(line["bbox"]))
        baselines.append(spans[0]["origin"][1])

    if any(t.endswith("-") for t in texts[:-1]):
        return None

    sizes = [r.size for r in runs]
    if max(sizes) > min(sizes) * MAX_SIZE_SPREAD:
        return None

    # Style dominant : celui qui porte le plus de caractères. Il donne
    # l'interligne, la largeur des espaces et le repli en cas de doute.
    weight: dict[tuple, int] = {}
    for run in runs:
        key = (run.fontname, run.size, run.color, run.alias)
        weight[key] = weight.get(key, 0) + len(run.text)
    top = max(weight, key=weight.get)
    dominant = next(r for r in runs
                    if (r.fontname, r.size, r.color, r.alias) == top)

    # La dernière ligne d'un paragraphe est courte : elle sous-estimerait la
    # largeur de la colonne, on la retire du calcul du bord droit.
    right = max(b[2] for b in boxes[:-1])
    right = min(right, page.rect.x1 - PAGE_MARGIN)
    body_left = min(b[0] for b in boxes[1:])

    gaps = [b - a for a, b in zip(baselines, baselines[1:])]
    gaps = [g for g in gaps if g > 0]
    leading = sorted(gaps)[len(gaps) // 2] if gaps else dominant.size * 1.18

    # Un « bloc » PyMuPDF n'est pas toujours un paragraphe : deux paragraphes
    # rapprochés peuvent y être regroupés. Les recomposer ensemble les fondrait
    # en un seul flux, ce qui perdrait la séparation. On cherche donc les indices
    # d'une rupture interne, et on renonce dès qu'on en trouve un.
    if any(g > leading * 1.35 for g in gaps):
        return None                                    # interligne élargi
    if any(b[0] > body_left + 4 for b in boxes[1:]):
        return None                                    # ligne en alinéa
    column = right - body_left
    if column > 0 and any(b[2] < right - column * 0.18 for b in boxes[:-1]):
        return None                                    # ligne courte en plein milieu

    rect = fitz.Rect(boxes[0])
    for box in boxes[1:]:
        rect |= fitz.Rect(box)

    return Paragraph(
        page=page.number,
        line_bboxes=boxes,
        baselines=baselines,
        runs=runs,
        first_left=boxes[0][0],
        left=body_left,
        right=right,
        leading=leading,
        room_below=_room_below(page, blocks, rect),
        dominant=dominant,
    )


def _room_below(page: fitz.Page, blocks: list[dict], rect: fitz.Rect) -> float:
    """Espace vertical libre sous un paragraphe, avant l'élément suivant.

    Seuls comptent les blocs qui chevauchent horizontalement la colonne : dans
    une mise en page à deux colonnes, celle d'à côté ne fait pas obstacle.
    """
    limit = page.rect.y1 - PAGE_MARGIN
    for block in blocks:
        bx0, by0, bx1, by1 = block["bbox"]
        if by0 <= rect.y1 + 0.5:
            continue
        if bx1 <= rect.x0 or bx0 >= rect.x1:
            continue
        limit = min(limit, by0)
    return max(0.0, limit - rect.y1)


def _tokenize(runs: list[Run], styles: dict) -> list[list[dict]]:
    """Découpe le paragraphe en mots, chacun pouvant mêler plusieurs styles.

    Un changement de style tombe rarement sur une frontière de mot — « ortho »
    en romain suivi de « graphe » en gras forme un seul mot. Un mot est donc une
    liste de segments, et non une chaîne.
    """
    tokens: list[list[dict]] = []
    current: list[dict] = []
    last_line = runs[0].line if runs else 0

    for run in runs:
        # Les lignes d'origine sont recollées par une espace : sans elle, le
        # dernier mot d'une ligne se souderait au premier de la suivante.
        if run.line != last_line:
            if current:
                tokens.append(current)
                current = []
            last_line = run.line
        style = styles[(run.fontname, run.size, run.color, run.alias)]
        for piece in re.split(r"(\s+)", run.text):
            if not piece:
                continue
            if piece.isspace():
                if current:
                    tokens.append(current)
                    current = []
                continue
            width = style["font"].text_length(piece, fontsize=run.size)
            current.append({"text": piece, "style": style, "size": run.size,
                            "color": run.color, "width": width})
    if current:
        tokens.append(current)
    return tokens


def _token_width(token: list[dict]) -> float:
    return sum(seg["width"] for seg in token)


def _wrap_tokens(tokens, space, first_width, width):
    """Répartit les mots en lignes. None si un mot excède la colonne à lui seul."""
    lines: list[list] = []
    current: list = []
    used = 0.0
    for token in tokens:
        w = _token_width(token)
        limit = first_width if not lines else width
        if w > limit:
            return None
        if current and used + space + w > limit:
            lines.append(current)
            current = [token]
            used = w
        else:
            used = used + space + w if current else w
            current.append(token)
    if current:
        lines.append(current)
    return lines


def plan_reflow(
    page: fitz.Page, raw: dict, item: TextItem, text: str, resolver: FontResolver
) -> ReflowPlan | None:
    """Prépare la recomposition du paragraphe contenant `item`, ou renonce.

    Tout est décidé ici, avant le moindre caviardage : si l'on effaçait le
    paragraphe pour découvrir ensuite qu'il ne se recompose pas, ses autres
    lignes seraient perdues.
    """
    parts = item.id.split("-")
    if len(parts) < 4:
        return None
    try:
        bi, li, gi = int(parts[1]), int(parts[2]), int(parts[3])
    except ValueError:
        return None
    para = paragraph_at(page, raw, bi)
    if para is None:
        return None
    if not any(r.line == li and r.group == gi for r in para.runs):
        return None

    runs = para.runs_with(li, gi, text.replace("\n", " ").strip())
    runs = [r for r in runs if r.text.strip()]
    if not runs:
        return None

    # Une police par style présent, résolue une seule fois.
    alone = {}
    for run in para.runs:
        alone[run.line] = alone.get(run.line, 0) + 1
    styles: dict = {}
    for run in runs:
        key = (run.fontname, run.size, run.color, run.alias)
        if key not in styles:
            # Une ligne d'origine qui ne portait qu'une portion donne sa largeur :
            # c'est le seul témoin fiable dont on dispose ici.
            probe = None
            if alone.get(run.line) == 1 and 0 <= run.line < len(para.line_bboxes):
                box = para.line_bboxes[run.line]
                probe = (run.text, run.size, box[2] - box[0])
            font, _ = resolver.resolve(page.number, run.fontname, run.alias, run.text, probe)
            styles[key] = {"font": font}

    tokens = _tokenize(runs, styles)
    if not tokens:
        return None
    dom = styles[(para.dominant.fontname, para.dominant.size,
                  para.dominant.color, para.dominant.alias)]
    space = dom["font"].text_length(" ", fontsize=para.dominant.size)

    lines = _wrap_tokens(tokens, space,
                         para.right - para.first_left, para.right - para.left)
    if not lines:
        return None

    leading = para.leading
    extra = len(lines) - len(para.baselines)
    if extra > 0 and extra * leading > para.room_below:
        # Pas la place d'ajouter les lignes : reste à resserrer l'interligne.
        span = para.baselines[-1] - para.baselines[0] + para.room_below
        squeezed = span / (len(lines) - 1) if len(lines) > 1 else leading
        if squeezed < leading * MIN_LEADING_SQUEEZE:
            return None
        leading = squeezed
    return ReflowPlan(para=para, lines=lines, leading=leading, space=space)


def run_reflow(page: fitz.Page, plan: ReflowPlan) -> None:
    """Écrit les lignes recomposées. Le caviardage a déjà eu lieu.

    Chaque segment est posé à sa propre abscisse, avec sa police, son corps et sa
    couleur : c'est ce qui permet à un mot en gras de le rester.
    """
    para = plan.para
    y = para.baselines[0]
    for index, line in enumerate(plan.lines):
        x = para.first_left if index == 0 else para.left
        for token in line:
            for seg in token:
                _write_line(page, fitz.Point(x, y), seg["text"],
                            seg["style"]["font"], seg["size"], seg["color"])
                x += seg["width"]
            x += plan.space
        y += plan.leading

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
    reflowed: int = 0       # paragraphes recomposés au lieu d'être rétrécis

    def __bool__(self) -> bool:
        return bool(self.changed)


def _overflows(item: TextItem, text: str, resolver: FontResolver, style: Style | None) -> bool:
    """Le nouveau texte dépasse-t-il la place disponible sur la ligne ?"""
    clean = text.replace("\n", " ").rstrip()
    if not clean:
        return False
    if style is not None and style.family:
        font, _ = fonts.choice_font(style.family, style.bold, style.italic)
    else:
        font, _ = resolver.resolve(item.page, item.fontname, item.font, clean, _probe(item))
    return font.text_length(clean, fontsize=item.size) > max(item.max_x - item.bbox[0], 1.0)


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
        raw = _page_dict(page)

        # Deux modifications dans un même bloc : le recomposer n'appliquerait que
        # l'une des deux, on s'en abstient.
        blocks = [item.id.split("-")[1] for item, _ in pairs]
        alone = {b for b in blocks if blocks.count(b) == 1}

        # Le plan de reflux se calcule avant tout effacement : une fois le
        # paragraphe caviardé, il est trop tard pour changer d'avis.
        plans: list[tuple[TextItem, EditSpec, ReflowPlan | None]] = []
        for item, spec in pairs:
            plan = None
            if (
                item.id.split("-")[1] in alone
                and spec.style is None
                and _overflows(item, spec.text, resolver, spec.style)
            ):
                plan = plan_reflow(page, raw, item, spec.text, resolver)
            plans.append((item, spec, plan))

        rects: list[fitz.Rect] = []
        for item, _, plan in plans:
            if plan is None:
                rects.append(fitz.Rect(item.bbox))
            else:
                rects.extend(fitz.Rect(b) for b in plan.para.line_bboxes)
        _erase(page, rects)

        for item, spec, plan in plans:
            if plan is not None:
                run_reflow(page, plan)
                result.reflowed += 1
            elif not _write(page, item, spec.text, resolver, spec.style):
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


def insert_image(
    doc: fitz.Document, pno: int, x: float, y: float, width: float, height: float, data: bytes
) -> tuple[float, float, float, float]:
    """Pose une image (logo, tampon, signature scannée) sur une page.

    (x, y) est le coin haut-gauche, en points PDF. Le rectangle est ramené dans
    la page si l'utilisateur a débordé, et les proportions de l'image sont
    conservées : elle est centrée dans le cadre demandé. Renvoie le rectangle
    réellement utilisé.
    """
    page = doc[pno]
    rect = fitz.Rect(x, y, x + max(width, 1.0), y + max(height, 1.0)) & page.rect
    if rect.is_empty or rect.width < 1 or rect.height < 1:
        raise ValueError("L'emplacement demandé est en dehors de la page.")
    # `keep_proportion` évite d'étirer une signature ; MuPDF lit lui-même le
    # format de l'image (PNG, JPEG, GIF, BMP, TIFF…) et lève si elle est illisible.
    page.insert_image(rect, stream=data, keep_proportion=True)
    return (rect.x0, rect.y0, rect.x1, rect.y1)


def redact_area(
    doc: fitz.Document, pno: int, x0: float, y0: float, x1: float, y1: float, blackout: bool
) -> bool:
    """Caviarde une zone rectangulaire : le contenu est retiré, pas seulement masqué.

    `blackout` pose en plus un rectangle noir, comme sur un document
    déclassifié ; sans lui la zone est simplement vidée. Dans les deux cas le
    texte est supprimé du PDF et les pixels des images situés dans la zone sont
    effacés — un copier-coller depuis un lecteur ne peut plus rien retrouver.
    Renvoie False si la zone est trop petite pour être exploitable.
    """
    page = doc[pno]
    rect = fitz.Rect(min(x0, x1), min(y0, y1), max(x0, x1), max(y0, y1)) & page.rect
    if rect.is_empty or rect.width < 1 or rect.height < 1:
        return False
    page.add_redact_annot(rect, fill=(0, 0, 0) if blackout else None)
    page.apply_redactions(
        images=fitz.PDF_REDACT_IMAGE_PIXELS,
        graphics=fitz.PDF_REDACT_LINE_ART_NONE,
        text=fitz.PDF_REDACT_TEXT_REMOVE,
    )
    return True


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


def move_page_to(doc: fitz.Document, pno: int, target: int) -> int:
    """Déplace une page à une position quelconque (glisser-déposer des vignettes).

    `move_page` insère *avant* la position indiquée. Descendre une page décale
    donc la cible d'un cran, la place libérée par le retrait comptant elle aussi.
    """
    if not 0 <= target < doc.page_count or target == pno:
        return pno
    doc.move_page(pno, target if target < pno else target + 1)
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


# --------------------------------------------------------------------------
# Formulaires
# --------------------------------------------------------------------------

# Types de champs que l'interface sait présenter.
_WIDGET_KINDS = {
    fitz.PDF_WIDGET_TYPE_TEXT: "text",
    fitz.PDF_WIDGET_TYPE_CHECKBOX: "checkbox",
    fitz.PDF_WIDGET_TYPE_RADIOBUTTON: "radio",
    fitz.PDF_WIDGET_TYPE_COMBOBOX: "choice",
    fitz.PDF_WIDGET_TYPE_LISTBOX: "choice",
}


def _on_states(widget) -> list[str]:
    """États « coché » d'une case ou d'un bouton radio, hors « Off »."""
    try:
        states = widget.button_states() or {}
    except Exception:
        return []
    found: list[str] = []
    for group in states.values():
        for state in group or []:
            if state and state != "Off" and state not in found:
                found.append(state)
    return found


def list_fields(doc: fitz.Document) -> list[dict]:
    """Champs de formulaire du document, regroupés par nom.

    Un même nom peut porter plusieurs widgets — c'est le cas d'un groupe de
    boutons radio, et d'un champ répété sur plusieurs pages. On les présente donc
    une seule fois, en mémorisant les pages concernées.
    """
    fields: dict[str, dict] = {}
    for pno in range(doc.page_count):
        for widget in doc[pno].widgets():
            kind = _WIDGET_KINDS.get(widget.field_type)
            if kind is None:      # bouton d'action, signature… rien à saisir
                continue
            name = widget.field_name or ""
            if not name:
                continue
            entry = fields.get(name)
            if entry is None:
                value = widget.field_value
                entry = {
                    "name": name,
                    "kind": kind,
                    "value": "" if value is None else str(value),
                    "options": list(widget.choice_values or []) if kind == "choice" else [],
                    "max_len": int(widget.text_maxlen or 0),
                    "pages": [],
                }
                fields[name] = entry
            # Pour un groupe de radios, la valeur utile est celle du bouton coché.
            if kind == "radio" and widget.field_value not in (None, "", "Off"):
                entry["value"] = str(widget.field_value)
            # Cases et radios n'exposent pas leurs choix dans `choice_values` mais
            # dans leurs états : c'est le nom de l'état « coché » qu'il faudra
            # écrire pour sélectionner ce bouton précis.
            if kind in ("checkbox", "radio"):
                for state in _on_states(widget):
                    if state not in entry["options"]:
                        entry["options"].append(state)
            if pno not in entry["pages"]:
                entry["pages"].append(pno)
    return list(fields.values())


def fill_fields(doc: fitz.Document, values: dict[str, str]) -> int:
    """Renseigne les champs nommés. Renvoie le nombre de widgets mis à jour."""
    filled = 0
    for pno in range(doc.page_count):
        for widget in doc[pno].widgets():
            name = widget.field_name or ""
            if name not in values:
                continue
            raw = values[name]
            kind = _WIDGET_KINDS.get(widget.field_type)
            if kind == "checkbox":
                widget.field_value = str(raw).lower() in ("1", "true", "on", "oui")
            elif kind == "radio":
                # Un radio ne se cocher qu'en lui donnant son propre nom d'état ;
                # les autres boutons du groupe se décochent d'eux-mêmes.
                widget.field_value = str(raw)
            else:
                widget.field_value = str(raw)
            try:
                widget.update()
            except Exception:
                continue      # widget récalcitrant : on passe au suivant
            filled += 1
    return filled


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
