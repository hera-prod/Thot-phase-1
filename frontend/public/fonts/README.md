# Polices — dossier `frontend/public/fonts`

Ce dossier héberge la police **Manrope** embarquée localement afin que l'application
ne dépende plus de Google Fonts.

## Fichiers

- `manrope-400.woff2`, `manrope-500.woff2`, `manrope-600.woff2`, `manrope-700.woff2`
  — quatre **instances statiques** de Manrope, un fichier par poids (400, 500, 600, 700).
  Ce ne sont pas des polices variables : aucun fichier ne porte de table `fvar` ni d'axe
  `wght` 200–800 ; chaque fichier ne couvre que sa propre graisse, servie telle quelle.
- `OFL.txt` — licence SIL Open Font License v1.1.
- `README.md` — ce fichier.

## Règles `@font-face`

Déclarées dans `frontend/src/styles/tokens.css` (une règle par graisse 400/500/600/700,
`font-family: "Manrope"`, `font-display: swap`,
`src: url("/fonts/manrope-<poids>.woff2") format("woff2")`).

## Provenance et licence

- Source : Google Fonts / dépôt git `googlefonts/manrope`.
- Police : Manrope, par The Manrope Project.
- Licence : **SIL Open Font License 1.1** (voir `OFL.txt`).
- Les fichiers woff2 ont été récupérés depuis l'API Google Fonts
  avec un User-Agent navigateur moderne.

L'application n'effectue plus aucune requête vers l'API Google Fonts :
les 3 lignes `<link>` (preconnect ×2 + feuille de style) ont
été retirées de `frontend/index.html`. La pile de repli système déclarée dans
`--thot-font-family` (`"Manrope", "Avenir Next", "Trebuchet MS", sans-serif`) est
conservée au cas où la police locale ne serait pas chargée.
