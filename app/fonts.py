"""Choix de la police utilisée pour réécrire un texte.

Trois niveaux, du plus fidèle au plus approximatif :

1. la police **embarquée dans le PDF** — rendu identique à l'original, mais elle
   est presque toujours sous-ensemblée : elle ne contient que les glyphes déjà
   employés dans le document, d'où la vérification de couverture ;
2. une police **système de la même famille** (Arial, Calibri, Times…), qui
   couvre tout le latin et reste très proche visuellement ;
3. une police **Base-14** approchante (sérif / sans / mono, gras, italique),
   toujours disponible.
"""

from __future__ import annotations

import os
import re
from pathlib import Path

import fitz

# Préfixe de sous-ensemble ajouté par les générateurs de PDF : « BCDEEE+Aptos ».
_SUBSET = re.compile(r"^[A-Z]{6}\+")
# Suffixes techniques sans valeur typographique : ArialMT, MinionPro, …
_SUFFIX = re.compile(r"(psmt|mt|ps|std|pro|regular)$")

_BOLD_WORDS = ("bold", "black", "heavy", "semibold", "demibold")
_ITALIC_WORDS = ("italic", "oblique")

_FONT_DIRS = (
    "/System/Library/Fonts",
    "/System/Library/Fonts/Supplemental",
    "/Library/Fonts",
    os.path.expanduser("~/Library/Fonts"),
    "/usr/share/fonts",
    "/usr/local/share/fonts",
    os.path.expanduser("~/.fonts"),
    "C:/Windows/Fonts",
)
# Les collections .ttc contiennent plusieurs polices ; on ne saurait pas laquelle
# est chargée, donc on les ignore.
_FONT_EXTS = (".ttf", ".otf")

# Familles Base-14, toujours disponibles : (normal, gras, italique, gras-italique)
SANS = ("helv", "hebo", "heit", "hebi")
SERIF = ("tiro", "tibo", "tiit", "tibi")
MONO = ("cour", "cobo", "coit", "cobi")

# Polices proposées dans l'interface. `families` est essayé dans l'ordre : la
# première est la police demandée, les suivantes sont des substituts au dessin
# identique (les Liberation sont métriquement compatibles avec les Microsoft).
CHOICES = (
    {"key": "arial", "label": "Arial",
     "families": ("arial", "liberationsans", "helvetica"), "base14": SANS},
    {"key": "times", "label": "Times New Roman",
     "families": ("timesnewroman", "liberationserif", "times"), "base14": SERIF},
)
_BY_KEY = {c["key"]: c for c in CHOICES}


def style_index(bold: bool, italic: bool) -> int:
    return (1 if bold else 0) + (2 if italic else 0)


def _norm(text: str) -> str:
    return re.sub(r"[^a-z0-9]", "", text.lower())


def parse_basefont(basefont: str) -> tuple[str, bool, bool]:
    """« BCDGEE+Aptos,Bold » -> ("Aptos", True, False)."""
    name = _SUBSET.sub("", basefont or "").replace(",", "-")
    low = name.lower()
    bold = any(w in low for w in _BOLD_WORDS)
    italic = any(w in low for w in _ITALIC_WORDS)
    family = name.split("-")[0]
    family = _SUFFIX.sub("", family.lower()) or family.lower()
    return family, bold, italic


# --------------------------------------------------------------- polices système

_system_index: dict[str, str] | None = None


def _index_system_fonts() -> dict[str, str]:
    """Recense une fois pour toutes les polices installées, par nom normalisé."""
    global _system_index
    if _system_index is not None:
        return _system_index
    index: dict[str, str] = {}
    for directory in _FONT_DIRS:
        base = Path(directory)
        if not base.is_dir():
            continue
        try:
            for path in base.rglob("*"):
                if path.suffix.lower() in _FONT_EXTS:
                    index.setdefault(_norm(path.stem), str(path))
        except OSError:
            continue
    _system_index = index
    return index


def find_system_font(family: str, bold: bool, italic: bool) -> str | None:
    """Cherche un fichier de police système correspondant à la famille demandée."""
    index = _index_system_fonts()
    base = _norm(family)
    if not base:
        return None
    styles: list[str] = []
    if bold and italic:
        styles += ["bolditalic", "boldoblique", "bditalic"]
    elif bold:
        styles += ["bold", "bd"]
    elif italic:
        styles += ["italic", "oblique", "it"]
    styles.append("")
    for style in styles:
        for candidate in (base + style, base + "-" + style if style else base):
            path = index.get(_norm(candidate))
            if path:
                return path
    return None


# ------------------------------------------------------------ polices embarquées

# Contrôle des métriques : bornes de l'écart admis entre la largeur calculée par
# la police et la largeur réellement occupée dans la page. Le seuil haut est
# serré — une police plus large que le rendu trahit un encodage faux — le seuil
# bas est lâche, une justification élargissant légitimement le rendu.
METRIC_CEILING = 1.04
METRIC_FLOOR = 0.75


