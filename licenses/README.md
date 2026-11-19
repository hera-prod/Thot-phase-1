# Licences — Thot Phase 1

> Note : les renvois `docs/...`, `q-r.md` et à la « Bible » désignent la documentation interne du projet ; elle n'est pas incluse dans l'export HuggingFace.

## Modèles (poids NON inclus, `distribution.json: models_included=false`, `models/` git-ignoré)

- **LFM2.5-8B (Liquid AI)** — licence **LFM Open License v1.0** (licence de la famille
  LFM2.5). Fichiers : `models/LLM/*`. La LFM Open License v1.0 réserve l'usage commercial
  gratuit aux structures réalisant moins de 10 M$ de revenus annuels : relire les termes
  exacts sur la page officielle avant toute redistribution des poids.
- **LFM2.5-VL-3B (Liquid AI)** — même famille, licence **LFM Open License v1.0** à vérifier
  sur la page HuggingFace officielle de LiquidAI avant redistribution ; les points de
  distribution peuvent chacun porter leur propre texte de licence. Fichiers : `models/Vision-general/*`.
- **PaddleOCR-VL-1.6 (PaddlePaddle, Apache-2.0)** — `models/PaddleOCR-VL-1.6/`.
  Texte Apache-2.0 : https://www.apache.org/licenses/LICENSE-2.0
- **PP-DocLayoutV3 (PaddlePaddle, Apache-2.0)** — `models/PP-DocLayoutV3_safetensors/`.
  Prévu, pas encore branché au pipeline OCR.
- **FLUX.2 Klein 9B KV** (`models/flux2-klein/transformer/`) — **FLUX.2 [klein] Non-Commercial
  License** (référencée « FLUX Non-Commercial License v2.1 » dans `distribution.json`) ;
  restrictions non commerciales et non production.
- **Qwen3-8B** (`models/flux2-klein/text_encoder/`) — modèle d'origine
  **Apache-2.0**. [Page officielle du modèle](https://huggingface.co/Qwen/Qwen3-8B).
- **LFM2.5-ColBERT-350M (Liquid AI)** — `models/colbert/LFM2.5-ColBERT-350M/`.
  Modèle de recherche sémantique (ColBERT, late interaction, 128 dim/jeton, MaxSim),
  bâti sur le backbone **LFM2.5-350M** de Liquid AI. Licence **LFM Open License v1.0**
  (identifiant `lfm1.0`) — vérifier la licence exacte sur la page officielle du modèle
  avant redistribution.
  [Page officielle du modèle](https://huggingface.co/LiquidAI/LFM2.5-ColBERT-350M).
- **GGUF QuantStack** — les artefacts GGUF conservent les restrictions du modèle source ;
  la conversion et la quantification n'accordent aucun droit commercial supplémentaire.
- **VAE et tokenizer FLUX.2** (`models/flux2-klein/vae/` et
  `models/flux2-klein/tokenizer/`) — **Apache-2.0** selon la provenance des composants.

Ces poids ne sont pas inclus dans la distribution.

## Code Thot Phase 1

**MIT**, Copyright (c) 2026 Raclin Théo (Héra). Voir le fichier `LICENSE` à la racine du dépôt.

## Dépendances de code

Le code du dépôt est sous MIT, mais cette licence **ne couvre pas** les dépendances de code
sur lesquelles il s'appuie : chacune conserve sa licence d'origine. Les avis complets
(composant, licence, usage, liens) figurent dans `THIRD_PARTY_NOTICES.md`. Trois d'entre
elles appellent une attention particulière avant toute redistribution de binaires :

- **PyMuPDF** (fitz) — **AGPL-3.0** ou licence commerciale **Artifex**. C'est la contrainte
  la plus forte du lot : un binaire embarquant PyMuPDF est soumis aux termes de l'AGPL-3.0,
  sauf à tenir une licence commerciale Artifex.
- **odfpy** — **GPL / LGPL** (moteur `.ods` de pandas, utilisé par `backend/ingestion/sheets.py`).
- **PyInstaller** — **GPL-2.0-or-later avec exception** de liaison runtime, qui permet de
  distribuer le binaire généré sans le placer sous la GPL, sous conditions.

Les autres dépendances (llama-cpp-python, torch, diffusers, transformers, SQLAlchemy,
pandas, openpyxl, etc. côté Python ; Electron, React, Vite, TypeScript, etc. côté Node) sont
de licences permissives, listées dans `THIRD_PARTY_NOTICES.md`. La police d'interface
**Manrope** est embarquée sous **SIL Open Font License 1.1** dans `frontend/public/fonts/`.

## Provenance des mécanismes mémoire d'image

Le planner, la garde d'activation, le device GPU canonique, le tiling VAE et la récupération OOM de Thot sont des implémentations indépendantes basées sur les API natives PyTorch/Diffusers et les mesures du projet. Le dossier `docs/unsloth-main/studio/` est une **référence interne non incluse** dans la livraison (dossier absent du dépôt) ; aucun fichier, import ou code AGPL-3.0-only n'y est repris. Une revue juridique reste requise avant toute redistribution.
