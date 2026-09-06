# Phase 2 — Génération d'images

> **Objectif** : ajouter un moteur de **génération d'images** (FLUX.2 Klein 9B, quantifié) et une **galerie**, tout en laissant l'orchestrateur appeler la génération depuis une discussion (création et édition multi-références).

---

## 1. Ce qui est nouveau

- Un **onglet « Génération »** dans la barre latérale pour générer une image directement.
- Un **onglet « Galerie »** (distinct de l'historique) listant toutes les images générées ou importées.
- La possibilité, **depuis une discussion**, que l'orchestrateur appelle l'outil de génération : il fournit le prompt et éventuellement des images de référence (édition jusqu'à 4 entrées).
- Le résultat est enregistré, visible dans la galerie et inséré dans la discussion.

---

## 2. Moteur de génération

Modèle retenu : **FLUX.2 Klein 9B**, quantifié (avec VAE et CLIP également quantifiés), testé pour tenir dans **8 Go**. Voir [Annexe 03 — FLUX.2 Klein](../annexes/03_flux_klein_generation.md).

- La diffusion ne **cohabite pas** avec le LLM : elle suit la même règle de swap séquentiel.
- L'outil de génération est un pilote de plus dans le `ModelManager` (décharger le LLM → charger FLUX → générer → recharger le LLM).

---

## 3. L'outil de génération vu de l'orchestrateur

L'appel respecte la **même grammaire JSON** que les outils de vision de la phase 1. Rien de nouveau côté protocole :

```json
{
  "tool": "generate_image",
  "arguments": {
    "prompt": "Un paysage cyberpunk brumeux",
    "reference_ids": ["img_001", "img_002"],
    "aspect_ratio": "16:9"
  }
}
```

- `reference_ids` réutilise des images déjà présentes dans la conversation (édition).
- Le résultat (chemin d'image + métadonnées) remonte au générateur, qui peut le commenter.

référence : [Pseudo-code backend — génération d'images](../pseudocode/backend_images.md).

---

## 4. La galerie

La galerie interroge la **même base SQLite** que la phase 1 (table des artefacts/`sources`), en filtrant par type `image`. Elle n'introduit **aucun** nouveau mécanisme de stockage : on réutilise l'identifiant unique, le chemin local et les métadonnées déjà en place.

> **Anticipation** : en phase 3, la galerie devient un explorateur de documents universel (images + PDF + CSV + Markdown). Ne pas coder la galerie « en dur pour les images » : dès la phase 2, prévoir une colonne de catégorie (`image`, `table`, `document`, `raw`).

référence : [Pseudo-code frontend — galerie](../pseudocode/frontend.md).

---

## 5. Progression et jauge

La diffusion s'effectue en plusieurs étapes (encodage, échantillonnage, décodage VAE). Le bus IPC doit exposer une **progression** pour afficher une jauge côté frontend.

---

## 6. Points de vigilance

- **Édition ≠ génération simple** : l'édition multi-références demande de passer les images d'entrée au moteur (jusqu'à 4), pas seulement un prompt.
- **Ne pas passer d'images brutes en base64 dans l'IPC** : transmettre des identifiants, le backend résout les chemins localement.
- **Swap strict** : FLUX.2 Klein ne doit jamais être chargé en même temps que le LLM.