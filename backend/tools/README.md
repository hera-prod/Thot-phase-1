# Tools — 10 outils : 5 de découverte (search_info / find_files / ask_files / generate_image / web_research) + 5 sous-outils (vision / doc_ocr / read_document / read_text / search_documents)

> Note : les renvois `docs/...`, `q-r.md` et à la « Bible » désignent la documentation interne du projet ; elle n'est pas incluse dans l'export HuggingFace.

## 1. Dossiers englobés

```
backend/tools/
├── base.py              ← BaseTool.call (validation Pydantic, timeout 15s, max_output 8000)
├── errors.py            ← codes d'erreur normalisés renvoyés au modèle (ID_OUT_OF_SCOPE, TIMEOUT, …)
├── registry.py          ← build_default_registry() : 10 outils (5 de découverte + 5 sous-outils)
├── vision_general.py    ← VisionGeneralTool / vision (artifact_id durable, image_path compat déprécié)
├── vision_ocr.py        ← VisionOCRTool / doc_ocr (worker PaddleOCR-VL, le moteur OCR livré ; GLM-OCR n'a jamais été livré, abandonné)
├── read_document.py     ← ReadDocumentTool (fiche + extrait d'un artefact)
├── read_text.py         ← ReadTextTool (texte brut / markdown)
├── search_documents.py  ← SearchDocumentsTool (FTS5 + ColBERT, portée conversation)
├── search_info.py       ← SearchInfoTool (recherche sémantique ColBERT)
├── find_files.py        ← FindFilesTool (identifier une ressource par description/fiche)
├── ask_files.py         ← AskFilesTool (route vision/doc_ocr/read_text selon le type de fichier)
├── generate_image.py    ← GenerateImageTool (diffusion FLUX.2 Klein)
└── web_research.py      ← WebResearchTool (seule sortie réseau du tour)
```

Appelés UNIQUEMENT depuis `backend/inference/loop.py` (orchestrateur → exécuteur).
Jamais depuis le frontend, jamais depuis Electron directement.

## 2. Pseudo-code (comment c'est fait)

```
# Registre — source de vérité des tool_defs injectés à l'orchestrateur
registre = build_default_registry()  # 10 outils enregistrés
#   Découverte (proposés au modèle) : search_info, find_files, ask_files,
#   generate_image, web_research                                    (5)
#   Sous-outils (appelés en interne, jamais exposés au modèle)       (5) :
#       VisionOCRTool, VisionGeneralTool, ReadDocumentTool,
#       ReadTextTool, SearchDocumentsTool
# `get_source` et `get_document_card` ont été retirés du registre : aucun
# appelant. Ils ne servaient qu'à annoncer des actions que le modèle ne
# pouvait pas exécuter. Aucun outil n'est enregistré sans rôle.

# Appel — un seul outil par itération, séquentiel, STRUCTUREL (plus de texte-ou-appel)
POUR chaque itération (max MAX_ITER = 16, backend/inference/loop.py) :
  réponse = orchestrateur(natifs runtime OU [fn(k='v')] texte — auto fuit, parser récupère)
  SI submit_brief → validate_submit_brief (schéma + preuves + faits) → brief rendu, FIN
  SI découverte → Pydantic strict AVANT exécution (inconnu/malformé = note correctif) :
    outil = registre.get(nom)
    résultat = exécuteur :
      valider Pydantic (artifact_id/source_id requis, traversal interdit hors data/blobs|temp)
      vérifier scope C18 (artifact lié à la conv, sinon ATTACHMENT_NOT_IN_SCOPE)
      vérifier taille ≤ 20 Mo (WARN 5 Mo → resize 1536 côté frontend, pas rejet dur)
      exécuter avec timeout 15s, tronquer à max_output 8000
      persister Observation {artifact_id, tool_name, status, payload_ref}
    résultat → contexte orchestrateur (Phase 1) + bulle UI ; le GÉNÉRATEUR ne voit QUE
    le brief validé rendu (contrat brief-seul — jamais de brut d'outil)
  SINON (silence) → phase brief FORCÉE (submit_brief seul, tool_choice nommé prouvé, retry ×2)

# Détail par outil
vision(artifact_id, query) :
  query_nette = clean_media_query(query)   # strip déixis : "la précédente image", "revoit..."
  image = prepare_vision_image(blob 1536px JPEG88, tracé NaFlex 512)
  prompt = SYSTEM_VISION (3-5 phrases) + query_nette
  réponse = VL-3B.multimodal(image_base64=data:image/jpeg;base64,...)

doc_ocr(artifact_id, query?) :
  query_nette = clean_media_query(query)
  tâche = resolve_ocr_task(mime, query_nette)  # Text / Table / Formula Recognition
  réponse = worker_PaddleOCRVL(artifact_bytes)  # PaddleOCR-VL = moteur livré ; GLM-OCR jamais livré, abandonné

search_info(query) : vecteurs de jetons ColBERT (128 dim/jeton), MaxSim, portée conversation
find_files(text?) : identifier une ressource par sa description ou sa fiche
ask_files(kind, mime, query) : route vers vision / doc_ocr / read_text selon le type
read_document(artifact_id) : fiche + extrait borné (jamais de chemin vers le LLM)
read_text(source_id) : lire blob texte/markdown, tronquer proprement, retourner extrait + total
generate_image(prompt, ...) : une image par tour, déduplication, prompt = argument du modèle
web_research(query) : SEULE sortie réseau du tour (mode local_only → WEB_DISABLED)
```

