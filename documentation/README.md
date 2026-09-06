# Thot — Documentation centrale

> **Thot** est un assistant conversationnel **local**, pensé pour fonctionner sur une carte graphique de **moins de 8 Go de VRAM**, multiplateforme (NVIDIA, AMD, Apple, et repli CPU/APU via Vulkan ou inférence processeur).

Ce document est l'**index central** de toute la documentation du projet. Il renvoie vers les documents de chaque phase, vers le dossier d'**annexes** (recherche scientifique, guides Python, ressources) et vers le **pseudo-code** annoté.

---

## 1. Vision du produit

Le projet est construit par **incréments successifs** : chaque phase pose une fondation qui ne sera jamais réécrite, seulement étendue. On appelle cela une architecture « phase = socle de la phase suivante ».

| Phase | Ce qu'elle apporte | Fichier |
| :--- | :--- | :--- |
| **Phase 1** | Chat + boucle agentique (orchestrateur / générateur) + vision + OCR + compaction | [phase_1_socle_et_vision.md](phase_1_socle_et_vision.md) |
| **Phase 2** | Génération d'images (FLUX.2 Klein 9B) + galerie + édition multi-références | [phase_2_generation_image.md](phase_2_generation_image.md) |
| **Phase 3** | Génération de fichiers (CSV/PDF/Markdown) + voix (STT + TTS) | [phase_3_documents_et_voix.md](phase_3_documents_et_voix.md) |
| **Phase 4** | Recherche web + deep research + cache de pages + fiches de fichiers | [phase_4_moteur_recherche.md](phase_4_moteur_recherche.md) |
| **Phase 5** | Espaces de travail + mémoire d'espace + référencement par score | [phase_5_espaces_et_memoire.md](phase_5_espaces_et_memoire.md) |

---

## 2. Contraintes fondamentales (invariants du projet)

Ces contraintes traversent **toutes** les phases. Elles ne doivent jamais être violées, sous peine de dette technique.

1. **Borne mémoire stricte (< 8 Go de VRAM)** — un seul modèle résident à la fois. Le passage d'un modèle à l'autre passe par un **déchargement/chargement séquentiel** (swap), jamais une cohabitation.
2. **Modèle de langage principal** : LFM 2.5 8B (activité réelle ~1B), contexte 128k, **sans cache KV classique** grâce à l'attention linéaire → le contexte coûte très peu de mémoire. Voir [annexe 01](annexes/01_lfm_et_architecture.md).
3. **Double invitation système** : un même modèle produit deux rôles — l'**orchestrateur** (prépare, peut appeler des outils) et le **générateur** (rédige la réponse finale, sans outil). Voir [phase 1](phase_1_socle_et_vision.md).
4. **Bus IPC** : Tauri ↔ Python en **JSON Lines sur stdin/stdout** via un *sidecar* (binaire PyInstaller). Pas de transport HTTP interne.
5. **Stockage** : base **SQLite** locale + répertoire de fichiers centralisé. Les images ne transitent **jamais** en base64 dans le bus IPC.
6. **Traitements lourds hors GPU** : transcription et synthèse vocale s'exécutent sur le **CPU** (voir [phase 3](phase_3_documents_et_voix.md)).

---

## 3. Contenu des annexes

Le dossier [`annexes/`](annexes/README.md) regroupe la **recherche scientifique**, les **guides d'utilisation Python** et les **ressources** qui justifient chaque choix d'architecture. Chaque document de phase cite ses annexes en note.

Le [sommaire des annexes](annexes/README.md) liste l'ensemble des fiches et renvoie vers leurs sources (papiers, documentations officielles).

---

## 4. Pseudo-code

Le dossier [`pseudocode/`](pseudocode/README.md) contient le **pseudo-code annoté** de l'application, backend et frontend.

- Chaque fragment est **marqué par phase** (`[Phase 1]`, `[Phase 2]`…) pour savoir à quel moment il apparaît.
- Le pseudo-code est **volontairement non exécutable** : c'est un plan de travail pour toi, pas un programme fini.
- Les **notes** expliquent *pourquoi* chaque élément existe, *ce qui l'appelle*, et les **pièges à ne pas confondre**.

---

## 5. Guide de lecture rapide

1. Commencer par [phase_1_socle_et_vision.md](phase_1_socle_et_vision.md) pour comprendre le socle.
2. Lire les annexes au fur et à mesure qu'elles sont citées (ex. [annexe 01](annexes/01_lfm_et_architecture.md) pour LFM).
3. Consulter le [pseudo-code](pseudocode/README.md) pour passer de la spécification à l'implémentation.
4. Les phases 2 à 5 se lisent en séquence : chacune suppose la précédente acquise.

---

## 6. Correspondance avec le code existant

Le dépôt contient déjà l'implémentation de la **phase 1** (`backend/`, `frontend/`, `src-tauri/`). La documentation décrit l'architecture **cible** sur les 5 phases ; le pseudo-code sert de référence avant réécriture ou extension.

- Stack réelle : React 19 + Vite + Tauri 2, sidecar Python `llama-cpp-python` (GGUF), SQLite via SQLAlchemy.
- Modèles : LFM2.5-8B-A1B (LLM), LFM2.5-VL-3B (vision générale), GLM-OCR (vision texte).

---

*Voir aussi : [README principal](../../README.md) du dépôt pour les commandes de lancement et de build.*