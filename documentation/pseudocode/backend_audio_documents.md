# Pseudo-code — Audio et génération de fichiers (Phase 3)

> Phase 3. Ce fichier couvre la **génération de fichiers** (CSV/PDF/Markdown) et la **voix** (STT + TTS), le tout sans toucher au GPU.

---

## 1. Génération de fichiers

```python
[Phase 3]
# tools/generate_file.py — le LLM émet du contenu structuré, Python matérialise le fichier.
class GenerateFileTool(BaseTool):
    name = "generate_file"
    def execute(self, args):
        kind = args["file_type"]     # "csv" | "pdf" | "markdown"
        content = args["content"]    # ⚠️ contenu STRUCTURÉ (lignes, titre, paragraphes), jamais binaire

        if kind == "csv":
            path = write_csv(content)          # module standard csv
        elif kind == "pdf":
            path = write_pdf(content)          # reportlab ou fpdf2
        else:
            path = write_markdown(content)     # simple fichier texte .md

        # ➜ enregistre l'artifact dans la galerie (catégorie "table"/"document").
        record = ingest_artifact(path, category=kind_to_category(kind))
        return {"ok": True, "result": record}
```

> **Pourquoi passer par Python ?** Demander au LLM d'émettre un flux binaire produit des fichiers corrompus. Ici le modèle décrit le contenu, le backend garantit un fichier valide.

---

## 2. Transcription (STT) sur CPU

```python
[Phase 3]
# audio/stt.py — ⚠️ s'exécute sur le CPU dans un THREAD dédié, jamais sur le GPU.
def stt_worker(audio_chunks):
    # NOTE : thread indépendant de la boucle d'inférence (ne bloque jamais l'orchestrateur).
    for chunk in audio_chunks:
        if vad_detected(chunk):                       # détection d'activité vocale
            text = whisper_or_moonshine.transcribe(chunk)   # Moonshine / whisper.cpp (CPU)
            emit_event("stt_result", {"text": text})   # ➜ injecté dans le champ de saisie
```

---

## 3. Synthèse vocale (TTS) sur CPU

```python
[Phase 3]
# audio/tts.py — lecture d'un message : génère un flux audio court, renvoyé à l'UI.
def read_aloud(message):
    audio = piper_or_kokoro.synthesize(message.text)   # Piper / Kokoro-82M (CPU, <150 Mo)
    emit_event("tts_audio", {"audio": audio})          # ➜ lecture immédiate côté interface
```

---

## Points de vigilance

- **STT/TTS sur CPU uniquement** : charger un modèle audio sur le GPU déchargerait le LLM à chaque prise de parole.
- **Thread audio indépendant** : ne jamais bloquer le cycle d'inférence.
- **`GenerateFileTool` ≠ `read_text`/`doc_ocr`** : la génération de fichier **écrit**, les autres **lisent**.
- **Génération ≠ retranscription** : la génération de fichier est un outil du LLM ; la retranscription vocale est un périphérique utilisateur.

---

## Correspondance

- [Document — Phase 3](../phase_3_documents_et_voix.md)
- Annexes : [04 — Audio embarqué](../annexes/04_audio_embarque.md), [05 — Génération de documents](../annexes/05_generation_documents_python.md)
- Suite : [backend_recherche.md](backend_recherche.md)