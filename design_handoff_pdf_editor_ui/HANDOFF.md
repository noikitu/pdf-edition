# Handoff : refonte visuelle de l'Éditeur PDF

## Contexte
Ce dossier contient les mockups HTML de la nouvelle interface de l'app
`noikitu/pdf-edition` (FastAPI + JS/CSS vanilla, `app/static/`). Les fichiers
HTML sont des **références de design**, pas du code à copier tel quel : le
travail consiste à **recréer ce design dans le code existant** de l'app —
`app/static/index.html` et `app/static/style.css` — en gardant intacte toute
la logique JS (`app/static/app.js`) qui gère l'édition, l'undo/redo, le
zoom, etc.

## Fidélité
**Haute fidélité (hifi)** : couleurs, typographie, espacements et coins
arrondis exacts sont donnés ci-dessous. Reproduire pixel pour pixel avec le
CSS existant de l'app (`style.css`), pas une nouvelle stack.

## Direction visuelle : "liquid glass" été
- **Palette** : citron `#FFFF7D`, blanc `#FFFFFF` (dominant), menthe `#E8FFEA`,
  cyan pâle `#E0FCFF` (utilisés en glows radiaux discrets), encre `#1A1A1A`.
- **Fond de page** : dégradés radiaux très doux (citron en haut-gauche, menthe
  en haut-droite, cyan en bas) sur un blanc cassé `#FBFDF9`.
- **Verre dépoli ("glass")** : panneaux translucides avec
  `backdrop-filter: blur(20px) saturate(1.5)`, fond `rgba(255,255,255,.5)`,
  bordure `1px solid rgba(255,255,255,.75)`, ombre interne haute
  (`inset 0 1px 0 rgba(255,255,255,.9)`) + ombre externe douce pour donner du
  relief. Utilisé pour : barre d'outils, carte de dépôt de fichier, barre de
  police flottante.
- **Boutons citron** (CTA principal, ex. "Choisir un PDF", "Télécharger") :
  dégradé citron `linear-gradient(180deg, rgba(255,255,160,.95), rgba(255,255,125,.85))`
  + même traitement verre (bordure claire, ombre interne haute, ombre externe
  teintée jaune).
- **Typographie** : sans-serif uniquement — `'Helvetica Neue', Helvetica,
  Arial, sans-serif` pour tout le texte (y compris le document PDF affiché,
  qui n'utilise plus de police serif).
- **Rayons** : généreux — 26–28px sur les grandes cartes, 8–14px sur les
  boutons/pastilles, pill (999px) pour les tags de statut.
- **Ombres** : douces et superposées (`0 30px 70px rgba(0,0,0,.14)` sur les
  cartes), jamais dures.

## Écrans

### 1. Accueil / dépôt de fichier (`#dropzone`)
- Barre du haut (`.toolbar` dans le code existant) : logo citron 30×30 en
  verre, wordmark "Éditeur PDF" en 600/18px, à droite un texte muet "Aucun
  document ouvert" en gris `#7A7A72`.
- Carte de dépôt centrée, effet verre, rayon 28px, padding 60×68px :
  icône de document stylisée (papier blanc avec coin plié, ombre douce),
  titre H1 700/40px "Modifiez le texte de votre PDF", paragraphe gris
  `#5F5F58`, bouton citron "Choisir un PDF" (rayon 14px), puis 3 lignes
  d'astuces avec coche noire.

### 2. Édition d'un fragment (`.viewer` + `.tf.editing` + `.stylebar`)
- Barre d'outils complète en verre : undo/redo, zoom −/125%/+, boutons
  "＋ Texte" / "Remplacer" en verre, à droite nom de fichier, badge
  "3 modifications" (fond menthe translucide, texte vert `#1A6B3A`), bouton
  citron "↓ Télécharger", bouton fermer.
- Page du document : fond `#FFFFFF`, ombre `0 20px 50px rgba(0,0,0,.14)`,
  rayon 10px, padding 64×72×56px, police Helvetica Neue partout.
- Fragment en cours d'édition : contour jaune `box-shadow: 0 0 0 2px #FFE900`
  + lueur jaune douce, curseur clignotant simulé par une barre verticale.
- Barre de police flottante au-dessus du fragment (`.stylebar`) : verre,
  rayon 12px, contient le sélecteur de police ("Arial ▾") + boutons G / I.
- Highlights de remplacement dans le texte : vert menthe translucide pour un
  remplacement validé, jaune translucide pour une zone en attente.

## Design tokens

| Token | Valeur |
| --- | --- |
| Citron | `#FFFF7D` |
| Blanc (dominant) | `#FFFFFF` |
| Menthe | `#E8FFEA` |
| Cyan pâle | `#E0FCFF` |
| Encre (texte) | `#1A1A1A` |
| Gris texte secondaire | `#5F5F58` / `#7A7A72` |
| Police | `'Helvetica Neue', Helvetica, Arial, sans-serif` |
| Rayon carte | 26–28px |
| Rayon bouton | 8–14px |
| Rayon pill | 999px |
| Ombre carte | `0 30px 70px rgba(0,0,0,.14)` |
| Verre — fond | `rgba(255,255,255,.5)` |
| Verre — blur | `blur(20-22px) saturate(1.5)` |
| Verre — bordure | `1px solid rgba(255,255,255,.75)` |

## Fichiers
- `PDF Editor Mockups.dc.html` — le mockup complet (2 écrans côte à côte),
  à ouvrir dans un navigateur pour inspecter les styles inline exacts.

## Instructions pour Claude Code
1. Ouvrir `PDF Editor Mockups.dc.html` et `app/static/style.css` /
   `app/static/index.html` côte à côte.
2. Reporter les tokens ci-dessus dans les variables CSS `:root` de
   `style.css` (remplacer `--bg`, `--panel`, `--accent`, etc.).
3. Ajouter le `backdrop-filter` sur `.toolbar`, `.panel`, `.stylebar`,
   `.dz-inner` et le bouton `.primary` pour l'effet verre.
4. Ne pas toucher à `app.js` — la logique (édition, undo/redo, zoom,
   remplacement) reste identique, seul l'habillage visuel change.
</content>