class FontResolver:
    """Résout la police d'écriture d'un fragment, avec mise en cache par document."""

    def __init__(self, doc: fitz.Document):
        self.doc = doc
        self._page_maps: dict[int, dict[str, int]] = {}
        self._embedded: dict[int, tuple[bytes, fitz.Font] | None] = {}
        self._files: dict[str, fitz.Font | None] = {}
        # Verdict du contrôle de métriques, retenu par police : une fois qu'on
        # sait qu'un encodage ment, inutile de le remesurer à chaque fragment.
        self._trusted: dict[tuple, bool] = {}

    # -- niveau 1 : la police du PDF ---------------------------------------
    def _page_map(self, pno: int) -> dict[str, int]:
        """{nom de police normalisé: xref} pour une page."""
        cached = self._page_maps.get(pno)
        if cached is None:
            cached = {}
            for info in self.doc[pno].get_fonts(full=True):
                xref, ftype, basefont = info[0], info[2], info[3]
                if not xref or ftype == "Type3":
                    continue
                cached.setdefault(_norm(_SUBSET.sub("", basefont)), xref)
            self._page_maps[pno] = cached
        return cached

    def _load_embedded(self, xref: int) -> tuple[bytes, fitz.Font] | None:
        if xref not in self._embedded:
            loaded = None
            try:
                buffer = self.doc.extract_font(xref)[3]
                if buffer:
                    loaded = (buffer, fitz.Font(fontbuffer=buffer))
            except Exception:
                loaded = None   # police illisible ou non extractible
            self._embedded[xref] = loaded
        return self._embedded[xref]

    # -- niveau 2 : une police système --------------------------------------
    def _load_file(self, path: str) -> fitz.Font | None:
        if path not in self._files:
            try:
                self._files[path] = fitz.Font(fontfile=path)
            except Exception:
                self._files[path] = None
        return self._files[path]

    # -- résolution ---------------------------------------------------------
    @staticmethod
    def _covers(font: fitz.Font, text: str) -> bool:
        return all(font.has_glyph(ord(ch)) for ch in text)

    def _metrics_ok(self, key, font: fitz.Font, probe) -> bool:
        """La police reproduit-elle la largeur réellement occupée par le texte ?

        `has_glyph` ne dit que la présence d'un dessin, pas sa justesse. Une
        police embarquée dont la table d'encodage est décalée — cas courant des
        sous-ensembles à encodage propre — possède tous les glyphes demandés
        mais rend les mauvais : « Python » s'écrit alors « R{vjqp », sans
        qu'aucune erreur ne soit levée.

        On compare donc la largeur que cette police donnerait au texte d'origine
        avec celle qu'il occupe réellement dans la page. Un encodage juste tombe
        à 0,1 % près ; un décalage se trahit par plusieurs pour cent. Le seuil
        est asymétrique : une justification ne peut qu'élargir le rendu, jamais
        le resserrer, donc une police plus large que le rendu est bien plus
        suspecte qu'une police plus étroite.
        """
        if key in self._trusted:
            return self._trusted[key]
        if not probe:
            return True                      # rien pour juger : on laisse passer
        text, size, width = probe
        if not text.strip() or width <= 1 or size <= 0:
            return True
        natural = font.text_length(text, fontsize=size)
        ok = width * METRIC_FLOOR <= natural <= width * METRIC_CEILING
        self._trusted[key] = ok
        return ok

    def resolve(
        self, pno: int, basefont: str, fallback_alias: str, text: str, probe=None
    ) -> tuple[fitz.Font, bool]:
        """Renvoie (police à utiliser, typographie d'origine préservée ?).

        Le second élément est vrai pour les niveaux 1 et 2 — dans les deux cas
        c'est bien la police du document que le lecteur verra —, et faux pour le
        repli Base-14, qui est la seule véritable approximation.
        """
        family, bold, italic = parse_basefont(basefont)

        page_map = self._page_map(pno)
        xref = page_map.get(_norm(basefont)) or page_map.get(_norm(family))
        if xref:
            embedded = self._load_embedded(xref)
            if (embedded and self._covers(embedded[1], text)
                    and self._metrics_ok(("pdf", xref), embedded[1], probe)):
                return embedded[1], True

        path = find_system_font(family, bold, italic)
        if path:
            font = self._load_file(path)
            # Le même contrôle vaut pour une police système : une substitution
            # aux métriques différentes déformerait la ligne.
            if (font and self._covers(font, text)
                    and self._metrics_ok(("sys", path), font, probe)):
                return font, True

        return base14_font(fallback_alias, text), False


def choice_font(key: str, bold: bool, italic: bool) -> tuple[fitz.Font, bool]:
    """Police explicitement demandée par l'utilisateur.

    Renvoie (police, True si c'est bien celle demandée et non un substitut).
    """
    entry = _BY_KEY.get(key)
    if entry is None:
        raise KeyError(key)
    for rank, family in enumerate(entry["families"]):
        path = find_system_font(family, bold, italic)
        if path:
            try:
                return fitz.Font(fontfile=path), rank == 0
            except Exception:
                continue
    return fitz.Font(entry["base14"][style_index(bold, italic)]), False


def catalogue() -> list[dict]:
    """Liste des polices proposées, avec leur disponibilité réelle."""
    return [
        {
            "key": entry["key"],
            "label": entry["label"],
            "available": find_system_font(entry["families"][0], False, False) is not None,
        }
        for entry in CHOICES
    ]


def base14_font(alias: str, text: str) -> fitz.Font:
    """Police standard, avec bascule Unicode si le texte sort du latin-1."""
    try:
        text.encode("latin-1")
        return fitz.Font(alias)
    except UnicodeEncodeError:
        pass
    for name in ("china-s", "japan", "korea"):
        font = fitz.Font(name)
        if FontResolver._covers(font, text):
            return font
    for family in ("arialunicode", "dejavusans", "notosans"):
        path = find_system_font(family, False, False)
        if path:
            try:
                return fitz.Font(fontfile=path)
            except Exception:
                continue
    return fitz.Font(alias)   # glyphes manquants remplacés par des puces
