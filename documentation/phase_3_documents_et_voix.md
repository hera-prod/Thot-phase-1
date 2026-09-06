# Phase 3 — Génération de fichiers et voix

> **Objectif** : permettre au modèle de **créer des fichiers** (CSV, PDF, Markdown) dans la discussion, et ajouter la **voix** (transcription de l'utilisateur + lecture des réponses).

---

## 1. Ce qui est nouveau

- **Génération de fichiers** : l'orchestrateur peut produire CSV, PDF, Markdown. Les fichiers générés rejoignent la **galerie**, désormais catégorisée (`texte`, `pdf`, `table`, `image`).
- **Entrée vocale (STT)** : l'utilisateur parle, le texte est retranscrit dans le champ de saisie.
- **Sortie vocale (TTS)** : lecture à voix haute d'un message texte.

---

## 2. Génération de fichiers : le modèle ne produit jamais d'octets bruts

Règle fondamentale : le **LLM émet du texte structuré** (lignes d'un tableau, squelette d'un rapport), et c'est le **backend Python** qui compile le fichier réel avec des bibliothèques fiables :

- **CSV** → module standard `csv`.
- **PDF** → `reportlab` ou `fpdf2`.
- **Markdown** → simple écriture texte.

> **Pourquoi ?** Demander au LLM d'émettre un flux binaire produit des fichiers corrompus et épuise le contexte avec du balisage inutile. En séparant « ce que le modèle pense » de « ce que Python écrit », les fichiers sont toujours syntaxiquement valides.

référence : [Annexe 05 — Génération de documents en Python](../annexes/05_generation_documents_python.md) et [Pseudo-code backend — audio & documents](../pseudocode/backend_audio_documents.md).

---

## 3. La voix s'exécute sur le CPU (pas le GPU)

Le micro et la synthèse ne doivent **jamais** décharger le LLM de la VRAM. STT et TTS s'exécutent sur les **cœurs du processeur**, en mémoire système :

| Rôle | Moteur possible | Empreinte |
| :--- | :--- | :--- |
| STT (micro) | `whisper.cpp` / `Moonshine` | ~100–250 Mo RAM CPU |
| TTS (lecture) | `Piper` / `Kokoro-82M` | ~50–150 Mo RAM CPU |

Le flux audio est capturé côté Tauri, transmis à un **thread Python dédié** (indépendant de la boucle d'inférence), et le texte retranscrit est injecté dans le champ de saisie.

référence : [Annexe 04 — Audio embarqué (STT/TTS)](../annexes/04_audio_embarque.md).

---

## 4. Catégorisation de la galerie

La galerie (créée en phase 2) évolue en **explorateur universel** grâce à une colonne de catégorie :

| Catégorie | Contenu |
| :--- | :--- |
| `image` | générations FLUX, imports |
| `table` | CSV |
| `document` | PDF, Markdown |
| `raw` | tout autre fichier |

référence : [Pseudo-code frontend — galerie](../pseudocode/frontend.md).

---

## 5. Points de vigilance

- **STT/TTS sur CPU uniquement** — ne pas charger de modèle audio sur le GPU.
- **Génération de fichiers = side-car Python**, pas de flux binaire du LLM.
- **Thread audio indépendant** : le microphone ne doit jamais bloquer le cycle d'inférence de l'orchestrateur.
- **`read_text` (lecture d'un fichier) ≠ TTS (le lire à voix haute)** : ne pas confondre l'extraction de texte et la synthèse vocale.