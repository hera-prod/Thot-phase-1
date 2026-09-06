# Annexe 02 — Quantification et GGUF

Cette fiche décrit le format **GGUF** et la **quantification**, mécanismes qui permettent de faire tenir des modèles de plusieurs milliards de paramètres dans 8 Go de VRAM.

---

## 1. Pourquoi quantifier ?

Un modèle en précision 16 bits (2 octets/paramètre) coûte ~2 Go par milliard de paramètres. Un 8B ≈ 16 Go : intenable en 8 Go. La quantification réduit le nombre de bits par poids :

| Précision | Bits/poids | 8B ≈ | Qualité relative |
| :--- | :--- | :--- | :--- |
| FP16 | 16 | ~16 Go | référence |
| Q8_0 | 8 | ~8 Go | quasi identique |
| Q4_K_M | ~4.5 (mixte) | ~5 Go | bon compromis |
| Q2_K | ~2.6 | ~3 Go | dégradation notable |

> Le **Q4_K_M** est le compromis usuel pour un 8B sur GPU 8 Go (bonne qualité, empreinte réduite).

## 2. Le format GGUF

GGUF est le format de fichiers unique utilisé par `llama.cpp` : il emballe les poids quantifiés **et** les métadonnées (architecture, vocabulaire, config) dans un seul fichier. C'est ce qui rend le chargement simple et portable.

- Chargement via `llama-cpp-python` (bindings Python de `llama.cpp`).
- Supporte l'offload partiel sur GPU (`n_gpu_layers`), le `mmap`, le `mlock`.

## 3. Les « k-quants »

Les k-quants (Q4_K, Q5_K, Q6_K, etc.) sont une famille de quantification par blocs introduite dans `llama.cpp`. Le suffixe `_M` (moyen) et `_S` (petit) indique des variantes de même précision avec un trade-off taille/qualité légèrement différent.

## 4. Éléments à surveiller dans Thot

- **Modèles présents (phase 1)** : LFM2.5-8B-A1B en Q4_K_M (~5 Go), LFM2.5-VL-3B en Q4_K_XL, GLM-OCR en f16.
- **Vision** : les modèles multimodaux (VL) requièrent souvent un réseau de projection **mmproj** assorti au GGUF, à charger avec `mmproj=` ; sans lui, l'image est traitée comme du texte (explosion du nombre de jetons).
- **Quantification de la diffusion** (phase 2) : FLUX.2 Klein, sa VAE et son CLIP sont également quantifiés pour tenir en mémoire (voir [Annexe 03](03_flux_klein_generation.md)).

---

## Sources

- Documentation officielle de quantification de `llama.cpp` : https://github.com/ggml-org/llama.cpp/blob/master/tools/quantize/README.md
- Dépôt `llama.cpp` : https://github.com/ggml-org/llama.cpp
- Évaluation de la quantification (« Which Quantization Should I Use? », arXiv) : https://arxiv.org/html/2601.14277v1