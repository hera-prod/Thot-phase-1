# Tools — vision / doc_ocr / get_source / read_text / search_documents / get_document_card / read_document

## 1. Dossiers englobés

```
backend/tools/
├── base.py              ← BaseTool.call (validation Pydantic, timeout 15s, max_output 8000)
├── registry.py          ← build_default_registry() : 7 outils (Phase 1 + 3 documentaires L11)
├── vision_general.py    ← VisionGeneralTool (artifact_id durable, image_path compat déprécié)
├── vision_ocr.py        ← VisionOCRTool / doc_ocr (GLM-OCR, resolve_ocr_task)
├── get_source.py        ← GetSourceTool (list-all SANS source_id, sinon détail 1 fichier)
└── read_text.py         ← ReadTextTool (texte brut / markdown)
```

Appelés UNIQUEMENT depuis `backend/inference/loop.py` (orchestrateur → exécuteur).
Jamais depuis le frontend, jamais depuis Rust directement.

## 2. Pseudo-code (comment c'est fait)

```
# Registre — source de vérité des tool_defs injectés à l'orchestrateur
registre = build_default_registry()  # vision + doc_ocr + get_source + read_text + search_documents
SI has_any_media : tool_defs = registre.definitions()  # les 5 (mode chat pur : search seul)

# Appel — un seul outil par itération, séquentiel
POUR chaque itération (max 5) :
  décision = orchestrateur(structuré tool_call OU texte)
  SI décision == "[vision(...)]" textuel → INVALID_DECISION, ne PAS exécuter, sanitizer strip
  SI décision == tool_call{nom, args} :
    outil = registre.get(nom)           # KeyError → "Outil inconnu"
    résultat = BaseTool.call(args) :
      valider Pydantic (artifact_id/source_id requis, traversal interdit hors data/blobs|temp)
      vérifier scope C18 (artifact lié à la conv, sinon ATTACHMENT_NOT_IN_SCOPE)
      vérifier taille ≤ 20 Mo (WARN 5 Mo → resize 1536 côté frontend, pas rejet dur)
      exécuter avec timeout 15s, tronquer à max_output 8000
      persister Observation {artifact_id, tool_name, status, payload_ref}
    ajouter au brief générateur (jamais brut > 8K, 8-16K tronqué, >16K résumé)

# Détail par outil
vision.inspect(artifact_id, query) :
  query_nette = clean_media_query(query)   # strip déixis : "la précédente image", "revoit..."
  image = prepare_vision_image(blob 1536px JPEG88, tracé NaFlex 512)
  prompt = SYSTEM_VISION (3-5 phrases) + query_nette
  réponse = VL-3B.multimodal(image_base64=data:image/jpeg;base64,...)

doc_ocr(artifact_id, query?) :
  query_nette = clean_media_query(query)
  tâche = resolve_ocr_task(mime, query_nette)  # Text / Table / Formula Recognition
  réponse = GLM-OCRavec_mmproj(artifact_bytes)  # jamais sans_mmproj (fix 09/09)

get_source(source_id?) :
  SI source_id fourni → détail 1 fichier (nom, kind, hash, aperçu)
  SINON → liste TOUS les fichiers de la conv (R3, courant + historique via MessageAttachment)

read_text(source_id) :
  lire blob texte/markdown, tronquer proprement, retourner extrait + total
```

## 2bis. Schémas ASCII

```
ROUTAGE (qui appelle quoi — jamais le frontend, jamais Rust)
────────────────────────────────────────────────────────────
loop.py (orchestrateur)
  │ tool_defs = registry.definitions()  (SEULEMENT si has_any_media)
  │ décision structurée tool_call{nom, args}
  ▼
┌──────────────┐  get(nom)   ┌─────────────────────────────────┐
│ ToolRegistry │ ───────────▶ │ vision │ doc_ocr │ get_source │ │
│  4 outils    │  KeyError si │         │         │ read_text  │ │
│              │  inconnu     └────────┬────────────────────────┘
└──────────────┘                       │ BaseTool.call(args)
                                       ▼
                              ┌─────────────────────┐
                              │ 1. valider Pydantic │
                              │ 2. scope C18        │──▶ ATTACHMENT_NOT_IN_SCOPE si hors conv
                              │ 3. taille ≤20 Mo    │
                              │ 4. timeout 15s      │
                              │ 5. tronquer 8000   │
                              │ 6. persist Observ.  │
                              └─────────┬───────────┘
                                        │ résultat → brief générateur
                                        ▼
                              ┌─────────────────────┐
                              │ ≤8K brut │ 8-16K    │
                              │ tronqué  │ >16K     │
                              │ résumé    │          │
                              └─────────────────────┘
```

```
CHOIX OUTIL (rappel R1/R2/R3)
─────────────────────────────
  entrée = (kind, mime, query_nette)
    │
    ├─ kind=pdf ───────────────────────────▶ doc_ocr
    ├─ kind=image + query demande lecture ──▶ doc_ocr   ("lire...écrit en japonais")
    ├─ kind=image ─────────────────────────▶ vision
    ├─ kind=texte ─────────────────────────▶ read_text
    └─ query sans source_id ───────────────▶ get_source (list-all R3)

  vision   : blob → 1536px JPEG88 → data:image/jpeg;base64 → VL-3B (3-5 phrases)
  doc_ocr  : blob → resolve_ocr_task(Text/Table/Formula) → GLM-OCR + mmproj (jamais sans)
```

## 3. Extraits réels

```python
# backend/tools/registry.py
registry.register_many([VisionGeneralTool(), VisionOCRTool(), GetSourceTool(), ReadTextTool()])
```

```python
# Schémas (args par outil — cf. test_prompt_tool_parity.py)
vision:     {artifact_id: str, query: str}
doc_ocr:    {artifact_id: str, query?: str}   # artifact_id ajouté 09/09
get_source: {source_id?: str}                 # absent = list-all
read_text:  {source_id: str}
```

Règles prompt orchestrateur (rappel) : un seul outil par tour, jamais d'id/chemin inventé,
jamais de `[vision(...)]` textuel.

## 4. Pièges

- `image_path` = compat dépréciée → log `compat`, cible = `artifact_id` durable (`data/blobs`).
- Traversal : seuls `data/blobs` et `data/temp` autorisés (20 Mo max).
- Doublon `(tool, artifact_id)` dans le même tour → servi depuis le cache, pas réexécuté.
