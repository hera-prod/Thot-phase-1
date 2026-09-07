# Sommaire des annexes

Les annexes rassemblent les **faits techniques sourcés** qui étayent les documents de phase, les [contrats transversaux](../contrats_transversaux.md) et le [contrat de portabilité](../portabilite.md). Elles n'arbitrent pas le produit : en cas de contradiction, l'ordre de vérité de [C01](../contrats_transversaux.md) s'applique.

**Date de vérification de toutes les sources citées : 2026-09-06.** Toute page citée a été récupérée à cette date. Les annexes doivent être revérifiées à chaque changement de version de modèle, de runtime ou de bibliothèque.

## Convention de lecture

Chaque fiche distingue explicitement trois natures d'énoncé :

| Marque | Signification |
| :--- | :--- |
| **Fait** | Affirmation présente dans la source liée immédiatement à côté, récupérée le 2026-09-06. |
| **Décision Thot** | Choix de conception du projet, déduit des faits mais non affirmé par une source. |
| **À mesurer** | Grandeur non documentée par une source primaire ; elle doit être obtenue par test sur les profils cibles avant toute annonce. `n.a.` marque une valeur non vérifiée. |

Aucune fiche ne contient de commande d'implémentation ni de code applicatif : les annexes justifient, elles n'exécutent pas.

## Liste des fiches

| Fiche | Thème | Citée par |
| :--- | :--- | :--- |
| [01 — LFM2.5 et architecture du modèle texte](01_lfm_et_architecture.md) | Couches conv/GQA, contexte, licence, runtimes | Phase 1 |
| [02 — GGUF, quantification et pile multimodale](02_quantification_gguf.md) | Tailles réelles, types de quantification, mtmd, GLM-OCR | Phase 1 |
| [03 — FLUX.2 klein et génération d'images](03_flux_klein_generation.md) | Encodeur Qwen3, VAE, distillation, licences, VRAM | Phase 2 |
| [04 — Audio embarqué STT/TTS](04_audio_embarque.md) | whisper.cpp, Moonshine, Piper, Kokoro, français | Phase 3 |
| [05 — Génération et lecture de documents](05_generation_documents_python.md) | Injection CSV, reportlab/fpdf2/PyMuPDF, extraction PDF | Phase 3 |
| [06 — Pratiques Python : sidecar, IPC, persistance](06_bonnes_pratiques_python.md) | asyncio subprocess, PyInstaller, SQLAlchemy/SQLite | Toutes |
| [07 — Agents, recherche, mémoire et sécurité](07_recherche_agents_memoire.md) | RAG, ReAct, Generative Agents, RFC 9111, OWASP LLM01 | Phases 4–5 |
| [08 — Portabilité et distribution](08_portabilite_et_distribution.md) | Tauri/WebView2, AppImage, macOS, SQLite sur clé, FAT32 | Phase 1 et toutes |

## Corrections apportées à la révision précédente

Les fiches ont été réécrites pour supprimer des affirmations non soutenues par une source primaire : « absence de cache KV » et « attention linéaire » pour LFM2.5 (fiche 01), encodeur « CLIP » pour FLUX.2 (fiche 03), empreintes audio globales et voix françaises supposées (fiche 04), quantifications et handlers présentés comme testés (fiches 02 et 06), références agentiques remplacées par les papiers réels (fiche 07). Le détail des lacunes assumées figure dans chaque fiche sous forme de `n.a.` ou d'entrée **À mesurer**.
