# Sommaire des annexes

Ce dossier regroupe l'ensemble des **références scientifiques**, des **guides d'utilisation Python** et des **ressources** qui justifient les choix d'architecture du projet. Chaque fiche est citée depuis le [document central](../README.md) et les [documents de phase](../phase_1_socle_et_vision.md).

## Liste des fiches

| Fiche | Thème | Citée par |
| :--- | :--- | :--- |
| [01 — LFM et attention linéaire](01_lfm_et_architecture.md) | Architecture du modèle de langage, absence de cache KV | Phase 1 |
| [02 — Quantification GGUF](02_quantification_gguf.md) | Formats GGUF, precision Q4/Q8, llama.cpp | Phase 1 |
| [03 — FLUX.2 Klein et génération d'images](03_flux_klein_generation.md) | Diffusion quantifiée, VAE, CLIP | Phase 2 |
| [04 — Audio embarqué STT/TTS](04_audio_embarque.md) | Moonshine, whisper.cpp, Piper, Kokoro sur CPU | Phase 3 |
| [05 — Génération de documents en Python](05_generation_documents_python.md) | reportlab, fpdf2, csv, PyMuPDF | Phase 3 |
| [06 — Bonnes pratiques Python](06_bonnes_pratiques_python.md) | IPC, sidecar/PyInstaller, threads, SQLAlchemy | Toutes |
| [07 — Agents, recherche et mémoire](07_recherche_agents_memoire.md) | RAG, cascade de récupération, mémoires | Phases 4–5 |

## Types de contenu

- **Recherche scientifique** : papiers (arXiv), rapports techniques, annonces officielles (fiches 01, 03, 04, 07).
- **Guides d'utilisation Python** : documentations officielles des bibliothèques (fiches 05, 06).
- **Ressources** : dépôts GitHub, pages de documentation, blog techniques.

## Convention de citation

Chaque fiche comporte une section **« Sources »** listant les URL réelles (bien qu'ici reprises sans hyperliens dans le texte, elles sont données en fin de fiche). Les affirmations techniques sont reliées à leur source pour permettre une vérification indépendante.

> **Rappel** : ces fiches sont des références, pas des tutoriels exécutables. Elles servent à justifier le pseudo-code et les documents de phase.