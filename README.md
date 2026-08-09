# Éditeur PDF

Application web **gratuite** pour corriger le texte d'un PDF comme dans un traitement de
texte : on clique sur une phrase, on la réécrit, on télécharge le PDF corrigé.
100 % Python (FastAPI + PyMuPDF) côté serveur, aucune dépendance JavaScript côté client.

Les fichiers ne quittent jamais la machine sur laquelle l'application tourne : ils sont
gardés en mémoire le temps de la session, jamais écrits sur disque.

## Démarrage

```bash
git clone https://github.com/noikitu/pdf-edition.git
cd pdf-edition
python3 -m venv .venv && source .venv/bin/activate
pip install -r requirements.txt
python run.py
```

Le navigateur s'ouvre sur <http://127.0.0.1:8000>. Options : `--port`, `--host`,
`--no-browser`, `--reload`.

## Utilisation

| Action | Comment |
| --- | --- |
| Ouvrir un PDF | glisser-déposer, ou « Choisir un PDF » |
| Corriger une phrase | cliquer dessus, taper, `Entrée` pour valider (`Échap` annule) |
| Supprimer un texte | le vider puis `Entrée` |
| Corriger partout | « Remplacer » → mot fautif, mot correct, « Tout remplacer » |
| Ajouter un texte | « Texte », cliquer à l'endroit voulu, saisir, « Ajouter » |
| Annuler / rétablir | `Ctrl+Z` / `Ctrl+Maj+Z` |
| Récupérer le résultat | « Télécharger » (`Ctrl+S`) |

Le remplacement global respecte la casse d'origine : `ortografe → orthographe`
transforme aussi `Ortografe` en `Orthographe`.

## Comment ça marche

Chaque page est rendue en image par le serveur, et une couche de texte transparente est
positionnée par-dessus, aux coordonnées exactes de chaque fragment du PDF. Modifier un
fragment déclenche côté serveur :

1. un **caviardage** (`redaction`) de la zone d'origine, qui retire l'ancien texte sans
   toucher aux images ni aux traits de la page ;
2. une **réécriture** sur la même ligne de base, avec la police Base-14 la plus proche de
   l'originale (sérif / sans / mono, gras, italique), la même taille et la même couleur ;
3. un nouveau rendu de la page, qui sert de retour visuel — ce que l'on voit est donc
   exactement le contenu du PDF, pas une simulation.

Si le texte saisi est plus long que la place disponible, la taille est réduite
progressivement (jusqu'à 75 % au maximum) pour ne pas déborder sur le fragment suivant.

## Limites connues

Ce sont celles du format PDF lui-même, qui n'a aucune notion de paragraphe :

- **Pas de reflow.** Rallonger fortement une phrase ne repousse pas les lignes suivantes.
  L'outil vise la correction (fautes, dates, noms, montants), pas la réécriture d'un
  paragraphe entier.
- **PDF scannés.** Une page qui n'est qu'une image ne contient pas de texte à éditer ; il
  faut d'abord lui appliquer un OCR.
- **Polices non standard.** Le texte réécrit utilise une police Base-14 approchante et non
  la police exacte du document (celle-ci n'est pas toujours intégrée ni redistribuable).
- **Texte pivoté** (vertical, en diagonale) : affiché, mais non éditable.
- **PDF protégés par mot de passe** : à déverrouiller au préalable.
- Alphabets hors latin-1 : le chinois / japonais / coréen utilise une police intégrée ;
  les autres écritures nécessitent une police système Unicode (Arial Unicode, DejaVu Sans).

## Structure

```
app/
  main.py        API FastAPI (upload, rendu, édition, export)
  pdf_ops.py     moteur PyMuPDF : extraction et réécriture du texte
  store.py       sessions en mémoire + pile d'annulation
  static/        interface (HTML / CSS / JS sans dépendance)
run.py           lancement local
tests/           tests du moteur d'édition
```

L'API est documentée automatiquement sur `/api/docs`.

## Tests

```bash
pip install -r requirements-dev.txt
pytest
```

## Licence

Le code de ce dépôt est libre d'usage. Attention toutefois : **PyMuPDF est distribué sous
AGPL-3.0**. Toute redistribution ou mise à disposition en ligne de cette application doit
donc respecter l'AGPL (publication du code source), sauf licence commerciale PyMuPDF.
Pour un usage interne / local, aucune contrainte.
