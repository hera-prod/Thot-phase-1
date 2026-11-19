---
license: mit
---

# Thot Phase 1

Thot Phase 1 est un assistant conversationnel local (chat, pièces jointes, historique, preuves) ciblant un usage sur matériel limité (budget de 8 Go VRAM), distribué sous la forme d'un dossier portable contenant le code source. L'application est bâtie sur Electron (processus principal + interface React/TypeScript) et un sidecar Python qui porte tout le moteur : inférence, recherche, ingestion, moteur d'image.

## Licence

Le code de Thot Phase 1 est distribué sous licence MIT. Voir le fichier `LICENSE` à la racine. Copyright (c) 2026 Raclin Théo (Héra).

Les poids de modèles ne sont pas inclus. Le modèle FLUX.2 Klein est soumis à une licence non commerciale (FLUX.2 [klein] Non-Commercial License) ; vérifier la licence exacte avant toute redistribution des poids. Les autres modèles conservent leurs licences respectives, détaillées dans `licenses/README.md`. Les composants tiers du code (bibliothèques Python et Node, police d'interface) conservent également leurs licences d'origine : voir `THIRD_PARTY_NOTICES.md`.

## Prérequis

- Python >= 3.11
- Node.js >= 22.12 et npm (Electron verrouille cette version minimale)
- Un environnement graphique (X11/Wayland sur Linux, bureau macOS ou Windows) pour Electron

Les dépendances Python et Node s'installent automatiquement au premier lancement, via les lanceurs ci-dessous.

> Sur Linux, Electron (Chromium) requiert un sandbox opérationnel ; cela dépend de l'environnement. La désactivation du sandbox (`--no-sandbox`) n'est pas recommandée comme solution publique.

## Démarrage rapide

Les lanceurs utilisent des chemins relatifs, créent un environnement virtuel Python (`.venv`) au premier lancement, installent les dépendances Python et Node, puis démarrent l'application.

- **Linux** : `./lancer-linux.sh`
- **macOS** : double-clic sur `lancer-macos.command`, ou `./lancer-macos.command` dans un terminal
- **Windows** : double-clic sur `lancer-windows.bat`

## Hors ligne

L'application est conçue pour fonctionner sans aucun accès réseau. Au premier rendu, l'interface n'émet aucun appel sortant : la police d'interface (Manrope) est embarquée localement dans `frontend/public/fonts/`, et `frontend/index.html` ne référence aucune URL distante. Les modèles sont lus localement dans `models/`, jamais depuis le Hub. La seule sortie réseau du produit est l'outil `web_research`, déclenché explicitement en conversation — et que le mode `local_only` désactive entièrement.

## Structure du dossier

```text
Thot-Phase-1/
├── backend/                    # Moteur Python (sidecar JSON Lines)
│   ├── config/                 # Chemins, réglages, plafonds
│   ├── db/                     # Modèles SQLAlchemy, migrations, tables FTS5
│   ├── image_engine/           # Moteur d'image FLUX.2 Klein (T2I, inpaint…)
│   ├── inference/              # Boucle d'orchestration, manager VRAM, politique média
│   ├── ingestion/              # PDF, tableurs, images, texte
│   ├── ipc/                    # Protocole stdin/stdout (emit/seq)
│   ├── prompts/                # Prompts système (orchestrateur, générateur, vision…)
│   ├── search/                 # Recherche FTS5 + ColBERT, fusion RRF
│   ├── services/               # Services métier (image, web, dépôt…)
│   ├── tools/                  # Registre des outils
│   ├── main.py                 # Point d'entrée du sidecar
│   └── check_models.py         # Contrôle de présence des poids
├── electron/                   # Processus principal Electron (main.cjs, preload.cjs…)
├── frontend/                   # Interface React/TypeScript (Vite)
│   ├── public/fonts/           # Police Manrope embarquée localement (licence OFL)
│   └── public/assets/          # Marque et états vides
├── scripts/                    # Outils produit (Electron, preflight) et diagnostics
│   └── assets/                 # Images de référence pour les diagnostics
├── tests/                      # Tests pytest, Electron et frontend (+ manuels)
├── licenses/                   # Index des licences des modèles
├── models/                     # Emplacement des poids (non inclus, seul le README)
├── documentation/              # PDF de présentation (Thot_phase_1.pdf)
├── lancer-linux.sh             # Lanceur Linux
├── lancer-macos.command        # Lanceur macOS
├── lancer-windows.bat          # Lanceur Windows
├── sidecar.py                  # Amorçage du sidecar Python
├── package.json                # Configuration Node (racine)
├── package-lock.json           # Verrouillage des dépendances Node
├── electron-builder.yml        # Configuration de build Electron (AppImage x64)
├── pyproject.toml              # Configuration Python (pytest, PyInstaller)
├── pyrightconfig.json          # Configuration de l'analyse statique
├── requirements.txt            # Dépendances Python
├── portable.json               # Active le mode portable (données dans ./data)
├── distribution.json           # Métadonnées de distribution
├── contrat_modes_image.py      # Contrat des modes du moteur image
├── reference_inpainting_klein.py  # Référence d'inpainting FLUX.2 Klein
├── ETAT_DU_DEPOT.txt           # Document de travail (état du dépôt)
├── THIRD_PARTY_NOTICES.md      # Composants tiers et leurs licences
├── LICENSE                     # Licence MIT
├── .gitattributes              # Règles Git (LFS pour les PDF)
└── .gitignore                  # Exclusions (poids, données, artefacts)
```

## Modèles

Les poids de modèles ne sont pas inclus dans ce dépôt. Ils doivent être placés dans le dossier `models/`. Consulter `models/README.md` pour la liste attendue, et `licenses/README.md` pour les conditions de licence.

Sans modèles, l'interface démarre mais l'inférence (chat, génération d'image, OCR, vision) est indisponible.

## Données

Les données locales ne sont pas incluses dans ce dépôt. Le stockage s'effectue dans le dossier `data/` à la racine (lorsque `portable.json` est présent) ou dans le dossier de données XDG (`~/.local/share/thot/data` par défaut) selon la configuration.

## Documentation PDF

Le PDF de présentation de Thot Phase 1 est fourni dans `documentation/` (`Thot_phase_1.pdf`, 38 pages). Il est conservé volontairement dans le dépôt : c'est la présentation du produit, lisible indépendamment du code. Le présent README couvre pour sa part le lancement et l'organisation du dépôt.

## Tests

- Backend Python : `python3 -m pytest tests/backend/` (les tests marqués `slow` sont exclus par défaut, `addopts` dans `pyproject.toml` ; les tests manuels, qui demandent un GPU, sont rangés dans `tests/backend/manual/`)
- Electron : `npm run test:electron`
- Frontend : `npm --prefix frontend run test`

## Licences des composants tiers

Le code de Thot est sous licence MIT, mais il s'appuie sur des composants tiers qui conservent leurs licences propres, dont certaines copyleft — PyMuPDF, par exemple, est distribué sous AGPL-3.0 ou sous licence commerciale Artifex. La liste complète (composant, licence, usage, liens) figure dans `THIRD_PARTY_NOTICES.md` ; les licences des poids de modèles sont détaillées dans `licenses/README.md`. En cas de redistribution de binaires, chaque licence doit être relue individuellement.

## Dépôt HuggingFace

Ce dossier est prêt à être poussé sur le Hub HuggingFace. Aucun fichier ne dépasse 100 Mo : les poids de modèles et les données locales sont absents du dépôt. Seul `documentation/Thot_phase_1.pdf` (14,26 Mo) dépasse la recommandation de 10 Mo du Hub : Git LFS est donc recommandé pour ce fichier, et le `.gitattributes` de la racine déclare déjà les `*.pdf` sous LFS.
