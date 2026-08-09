"""Tests du moteur d'édition PDF."""

import fitz
import pytest

from app import pdf_ops


@pytest.fixture()
def doc():
    d = fitz.open()
    page = d.new_page()
    page.insert_text((72, 100), "Titre du document", fontname="hebo", fontsize=20)
    page.insert_text((72, 140), "Une faute d'ortografe a corriger.", fontname="helv", fontsize=12)
    page.insert_text((72, 160), "Texte en serif ici.", fontname="tiro", fontsize=11)
    page.insert_text((72, 180), "code@exemple.fr", fontname="cour", fontsize=10, color=(0, 0, 1))
    page.draw_line(fitz.Point(72, 200), fitz.Point(520, 200))
    d.new_page().insert_text((72, 100), "Ortografe encore.", fontname="helv", fontsize=12)
    yield d
    d.close()


def item_starting(doc, pno, prefix):
    return next(i for i in pdf_ops.extract_items(doc, pno) if i.text.startswith(prefix))


# ------------------------------------------------------------------ extraction

def test_extraction_du_style(doc):
    titre = item_starting(doc, 0, "Titre")
    assert titre.size == 20
    assert titre.bold and titre.font == "hebo"

    serif = item_starting(doc, 0, "Texte en serif")
    assert serif.font == "tiro"

    mono = item_starting(doc, 0, "code@")
    assert mono.font == "cour"
    assert mono.color == "#0000ff"


def test_identifiants_stables(doc):
    assert [i.id for i in pdf_ops.extract_items(doc, 0)] == [
        i.id for i in pdf_ops.extract_items(doc, 0)
    ]


def test_pages_et_rendu(doc):
    pages = pdf_ops.page_info(doc)
    assert len(pages) == 2 and pages[0]["width"] > 0
    assert pdf_ops.render_page(doc, 0, scale=1).startswith(b"\x89PNG")


# --------------------------------------------------------------------- édition

def test_modification_remplace_le_texte(doc):
    item = item_starting(doc, 0, "Une faute")
    assert pdf_ops.apply_edits(doc, {item.id: "Une faute d'orthographe corrigée."}) == 1
    texte = doc[0].get_text()
    assert "Une faute d'orthographe corrigée." in texte
    assert "ortografe" not in texte


def test_modification_conserve_les_traits(doc):
    avant = len(doc[0].get_drawings())
    pdf_ops.apply_edits(doc, {item_starting(doc, 0, "Titre").id: "Nouveau titre"})
    assert len(doc[0].get_drawings()) == avant


def test_texte_vide_supprime_le_fragment(doc):
    item = item_starting(doc, 0, "code@")
    assert pdf_ops.apply_edits(doc, {item.id: ""}) == 1
    assert "code@exemple.fr" not in doc[0].get_text()


def test_texte_identique_ignore(doc):
    item = item_starting(doc, 0, "Titre")
    assert pdf_ops.apply_edits(doc, {item.id: item.text}) == 0


def test_identifiant_inconnu_ignore(doc):
    assert pdf_ops.apply_edits(doc, {"0-99-99-99": "peu importe"}) == 0


def test_texte_long_est_reduit_mais_lisible(doc):
    item = item_starting(doc, 0, "Texte en serif")
    long = "Texte en serif ici, mais nettement plus long qu'auparavant pour tenir."
    pdf_ops.apply_edits(doc, {item.id: long})
    apres = item_starting(doc, 0, "Texte en serif")
    assert apres.text == long
    assert item.size * pdf_ops.MIN_SHRINK <= apres.size <= item.size
    assert apres.bbox[2] <= doc[0].rect.x1


def test_accents_preserves(doc):
    item = item_starting(doc, 0, "Une faute")
    pdf_ops.apply_edits(doc, {item.id: "Éàçùôî — accents préservés"})
    assert "Éàçùôî — accents préservés" in doc[0].get_text()


# ------------------------------------------------------- recherche / remplacement

def test_remplacement_global_multi_pages(doc):
    assert pdf_ops.count_matches(doc, "ortografe", case_sensitive=False) == 2
    assert pdf_ops.replace_all(doc, "ortografe", "orthographe", case_sensitive=False) == 2
    assert "orthographe" in doc[0].get_text()
    assert "Orthographe" in doc[1].get_text()  # la casse d'origine est respectée
    assert pdf_ops.count_matches(doc, "ortografe", case_sensitive=False) == 0


def test_remplacement_sensible_a_la_casse(doc):
    assert pdf_ops.replace_all(doc, "Ortografe", "Orthographe", case_sensitive=True) == 1
    assert "ortografe" in doc[0].get_text()


def test_recherche_vide(doc):
    assert pdf_ops.count_matches(doc, "", False) == 0
    assert pdf_ops.replace_all(doc, "", "x", False) == 0


# ------------------------------------------------- résolution des identifiants

def test_resolution_identifiant_valide(doc):
    item = item_starting(doc, 0, "Titre")
    resolus, restants = pdf_ops.resolve_edits(
        doc, [{"id": item.id, "original": item.text, "text": "Nouveau titre"}]
    )
    assert resolus == {item.id: "Nouveau titre"} and restants == []


def test_resolution_par_le_contenu_si_identifiant_perime(doc):
    """Après une modification, les index bougent : on retrouve le fragment par son texte."""
    cible = item_starting(doc, 0, "code@")
    pdf_ops.apply_edits(doc, {item_starting(doc, 0, "Titre").id: "Titre plus long qu'avant"})

    resolus, restants = pdf_ops.resolve_edits(
        doc, [{"id": cible.id, "original": cible.text, "text": "nouveau@exemple.fr"}]
    )
    assert restants == []
    assert pdf_ops.apply_edits(doc, resolus) == 1
    assert "nouveau@exemple.fr" in doc[0].get_text()
    assert "Titre plus long qu'avant" in doc[0].get_text()   # rien n'a été écrasé


def test_identifiant_perime_sans_correspondance_est_refuse(doc):
    item = item_starting(doc, 0, "Titre")
    resolus, restants = pdf_ops.resolve_edits(
        doc, [{"id": item.id, "original": "texte qui n'existe plus", "text": "peu importe"}]
    )
    assert resolus == {} and len(restants) == 1


def test_identifiant_hors_page_est_refuse(doc):
    resolus, restants = pdf_ops.resolve_edits(
        doc, [{"id": "42-0-0-0", "original": "x", "text": "y"}]
    )
    assert resolus == {} and len(restants) == 1


# ----------------------------------------------------------------- ajout de texte

def test_ajout_de_texte(doc):
    pdf_ops.add_textbox(doc, 0, 72, 300, "Ligne une\nLigne deux", size=14, color="#ff0000", bold=True)
    ajout = item_starting(doc, 0, "Ligne une")
    assert ajout.size == 14 and ajout.bold and ajout.color == "#ff0000"
    assert "Ligne deux" in doc[0].get_text()


# ------------------------------------------------------------------- utilitaires

@pytest.mark.parametrize(
    "nom, attendu",
    [
        ("Helvetica", "helv"),
        ("Arial-BoldMT", "hebo"),
        ("Times-Italic", "tiit"),
        ("Georgia-BoldItalic", "tibi"),
        ("CourierNewPSMT", "cour"),
    ],
)
def test_correspondance_des_polices(nom, attendu):
    assert pdf_ops.base14_alias(nom, 0) == attendu


def test_police_de_repli_hors_latin1():
    nom, _ = pdf_ops.resolve_font("helv", "Bonjour")
    assert nom == "helv"
    nom_cjk, _ = pdf_ops.resolve_font("helv", "こんにちは")
    assert nom_cjk != "helv"
