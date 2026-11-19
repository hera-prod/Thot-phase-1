# Notices tiers — Thot Phase 1

Le code de l'application Thot Phase 1 est sous licence **MIT** (Copyright (c) 2026 Raclin Théo (Héra),
voir `LICENSE` à la racine). Cette licence MIT **ne couvre pas** les composants tiers listés ci-dessous,
conservés sous leur licence d'origine. Leur inclusion dans le bundle ne transfère aucun droit
supplémentaire sur ces composants. Les poids de modèles ne sont **pas distribués** avec ce dépôt
(voir `licenses/README.md`) ; les composants listés ici sont ceux que le code embarque ou appelle.

## 1. PyMuPDF (fitz)

- Rôle : lecture/extraction de PDF (`backend/ingestion/`, gate).
- Licence : **AGPL-3.0** ou licence commerciale **Artifex**.
- Site : https://pymupdf.readthedocs.io/
- Code source : https://github.com/pymupdf/PyMuPDF
- Si vous redistribuez le binaire Thot, vous êtes soumis aux termes de l'AGPL-3.0 pour PyMuPDF
  (ou devez tenir une licence commerciale Artifex séparée).
- **AVERTISSEMENT** : si un binaire PyInstaller embarquant PyMuPDF est un jour distribué,
  une revue juridique est nécessaire avant publication. PyMuPDF est la dépendance la plus
  contraignante du projet ; elle conditionne les termes de toute redistribution.

## 2. odfpy

- Rôle : lecture des classeurs/tableurs `.ods` (moteur `.ods` de pandas, `backend/ingestion/sheets.py`).
- Licence : **GPL / LGPL**.
- Site : https://odfpy.readthedocs.io/
- Code source : https://github.com/documentfreedom/odfpy

## 3. PyInstaller

- Rôle : packaging du sidecar Python en binaire autonome (`pyinstaller --onedir`, `pyproject.toml`).
- Licence : **GPL-2.0-or-later avec exception** de liaison runtime (exception permettant de lier le
  binaire généré sans le placer sous la GPL, sous conditions).
- Site : https://pyinstaller.org/
- Code source : https://github.com/pyinstaller/pyinstaller

## 4. Manrope (police)

- Rôle : police d'interface, **embarquée localement** dans `frontend/public/fonts/`
  (`manrope-400.woff2`, `manrope-500.woff2`, `manrope-600.woff2`, `manrope-700.woff2`) ;
  le texte de licence `OFL.txt` est joint dans le même dossier.
- Licence : **SIL Open Font License 1.1 (OFL-1.1)**.
- Site : https://fonts.google.com/specimen/Manrope

## 5. Bibliothèques Python (runtime du sidecar)

Ces paquets sont déclarés dans `requirements.txt` et conservent leur licence d'origine :

| Composant | Licence | Usage dans le projet |
|---|---|---|
| llama-cpp-python | MIT | Inférence LLM/VL (GGUF) |
| torch / torchvision | BSD-3-Clause | Tenseurs, moteur image |
| diffusers | Apache-2.0 | Pipeline FLUX.2 Klein (inpaint notamment) |
| transformers | Apache-2.0 | Tokenizers, ColBERT, PaddleOCR-VL |
| huggingface_hub | Apache-2.0 | Chargement `from_pretrained` (lecture locale) |
| SQLAlchemy | MIT | ORM, 25 tables métier |
| pandas | BSD-3-Clause | Repli tableurs (xlsx/ods) |
| openpyxl | MIT | Lecture `.xlsx` |
| defusedxml | PSF | XML durci (xlsx/ods) |
| jinja2 | BSD-3-Clause | Rendu de gabarits |
| psutil | BSD-3-Clause | Mesures mémoire/processus |
| gguf | MIT | Artefacts GGUF |
| safetensors | Apache-2.0 | Poids safetensors |
| tokenizers | Apache-2.0 | Tokenisation |
| pillow | MIT-CMU | Traitement d'images |

## 6. Composants Node (Electron, frontend)

Le processus principal repose sur **electron** (MIT) et **electron-builder** (MIT) ; l'interface
repose sur **react** / **react-dom** (MIT), **vite** (MIT), **typescript** (Apache-2.0), ainsi que
les outils de build associés (**esbuild**, **rollup**, plugins Vite/Tailwind, bibliothèques de test).
Ces composants sont résolus par npm : la licence exacte de chaque paquet figure dans
`node_modules/*/package.json` et dans `frontend/package-lock.json`, que l'on consultera avant
toute redistribution.

## 7. Poids de modèles (non distribués)

Les poids des modèles (LFM2.5-8B, LFM2.5-VL-3B, PaddleOCR-VL-1.6, PP-DocLayoutV3,
FLUX.2 Klein, Qwen3-8B, LFM2.5-ColBERT-350M) ne sont pas inclus dans le dépôt (`models/`
git-ignoré, `models_included=false`). Chacun est régi par la licence de son éditeur (voir
`licenses/README.md`). Ils ne sont pas couverts par la MIT du dépôt.

---

Aucune des licences ci-dessus n'est modifiée par la licence MIT du code de Thot. En cas de
redistribution de binaires, vérifier chaque licence individuellement — en commençant par
PyMuPDF (AGPL-3.0) et les licences non commerciales des modèles.
