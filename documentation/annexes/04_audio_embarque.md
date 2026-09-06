# Annexe 04 — Audio embarqué (STT / TTS)

Cette fiche documente les moteurs de **transcription** (Speech-to-Text) et de **synthèse vocale** (Text-to-Speech) utilisés en **phase 3**, exclusivement sur **CPU**.

---

## 1. Pourquoi le CPU ?

Charger un modèle audio sur le GPU obligerait à décharger le LLM à chaque prise de parole. En exécutant STT/TTS sur les cœurs du processeur (RAM système), l'utilisateur peut dicter pendant que le LLM reste prêt en VRAM. Les modèles sont assez petits pour que ce soit imperceptible en latence.

## 2. Transcription (STT)

| Moteur | Empreinte | Note |
| :--- | :--- | :--- |
| **Moonshine** | ~50–100 Mo (ONNX) | très rapide, optimisé pour la dictée en continu |
| **whisper.cpp** (Base) | ~150–250 Mo | excellente prise en charge du français |

- **Moonshine** est un modèle ASR léger conçu pour la transcription en direct et les commandes vocales.
- **whisper.cpp** est le port C/C++ de Whisper (OpenAI), exécutable sur CPU avec vectorisation (AVX2 / Neon).

## 3. Synthèse vocale (TTS)

| Moteur | Empreinte | Note |
| :--- | :--- | :--- |
| **Piper** | ~50 Mo (ONNX) | voix rapides, tourne sur CPU, format ONNX |
| **Kokoro-82M** | ~150 Mo (ONNX) | voix naturelles, 82M paramètres |

Les deux fonctionnent via le runtime **ONNX** sur CPU et proposent des voix françaises.

## 4. Architecture d'intégration

- **VAD** (détection d'activité vocale) en amont pour segmenter la parole.
- Tauri capture l'audio → envoie en tampon à un **thread Python dédié** → le texte transcrit est injecté dans le champ de saisie.
- Pour la lecture : clic sur l'icône → backend génère un court flux audio → lecture côté interface.

> **Règle** : le fil audio est **indépendant** du cycle d'inférence de l'orchestrateur ; il ne doit jamais le bloquer.

---

## Sources

- Moonshine — papier : https://arxiv.org/pdf/2410.15608
- Moonshine — docs Hugging Face : https://huggingface.co/docs/transformers/model_doc/moonshine
- whisper.cpp — dépôt : https://github.com/ggml-org/whisper.cpp
- Piper TTS — dépôt (rhasspy) : https://github.com/rhasspy/piper
- Kokoro-82M (ONNX) — Hugging Face : https://huggingface.co/onnx-community/Kokoro-82M-ONNX