# LemonPDF

Application web **gratuite** pour corriger le texte d'un PDF comme dans un traitement de
texte : on clique sur une phrase, on la réécrit, on télécharge le PDF corrigé.
100 % Python (FastAPI + PyMuPDF) côté serveur, aucune dépendance JavaScript côté client.

Les fichiers ne quittent jamais la machine sur laquelle l'application tourne. Le document
en cours y est sauvegardé (`~/.lemonpdf/sessions`) afin de survivre à un redémarrage du
serveur ; fermer le document efface cette sauvegarde, et `LEMONPDF_AUTOSAVE=0` revient à
un fonctionnement strictement en mémoire.

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

## Déploiement

L'application n'a besoin d'aucune base de données ni service externe : un seul
processus suffit.

```bash
docker compose up --build          # puis http://localhost:8000
```

Ou directement :

```bash
docker build -t lemonpdf . && docker run -p 8000:8000 lemonpdf
```

Sur un hébergeur : **Render** (« New → Blueprint » sur ce dépôt, `render.yaml` est
fourni), ou n'importe quelle plateforme qui lit un `Dockerfile` ou un `Procfile`
(Railway, Fly.io, Cloud Run, Heroku). La variable standard `PORT` est respectée.

Tout se règle par variables d'environnement, sans toucher au code :

| Variable | Défaut | Rôle |
| --- | --- | --- |
| `HOST` / `PORT` | `127.0.0.1` / `8000` | adresse d'écoute |
| `LEMONPDF_MAX_UPLOAD_MB` | `40` | taille maximale d'un fichier envoyé |
| `LEMONPDF_SESSION_TTL` | `21600` | durée de vie d'un document inactif, en secondes |
| `LEMONPDF_MAX_SESSIONS` | `40` | documents gardés en mémoire simultanément |
| `LEMONPDF_OPEN_BROWSER` | `1` | ouvrir le navigateur au démarrage |
| `LEMONPDF_AUTOSAVE` | `1` | sauvegarder les documents en cours sur disque |
| `LEMONPDF_DATA_DIR` | `~/.lemonpdf` | où sont écrites ces sauvegardes |

Un point à connaître : **un seul worker**. Les documents en cours d'édition vivent dans
la mémoire du processus, un second worker ne les verrait pas — d'où `workers=1` dans
`run.py`. Pour encaisser plus de monde, on met plusieurs instances derrière un
répartiteur avec des sessions collantes, pas plusieurs workers.

## Utilisation

| Action | Comment |
| --- | --- |
| Ouvrir un PDF | glisser-déposer, ou « Choisir un PDF » |
| Corriger une phrase | cliquer dessus, taper, `Entrée` pour valider (`Échap` annule) |
| Changer la police | pendant l'édition, menu **Arial / Times New Roman** + `G` / `I` au-dessus du texte |
| Supprimer un texte | le vider puis `Entrée` |
| Corriger partout | « Remplacer » → mot fautif, mot correct, « Tout remplacer » |
| Ajouter un texte | « Texte », cliquer à l'endroit voulu, saisir, « Ajouter » |
| Insérer une image ou une signature | « Image », choisir le fichier, cliquer sur la page, ajuster le cadre, « Insérer » |
| Signer à la main | ✒️ dans la barre, signer à la souris ou au doigt, « Placer sur la page » |
| Naviguer et réordonner | vignettes dans le menu latéral : clic pour aller à la page, glisser pour déplacer |
| Remplir un formulaire | menu → « Formulaire », saisir les champs, « Renseigner le formulaire » |
| Effacer ou noircir une zone | « Caviarder », tracer un rectangle, puis « Effacer » ou « Noircir » |
| Surligner | « Surligner », puis cliquer un texte |
| Regarder de près | 🔍 active la loupe ; la molette règle le grossissement (1,5× à 6×) |
| Faire relire par un LLM | « Assistant » → fournisseur, clé API, consigne, « Analyser », puis cocher les corrections retenues |
| Gérer les pages | au survol d'une page : pivoter, monter, descendre, supprimer |
| Fusionner / extraire | « Fusionner » ajoute un PDF à la fin ; « Pages » extrait une sélection (`1-3, 5, 8-`) |
| Alléger le fichier | « Compresser » |
| Annuler / rétablir | `Ctrl+Z` / `Ctrl+Maj+Z` |
| Récupérer le résultat | « Télécharger » (`Ctrl+S`) |