## 2bis. Schémas ASCII

```
ROUTAGE (qui appelle quoi — jamais le frontend, jamais Electron)
────────────────────────────────────────────────────────────
loop.py (orchestrateur)
  │ tool_defs = 5 outils de découverte (+ submit_brief toujours) + 5 sous-outils
  │ natifs runtime OU [fn(k='v')] texte parsé (auto fuit, parser récupère)
  ▼
┌──────────────┐            ┌──────────────────────────────────┐
│ ToolRegistry │  get(nom)  │ Découverte (proposés au modèle)  │
│  10 outils   │ ─────────▶ │ search_info · find_files ·       │
│ (+ submit_   │  Pydantic  │ ask_files · generate_image ·     │
│   brief      │  si inconnu│ web_research                     │
│   terminator)│ (correctif)│ Sous-outils (jamais exposés) :   │
│              │            │ vision · doc_ocr · read_text ·   │
│              │            │ read_document · search_documents │
└──────────────┘            └──────────────────┬───────────────┘
                                               │ BaseTool.call(args)
                                               ▼
                                      ┌─────────────────────┐
                                      │ 1. valider Pydantic │
                                      │ 2. scope C18        │──▶ ATTACHMENT_NOT_IN_SCOPE si hors conv
                                      │ 3. taille ≤20 Mo    │
                                      │ 4. timeout 15s      │
                                      │ 5. tronquer 8000    │
                                      │ 6. persist Observ.  │
                                      └─────────┬───────────┘
                                                │ résultat → brief générateur
                                                ▼
                                      ┌─────────────────────┐
                                      │ ≤8K : brut          │
                                      │ 8–16K : tronqué     │
                                      │ >16K : résumé       │
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
    └─ query sans source_id ───────────────▶ search_documents (recherche documentaire)

  vision   : blob → 1536px JPEG88 → data:image/jpeg;base64 → VL-3B (3-5 phrases)
  doc_ocr  : blob → gate (natif/PaddleOCR-VL/scène→VL-3B) ; PaddleOCR-VL est le seul moteur OCR livré
```

## 3. Extraits réels

```python
# backend/tools/registry.py — 10 outils enregistrés (5 de découverte + 5 sous-outils)
registry.register_many([SearchInfoTool(), FindFilesTool(), AskFilesTool()])
registry.register(GenerateImageTool())
registry.register(WebResearchTool())
# Sous-outils : compétences appelées par les outils ci-dessus. Ni exposés au
# modèle, ni protégés d'un appel direct — les retirer casserait `ask_files`.
registry.register_many([
    VisionOCRTool(), VisionGeneralTool(),
    ReadDocumentTool(), ReadTextTool(), SearchDocumentsTool(),
])
```

```python
# Schémas (args par outil — cf. test_prompt_orchestrateur.py)
vision:     {artifact_id: str, query: str}
doc_ocr:    {artifact_id: str, query?: str}   # artifact_id ajouté 09/09
read_document: {artifact_id: str, query?: str, locator?: str, max_chars?: int (500..16000), purpose: str}
read_text:  {source_id: str}
search_info: {query: str}
search_documents: {query: str (1..300), scope?: conversation|workspace, types?: [...],
                   status?: any|unanalysed|indexed|partial|analysed|failed|stale,
                   limit?: 1..20, cursor?: str, purpose: str}
find_files: {text?: str}
ask_files:  {kind: str, mime: str, query: str}
generate_image: {prompt: str, ...}
web_research: {query: str}
```

Règles prompt orchestrateur (rappel) : un seul outil par tour, jamais d'id/chemin inventé,
jamais de `[vision(...)]` textuel.

## 4. Pièges

- `image_path` = compat dépréciée → log `compat`, cible = `artifact_id` durable (`data/blobs`).
- Traversal : seuls `data/blobs` et `data/temp` autorisés (20 Mo max).
- Doublon `(tool, artifact_id)` dans le même tour → servi depuis le cache, pas réexécuté.
- `get_source` / `get_document_card` ont été retirés : aucun appelant. Ne pas les réintroduire.
