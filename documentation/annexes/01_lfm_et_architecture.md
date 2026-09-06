# Annexe 01 — LFM et attention linéaire

Cette fiche explique pourquoi **LFM 2.5** est un choix adapté à une contrainte de **8 Go de VRAM** : c'est un modèle à **mélange d'experts** (MoE) dont l'architecture réduit drastiquement la mémoire de contexte.

---

## 1. Ce qu'est LFM 2.5

- **LFM 2.5 8B-A1B** : un modèle de ~8 milliards de paramètres au total, mais avec **~1 milliard de paramètres actifs** par jeton (MoE). Champ d'application : texte pur, sans vision.
- Contexte : **128 000 jetons**.
- Distribué pour inférence locale (via GGUF pour `llama.cpp`).

## 2. L'absence de cache KV traditionnel

Les transformeurs classiques stockent un **cache clé-valeur (KV)** qui grandit avec la longueur du contexte : c'est ce qui rend un contexte de 128k prohibitif (6 à 8 Go de VRAM à lui seul en précision 16 bits).

L'architecture « Liquid » de Liquid AI remplace en grande partie cette attention dense par :
- une **attention linéaire** (approximation à coût constant, au lieu de quadratique) ;
- un **échange de jetons par diffusion sans encodage positionnel** (NoPE), qui permet au modèle de « raisonner » sur de longues séquences sans alourdir la mémoire.

Résultat : la consommation mémoire du **contexte** devient très faible face à un transformeur classique, ce qui rend un grand contexte viable sur matériel grand public.

> **Conséquence directe pour le projet** : on peut viser les 128k de contexte en phase 1 sans exploser la borne VRAM.

## 3. MoE et coût d'inférence

Seul un sous-ensemble des experts (chez Liquid AI, souvent 1 à 2 sur ~50) est activé par jeton. Cela divise le coût de calcul tout en gardant la capacité d'un modèle large.

## 4. Points d'attention

- LFM 2.5 est **texte pur** : il ne peut pas recevoir d'image. La vision passe par des modèles dédiés (VL-3B, GLM-OCR).
- Certains *chat handler* multimodaux ne sont pas toujours disponibles selon la version de `llama-cpp-python` : prévoir des stratégies de repli (voir [Annexe 02](02_quantification_gguf.md)).

---

## Sources

- Liquid AI — « Introducing LFM2 » (annonce officielle de la 2ᵉ génération) : https://www.liquid.ai/blog/liquid-foundation-models-v2-our-second-series-of-generative-ai-models
- LFM2 Technical Report (arXiv) : https://arxiv.org/html/2511.23404v1
- Documentation Liquid — fiche LFM2.5-8B-A1B : https://docs.liquid.ai/lfm/models/lfm25-8b-a1b