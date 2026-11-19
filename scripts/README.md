# Scripts — outils produit et diagnostics

Ce dossier rassemble deux natures de scripts, qu'il faut distinguer : les **outils produit**, qui font partie du fonctionnement livré de l'application, et les **scripts de diagnostic**, instruments de développement conservés pour mesurer et contrôler le moteur d'image. L'assemblage de production (voir `electron-builder.yml`) n'embarque que `electron/` et `frontend/dist/` : rien de ce dossier ne se retrouve dans l'AppImage.

## Outils produit

- `scripts/start-electron.mjs` — lanceur unique de l'application. `npm start` sert le bundle local (protocole `app://`, sans port ni Vite) et `npm run dev` démarre Vite puis Electron avec rechargement à chaud. Un seul point d'entrée, deux modes : quand Electron se ferme, tout ce qu'il a démarré se ferme avec lui.
- `scripts/dev-preflight.mjs` — contrôles de prérequis du mode développement (`npm run dev:preflight`) : remet la session dans un état cohérent avant lancement (port 1420 libre, Vite orphelin d'une session précédente signalé). La règle est de ne toucher qu'à un processus dont la lignée est reconnue comme appartenant à ce dépôt ; tout autre occupant du port est signalé, jamais tué.

## Diagnostics du moteur image

Les scripts `diag_*.py` sont des outils de développement du moteur d'image (backend/image_engine/) : génération text-to-image, inpaint, outpaint, comparaisons de références, cas d'usage par défaut. Ils se lancent **depuis la racine du dépôt** (par exemple `python3 scripts/diag_t2i_refs.py`) et utilisent `scripts/assets/` comme jeux d'images de référence.

- `diag_t2i_refs.py` — text-to-image avec images de référence.
- `diag_inpaint_real.py` — inpaint sur cas réel.
- `diag_i1_real.py` — diagnostic inpaint (variante 1).
- `diag_o1_real.py`, `diag_o2_real.py`, `diag_o2_noref.py` — diagnostics outpaint, avec et sans référence.
- `diag_outpaint_refs.py` — outpaint avec références.
- `diag_ref_ab.py` — comparaison A/B de références.
- `diag_user_case_default.py` — cas d'usage utilisateur par défaut.

`scripts/assets/` contient les images de référence employées par ces diagnostics (`cafe-1.png`, `personne-1.png`). Ces deux PNG sont des jeux de test de développement : ils ne sont pas des assets de l'interface, qui vivent dans `frontend/public/assets/`.

## Mesures et contrôles de captures

- `scripts/v4_metrics.py` — mesures quantitatives sur les captures (qualité, tailles, ratios).
- `scripts/check_captures_v4.py` — contrôles de conformité des captures produites.

## Scénario de bout en bout

- `scripts/e2e_web_when_gpu_free.sh` — scénario de bout en bout du parcours complet. Comme son nom l'indique, il attend qu'un GPU soit libre avant de s'exécuter : c'est un test lourd, à réserver aux machines dédiées, pas à une exécution systématique.

## Portée

Les scripts de diagnostic, de mesure et de scénario **ne font pas partie du produit livré**. Ils sont conservés dans le dépôt parce qu'ils documentent le raisonnement de développement (ce qui a été mesuré, sur quelles images, avec quels résultats) et permettent de rejouer ces mesures ; ils dépendent d'un GPU et de poids de modèles absents du dépôt. Seuls `start-electron.mjs` et `dev-preflight.mjs` participent au fonctionnement de l'application.
