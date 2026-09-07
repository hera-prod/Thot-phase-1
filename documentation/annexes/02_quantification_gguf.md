# Annexe 02 — GGUF, quantification et pile multimodale

Sources récupérées le **2026-09-06**. Conventions **Fait / Décision Thot / À mesurer** : voir le [sommaire des annexes](README.md).

## Tailles réelles des GGUF de LFM2.5-8B-A1B

**Fait.** Tailles de fichiers publiées par l'API du dépôt ([API Hugging Face, blobs](https://huggingface.co/api/models/LiquidAI/LFM2.5-8B-A1B-GGUF?blobs=true)) :

| Quantification | Taille (octets) | Ordre de grandeur |
| :--- | ---: | :--- |
| Q4_0 | 4 844 678 368 | ≈ 4,84 Go |
| Q4_K_M | 5 155 564 768 | ≈ 5,16 Go |
| Q5_K_M | 6 030 339 296 | ≈ 6,03 Go |
| Q6_K | 6 959 787 232 | ≈ 6,96 Go |
| Q8_0 | 9 010 195 680 | ≈ 9,01 Go |
| F16 / BF16 | 16 947 260 640 | ≈ 16,95 Go |

**À mesurer.** Ce sont des tailles **sur disque**. L'empreinte à l'exécution ajoute contexte, buffers de calcul et état d'attention : aucune source récupérée ne publie ces valeurs (`n.a.`). Deux conséquences directes : le profil « moins de 8 Go » ne se déduit pas de la taille du fichier, et une quantification tenant sur disque ne tient pas nécessairement en mémoire.

## Types de quantification disponibles

**Fait.** L'outil de quantification de llama.cpp expose notamment les types **Q2_K à Q6_K**, **Q8_0**, la série **IQ1_S à IQ4_NL**, ainsi que F16/BF16/F32, et permet de fixer séparément le type des tenseurs de sortie et d'embeddings, voire tenseur par tenseur ; l'option de désactivation des mélanges « k-quant » existe également ([llama.cpp — tools/quantize/README.md](https://github.com/ggml-org/llama.cpp/blob/master/tools/quantize/README.md)).

**Fait.** La même page indique que la quantification réduit la précision des poids, **diminue la taille du modèle** et **peut accélérer l'inférence**, les méthodes différant en taille disque et en vitesse ([llama.cpp — tools/quantize/README.md](https://github.com/ggml-org/llama.cpp/blob/master/tools/quantize/README.md)).

**À mesurer.** Aucune valeur de perplexité, de qualité en français ou de vitesse n'est reprise ici : les tableaux comparatifs de la page portent sur un autre modèle et ne se transposent pas à LFM2.5. La révision précédente de cette fiche annonçait des choix de quantification « déjà testés » : affirmation retirée.

**Décision Thot.** Une quantification n'entre au manifeste de distribution qu'accompagnée de son checksum et d'une fiche de mesure (OS, arch, backend, contexte, débit, pic mémoire), conformément à [C19](../contrats_transversaux.md).

## Pile multimodale : mtmd, projecteurs, modèles OCR

**Fait.** Le support multimodal de llama.cpp passe par la bibliothèque **libmtmd** et exige, en plus du modèle, un fichier de **projecteur multimodal** fourni séparément ; une option permet de garder ce projecteur hors GPU ([llama.cpp — docs/multimodal.md](https://github.com/ggml-org/llama.cpp/blob/master/docs/multimodal.md)).

**Fait.** Cette page liste des modèles préquantifiés « prêts à l'emploi » et renvoie, pour plusieurs modèles OCR récents (dont **GLM-OCR**, PaddleOCR-VL, Deepseek-OCR, Dots.OCR, HunyuanOCR), à des **discussions** dédiées plutôt qu'à des artefacts officiels ([llama.cpp — docs/multimodal.md](https://github.com/ggml-org/llama.cpp/blob/master/docs/multimodal.md)).

**Fait.** `llama-cpp-python` fournit des handlers de vision nommés (llava 1.5/1.6, moondream2, nanollava, llama-3-vision-alpha, minicpm-v-2.6, qwen2.5-vl, gemma-4) **et un handler générique compatible mtmd** applicable à un GGUF disposant d'un projecteur mtmd et d'un gabarit de chat intégré ; la version publiée constatée est **0.3.35**, avec une mise à jour de llama.cpp vendored datée du 17 août 2026 ([README llama-cpp-python](https://github.com/abetlen/llama-cpp-python), [PyPI](https://pypi.org/project/llama-cpp-python/)).

**Fait.** LFM2.5-VL-3B est publié en GGUF par Liquid AI et son usage documenté côté llama.cpp comprend un plafond explicite de jetons image ([Docs Liquid AI — LFM2.5-VL-3B](https://docs.liquid.ai/lfm/models/lfm25-vl-3b)). La valeur par défaut de ce plafond est `n.a.`.

**Fait.** GLM-OCR est un modèle compact de **0,9 B** (encodeur visuel 0,4 B + décodeur 0,5 B) ([GLM-OCR Technical Report, arXiv:2603.10910](https://arxiv.org/abs/2603.10910)), sous licence **MIT** d'après les métadonnées du dépôt ([API Hugging Face](https://huggingface.co/api/models/zai-org/GLM-OCR)).

**Décision Thot.** La révision précédente affirmait des quantifications précises pour GLM-OCR et LFM2.5-VL (« f16 », « Q4_K_XL ») et l'existence d'un « CLIP quantifié » : ces mentions sont retirées, faute de source. La pile OCR/vision est donc décrite comme **une décision ouverte** ([decisions.md](../decisions.md)) avec deux chemins à qualifier :

1. binding Python via le handler mtmd générique, sur la version exactement embarquée ;
2. repli par binaire llama.cpp multimodal embarqué, si le binding échoue sur le couple (modèle, projecteur) retenu.

Conformément à [C07](../contrats_transversaux.md), aucun repli ne consiste à retirer le projecteur ni à substituer un handler d'une autre architecture.

**À mesurer** : disponibilité effective d'un GGUF + projecteur pour le modèle OCR retenu ; qualité OCR en français sur documents Thot ; pic mémoire du pipeline vision complet ; coût en jetons par page selon le plafond d'image.
