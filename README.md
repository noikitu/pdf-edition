# LemonPDF

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
| Changer la police | pendant l'édition, menu **Arial / Times New Roman** + `G` / `I` au-dessus du texte |
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
2. une **réécriture** sur la même ligne de base, à la même taille et dans la même
   couleur ;
3. un nouveau rendu de la page, qui sert de retour visuel — ce que l'on voit est donc
   exactement le contenu du PDF, pas une simulation.

Si le texte saisi est plus long que la place disponible, la taille est réduite
progressivement (jusqu'à 75 % au maximum) pour ne pas déborder sur le fragment suivant.

### Le choix de la police

Par défaut, l'app essaie de conserver la typographie d'origine, dans cet ordre :

1. **la police embarquée dans le PDF** — rendu strictement identique. Elle n'est
   retenue que si elle contient tous les caractères saisis : les PDF n'embarquent
   qu'un *sous-ensemble* de chaque police, limité aux glyphes réellement employés,
   et écrire un caractère absent produirait un blanc ;
2. **une police système de la même famille** (Arial, Times New Roman…), qui couvre
   tout le latin pour un rendu visuellement identique ;
3. **une police Base-14** approchante, toujours disponible.

Le menu affiché pendant l'édition permet d'imposer **Arial** ou **Times New Roman**,
en gras et/ou italique, au lieu de cette détection automatique.

À l'export, les polices sont réduites aux seuls glyphes utilisés : écrire du texte
embarque sinon la police entière, ce qui ajoute plus d'un mégaoctet par police.

## Limites connues

Ce sont celles du format PDF lui-même, qui n'a aucune notion de paragraphe :

- **Pas de reflow.** Rallonger fortement une phrase ne repousse pas les lignes suivantes.
  L'outil vise la correction (fautes, dates, noms, montants), pas la réécriture d'un
  paragraphe entier.
- **PDF scannés.** Une page qui n'est qu'une image ne contient pas de texte à éditer ; il
  faut d'abord lui appliquer un OCR.
- **Polices non standard.** Quand ni la police embarquée ni une police système de la même
  famille ne convient, le texte réécrit tombe sur une Base-14 approchante ; l'app le
  signale alors par « police substituée ».
- **Texte pivoté** (vertical, en diagonale) : affiché, mais non éditable.
- **PDF protégés par mot de passe** : à déverrouiller au préalable.
- Alphabets hors latin-1 : le chinois / japonais / coréen utilise une police intégrée ;
  les autres écritures nécessitent une police système Unicode (Arial Unicode, DejaVu Sans).

## Structure

```
app/
  main.py        API FastAPI (upload, rendu, édition, export)
  pdf_ops.py     moteur PyMuPDF : extraction et réécriture du texte
  fonts.py       choix de la police : embarquée, système, ou Base-14
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
