# Annexe 03 — FLUX.2 klein et génération d'images

Sources récupérées le **2026-09-06**. Conventions **Fait / Décision Thot / À mesurer** : voir le [sommaire des annexes](README.md).

## Composition de la pile

| Élément | Fait | Source |
| :--- | :--- | :--- |
| Encodeur de texte de klein 9B | **Qwen3, 8 B** (« 8B Qwen3 text embedder ») | [Carte FLUX.2-klein-9B](https://huggingface.co/black-forest-labs/FLUX.2-klein-9B), [Blog BFL klein](https://bfl.ai/blog/flux2-klein-towards-interactive-visual-intelligence) |
| Encodeur de FLUX.2 [dev] | **Mistral-3, 24 B** (modèle vision-langage) | [Blog BFL — FLUX.2](https://bfl.ai/blog/flux-2) |
| VAE | Un **FLUX.2 - VAE** dédié est publié sous **Apache 2.0** | [Blog BFL — FLUX.2](https://bfl.ai/blog/flux-2) |
| Encodeur de texte de klein **Base** 9B | `n.a.` — non indiqué sur la carte | [Carte FLUX.2-klein-base-9B](https://huggingface.co/black-forest-labs/FLUX.2-klein-base-9B) |
| Canaux latents du VAE | `n.a.` — aucune source primaire récupérée ne le précise | — |

**Correction.** La révision précédente de cette fiche décrivait un **encodeur de texte CLIP** et un « CLIP quantifié ». Aucune source ne mentionne CLIP pour FLUX.2 : l'affirmation est retirée. La conséquence est matérielle, pas cosmétique — un encodeur Qwen3 de 8 B pèse plusieurs gigaoctets et doit être budgété comme un modèle à part entière, pas comme un petit encodeur de texte.

## Distillation et nombre d'étapes

**Fait.** klein 9B est **distillé en étapes** vers **4 étapes d'inférence** ([Carte FLUX.2-klein-9B](https://huggingface.co/black-forest-labs/FLUX.2-klein-9B)) ; la documentation BFL confirme ce régime pour klein 4B et 9B ([docs.bfl.ml — FLUX.2 overview](https://docs.bfl.ml/flux_2/flux2_overview)).

**Fait.** klein **Base** 9B est décrit comme **non distillé**, entraîné sans distillation d'étapes ni de guidance, avec un exemple d'inférence à **50 étapes** ([Carte FLUX.2-klein-base-9B](https://huggingface.co/black-forest-labs/FLUX.2-klein-base-9B)).

**Décision Thot.** Pour une application locale sous contrainte GPU, la variante distillée est le candidat par défaut du studio image ; la variante Base reste réservée à d'éventuels travaux de personnalisation, avec un coût de calcul d'un autre ordre.

## Licences

**Fait.** Les modèles **klein 9B et klein Base 9B** sont publiés sous **FLUX Non-Commercial License** ; les modèles **4B** (klein et Base) sont annoncés sous **Apache 2.0** ([docs.bfl.ml — FLUX.2 overview](https://docs.bfl.ml/flux_2/flux2_overview)).

**Fait.** L'ancienne « FLUX [dev] Non-Commercial License » a été renommée « FLUX Non-Commercial License », et les variantes de précision réduite publiées par BFL restent sous la même licence ([Blog BFL klein](https://bfl.ai/blog/flux2-klein-towards-interactive-visual-intelligence)).

L'URL canonique du texte intégral de cette licence est `n.a.` sur les pages récupérées.

**Décision Thot.** La licence détermine l'édition distribuable : une édition destinée à un usage commercial ne peut embarquer que des poids compatibles (piste **4B Apache 2.0**), et une édition embarquant du 9B doit afficher la restriction non commerciale dans `licenses/` et à l'écran de premier lancement ([C18](../contrats_transversaux.md)).

## Mémoire

**Fait.** Les cartes des modèles 9B annoncent une empreinte d'**environ 29 Go de VRAM** et une accessibilité « RTX 4090 et au-delà » ([Carte klein-9B](https://huggingface.co/black-forest-labs/FLUX.2-klein-9B), [Carte klein-base-9B](https://huggingface.co/black-forest-labs/FLUX.2-klein-base-9B)). La documentation BFL annonce **environ 13 Go** pour les variantes 4B ([docs.bfl.ml](https://docs.bfl.ml/flux_2/flux2_overview)).

**Aucune source récupérée n'annonce un fonctionnement en 8 Go**, quantifié ou non (`n.a.`). Toute mention antérieure d'un fonctionnement « déjà testé » sous 8 Go est retirée.

**Décision Thot.** Conformément à [C07](../contrats_transversaux.md), le pipeline image obtient une location exclusive du GPU, et son chargement interne (transformeur + VAE + encodeur de texte) doit respecter le profil **mesuré**, pas estimé. Si aucune configuration ne tient dans le profil cible, l'interface déclare la génération d'images **indisponible** pour ce profil, conformément au [contrat de portabilité](../portabilite.md), au lieu de promettre une équivalence.

**À mesurer** : pic mémoire réel par composant et en séquence ; faisabilité d'un déchargement/rechargement par étage (encodeur, transformeur, VAE) ; qualité et latence d'une variante 4B quantifiée sur GPU 8 Go ; poids total sur disque de la pile complète, à confronter aux limites du support de distribution (voir [annexe 08](08_portabilite_et_distribution.md)).
