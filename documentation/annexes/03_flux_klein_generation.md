# Annexe 03 — FLUX.2 Klein et génération d'images

Cette fiche documente le moteur de génération d'images retenu en **phase 2** : **FLUX.2 Klein 9B**, quantifié pour tenir en 8 Go.

---

## 1. Le modèle

- **FLUX.2 Klein 9B** : modèle de diffusion de ~9 milliards de paramètres, orienté rapidité et efficacité, développé par Black Forest Labs.
- Synonyme : FLUX.2 « klein » (petit, en allemand), version allégée de la famille FLUX.2.
- Capable de **génération** et d'**édition** guidée par des images de référence.

## 2. La chaîne de diffusion et sa quantification

Un modèle de diffusion s'appuie sur plusieurs composants :
- le **backbone** de diffusion (ici ~9B) ;
- le **VAE** (encodeur/décodeur d'image latente) ;
- le **CLIP** (encodeur de texte).

Pour tenir en 8 Go, **les trois composants sont quantifiés** (backbone, VAE, CLIP). C'est déjà testé, y compris en édition multi-images (jusqu'à **4 images** d'entrée).

## 3. Intégration dans Thot

- FLUX.2 Klein suit la **même règle de swap séquentiel** que les modèles de vision : on décharge le LLM, on charge la diffusion, on génère, puis on recharge le LLM.
- L'appel est exposé comme un **outil** standard (`generate_image`) pour l'orchestrateur.
- La progression de la diffusion (4 étapes typiques) alimente une jauge dans l'interface.

## 4. Points d'attention

- **Cohabitation interdite** : ne jamais charger FLUX en même temps que le LLM sur un GPU 8 Go.
- **Édition ≠ génération** : l'édition multi-références exige de passer les images d'entrée (identifiants, pas de base64 dans l'IPC).
- Le temps de **chargement/déchargement** des gros modèles n'est pas négligeable : c'est un des coûts acceptés de l'approche « un seul modèle à la fois ».

---

## Sources

- Fiche officielle FLUX.2 [klein] (Black Forest Labs) : https://bfl.ai/models/flux-2-klein
- Annonce « FLUX.2 [klein]: Towards Interactive Visual Intelligence » : https://bfl.ai/blog/flux2-klein-towards-interactive-visual-intelligence
- Dépôt d'inférence officiel : https://github.com/black-forest-labs/flux2
- Poids sur Hugging Face : https://huggingface.co/black-forest-labs/FLUX.2-klein-9B