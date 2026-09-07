# Annexe 01 — LFM2.5 et architecture du modèle texte

Sources récupérées le **2026-09-06**. Conventions **Fait / Décision Thot / À mesurer** : voir le [sommaire des annexes](README.md).

## LFM2.5-8B-A1B : ce que disent les sources

| Élément | Fait | Source |
| :--- | :--- | :--- |
| Structure | **24 couches : 18 blocs de convolution « double-gated » + 6 blocs d'attention GQA** | [Carte modèle Hugging Face](https://huggingface.co/LiquidAI/LFM2.5-8B-A1B) |
| Formulation officielle de la famille | « gated short convolutions with a small number of grouped query attention blocks » | [LFM2 Technical Report, arXiv:2511.23404](https://arxiv.org/abs/2511.23404) |
| Paramètres | **8,3 B au total, 1,5 B actifs** (mélange d'experts) | [Carte modèle](https://huggingface.co/LiquidAI/LFM2.5-8B-A1B), [arXiv:2511.23404](https://arxiv.org/abs/2511.23404) |
| Contexte | **128 000 jetons**, vocabulaire 128 000 | [Carte modèle](https://huggingface.co/LiquidAI/LFM2.5-8B-A1B) |
| Contexte de la génération LFM2 | La famille LFM2 est décrite avec « all with 32K context length » | [arXiv:2511.23404](https://arxiv.org/abs/2511.23404) |
| Runtimes annoncés | llama.cpp/GGUF, Transformers ≥ 5.0.0, vLLM, SGLang, MLX, ONNX, LM Studio | [Carte modèle](https://huggingface.co/LiquidAI/LFM2.5-8B-A1B) |
| Décodage spéculatif | Un drafter séparé de **328 M** paramètres est annoncé pour un décodage « ~2,5× plus rapide » | [Carte modèle](https://huggingface.co/LiquidAI/LFM2.5-8B-A1B) |

## Points explicitement non documentés (`n.a.`)

- **Cache KV** : aucune des deux sources n'affirme que le modèle utilise, ou n'utilise pas, un cache clé/valeur. La révision précédente de cette fiche parlait d'« absence de cache KV » : cette affirmation est **retirée**, faute de source. La présence de 6 blocs GQA implique un état d'attention dont le coût mémoire est **à mesurer**.
- **Attention linéaire / NoPE** : ces termes n'apparaissent dans aucune source récupérée pour LFM2.5. Formulations retirées.
- **Nombre d'experts et nombre d'experts activés par jeton** : `n.a.` sur la carte modèle et dans le rapport technique. L'ancienne mention « 1 à 2 experts sur ~50 » est retirée.
- **Empreinte mémoire mesurée** en inférence : `n.a.` — aucune source ne publie de VRAM/RAM constatée.

## Licence

**Fait.** Le dépôt GGUF porte le tag de licence `lfm1.0` ([API Hugging Face](https://huggingface.co/api/models/LiquidAI/LFM2.5-8B-A1B-GGUF)), tandis que le dépôt de poids principal est tagué `license:other` ([API Hugging Face](https://huggingface.co/api/models/LiquidAI/LFM2.5-8B-A1B)).

**Fait.** La **LFM Open License v1.0** est présentée par Liquid AI comme fondée sur Apache 2.0, avec attribution requise, absence de clause copyleft, usage recherche/à but non lucratif non restreint, et des droits commerciaux qui **cessent au-delà de 10 000 000 USD de revenu annuel** ([Liquid AI — LFM License](https://www.liquid.ai/lfm-license)).

**Décision Thot.** Le texte intégral de la licence doit être relu et archivé sous `licenses/` avant toute distribution ; la documentation utilisateur ne présente pas ce modèle comme « libre » sans réserve.

## Conséquences pour l'architecture Thot

**Décision Thot.**

- Le grand contexte annoncé (128 000 jetons) est traité comme une **cible produit à qualifier**, conformément à [C06](../contrats_transversaux.md) : le budget se calcule avec le tokenizer et le gabarit effectifs, pas avec le chiffre commercial.
- Le seuil de compaction est un signal préventif configurable, jamais une constante déduite de la carte modèle.
- Le décodage spéculatif via drafter est une piste d'optimisation **à mesurer** dans le profil 8 Go : il ajoute des poids et une occupation mémoire supplémentaires, non chiffrés par la source.

**À mesurer** (avant de déclarer un profil supporté, cf. [C19](../contrats_transversaux.md)) : empreinte réelle poids + état d'attention + buffers à contexte utile ; débit de décodage avec et sans drafter ; comportement du contexte long sur le backend retenu.