**Les PDF scannés sont signalés.** Un scan ne contient pas de texte mais des images :
il n'y a alors rien à corriger, et cliquer sur la page ne peut rien faire. L'app le
détecte à l'ouverture, l'annonce dans un bandeau et marque les pages concernées, au lieu
de laisser chercher pourquoi rien ne se passe. Le reste demeure utilisable : surligner,
caviarder, ajouter du texte, signer, gérer les pages.

« Caviarder » retire réellement le contenu du PDF — texte supprimé, pixels des images
effacés dans la zone — et non un simple rectangle posé par-dessus, que n'importe quel
lecteur permettrait de contourner. `Échap` quitte le mode en cours.

Le remplacement global respecte la casse d'origine : `ortografe → orthographe`
transforme aussi `Ortografe` en `Orthographe`.

## Assistance par LLM (facultative)

Un modèle peut relire le texte et **proposer** des corrections. Il n'a aucun accès direct
au PDF : il reçoit des fragments de texte, renvoie des remplacements, et l'application
passe ensuite par la même route d'édition que le reste de l'app — donc par la pile
d'annulation. Rien n'est modifié sans que vous ayez coché la proposition.

```bash
pip install -r requirements-llm.txt      # OpenAI, Gemini et Anthropic
```

Chaque fournisseur est indépendant : n'installez que celui qui vous intéresse
(`langchain-openai`, `langchain-google-genai` ou `langchain-anthropic`). Le panneau
« Assistant » grise ceux dont le paquet est absent. Le champ « Modèle » est libre : si un
nom de modèle a changé depuis, saisissez-le sans attendre une mise à jour de l'app.

À savoir avant d'activer :

- **Le texte des pages analysées est envoyé au fournisseur choisi.** Le fichier lui-même
  ne quitte pas la machine, mais son contenu textuel, si. C'est le seul endroit de l'app
  où quelque chose sort, et il faut une action explicite pour cela.
- **La clé API n'est jamais stockée côté serveur** : elle accompagne la requête d'analyse
  et n'est ni journalisée ni conservée. Le navigateur la garde dans le `sessionStorage` de
  l'onglet, effacé à sa fermeture.
- Sur un document long, l'analyse est découpée en lots. Au-delà de douze, elle s'arrête et
  le signale plutôt que de laisser croire que tout a été relu — analysez alors page par page.

LangChain ne sert qu'à une chose ici, mais elle compte : une interface unique pour les
trois fournisseurs et une sortie structurée validée, sans écrire un adaptateur par API.

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

### Quand le texte ne rentre plus

Si le texte saisi dépasse la place disponible, le **paragraphe entier est recomposé** :
ses retours à la ligne sont recalculés, et il gagne une ligne s'il y a de la place en
dessous. À défaut, l'interligne est resserré de 10 % au maximum.

Recomposer est refusé — et la taille de police est alors réduite, jusqu'à 75 %, comme
avant — dès que l'opération risquerait de perdre quelque chose :

- **plusieurs styles** dans le paragraphe : un mot en gras serait aplati ;
- une **césure** en fin de ligne : impossible de savoir si `bien-` + `être` formait un mot
  coupé ou un mot composé ;
- des **indices de rupture interne** — interligne élargi, ligne en alinéa, ligne courte en
  plein milieu. Un « bloc » PyMuPDF n'est pas toujours un paragraphe : deux paragraphes
  rapprochés peuvent y être regroupés, et les recomposer ensemble les fondrait en un seul ;
- **pas assez de place** sous le paragraphe, ou deux modifications dans le même paragraphe.

La recomposition est décidée entièrement avant le moindre caviardage : effacer le
paragraphe pour découvrir ensuite qu'il ne se recompose pas perdrait ses autres lignes.

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
