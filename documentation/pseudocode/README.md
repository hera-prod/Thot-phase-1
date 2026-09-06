# Pseudo-code — sommaire et conventions

Ce dossier contient le **pseudo-code annoté** du projet. Il sert de **plan de travail** avant l'écriture du vrai code : il décrit la structure, les responsabilités et les interactions, sans être exécutable.

## Conventions de lecture

- Chaque bloc précède/traîne des **notes** en commentaires, expliquant *pourquoi* le code existe, *par quoi* il est appelé, et ce qu'il ne faut **pas confondre**.
- La marque `[Phase N]` indique à **quelle phase** l'élément apparaît (et donc quand l'implémenter).
- `⚠️` signale un **piège** (risque de confusion, cas limite).
- `➜` indique une **relation** (« appelé depuis… », « produit pour… »).
- Le pseudo-code est volontairement **simplifié** : pas de gestion d'erreur exhaustive, mais les points critiques y sont marqués.

## Sommaire

| Fichier | Contenu | Phases |
| :--- | :--- | :--- |
| [backend_socle.md](backend_socle.md) | IPC JSON Lines, `main`, config, chemins, `ModelManager`, détection matérielle | 1 |
| [backend_agent_loop.md](backend_agent_loop.md) | Boucle agentique, double historique, sanitizer, préparation des outils, compaction | 1 |
| [backend_outils.md](backend_outils.md) | `ToolRegistry`, outil de base, vision, OCR, get_source, read_text | 1 |
| [backend_db.md](backend_db.md) | Modèles SQLAlchemy, stockage média, évolutions du schéma | 1 → 5 |
| [backend_images.md](backend_images.md) | Génération d'images FLUX.2 Klein, galerie, édition | 2 |
| [backend_audio_documents.md](backend_audio_documents.md) | Génération CSV/PDF/Markdown + STT/TTS sur CPU | 3 |
| [backend_recherche.md](backend_recherche.md) | Cascade de recherche, fiches de fichiers, cache web, deep research | 4 |
| [backend_espaces.md](backend_espaces.md) | Espaces de travail, mémoires, référencement par score | 5 |
| [frontend.md](frontend.md) | Navigation, pages, IPC TypeScript, composants, galerie et espaces | 1 → 5 |

## Règle d'or

Le pseudo-code **décrit l'architecture cible** telle qu'elle doit être, phase après phase. Quand un élément de phase N réutilise un élément de phase antérieure, il est marqué `[réutilise Phase N-x]`.

> Le vrai code de la phase 1 existe déjà dans `backend/`, `frontend/`, `src-tauri/`. Ce pseudo-code est la **référence de cohérence** : relis un bloc ici avant d'écrire le code correspondant, pour vérifier que l'implémentation ne dérive pas de l'architecture prévue.