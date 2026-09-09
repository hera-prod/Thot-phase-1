# Inference Python — loop + manager + politique média

## 1. Dossiers englobés

```
backend/
├── main.py                 ← point d'entrée sidecar (stdin loop → worker 1 run lourd)
├── inference/
│   ├── loop.py             ← SharedContext + Phase 0 + orchestrateur → brief → générateur
│   ├── media_policy.py     ← R1/R2/R3 déterministe (JAMAIS le LLM qui décide d'observer)
│   ├── manager.py          ← FSM VRAM + admission 1 run + replis OOM
│   ├── ipc.py              ← emit/seq/run_id/protocol_version, MAX_LINE 256KB, MAX_QUEUE 32
│   ├── hardware.py         ← détection VRAM/RAM, estimate_layers
│   └── compaction.py       ← résumé + ContextSnapshot
├── prompts/
│   ├── orchestrator.py     ← SYSTEM_ORCHESTRATOR (mode d'emploi d'appel, anti-hallucination)
│   ├── generator.py        ← SYSTEM_GENERATOR (brief 6 champs seul, jamais tool_defs/chemins)
│   ├── vision.py / ocr.py / compaction.py
├── config/paths.py + defaults.py  ← MODEL_CONFIGS, resolve_n_ctx adaptatif
```

## 2. Pseudo-code (comment c'est fait)

```
# main.py — un tour par conversation, idempotent
SUR stdin_ligne :
  normalisé = {request_id, method, params, protocol_version}
  run_id = request_id            # Phase 1 : 1:1 pour idempotence C11
  SI fingerprint déjà vu → rejouer terminal sans réexécuter
  SINON worker_unique :
    persist_user_turn AVANT appel LLM (C : jamais perdre le tour user)
    loop.run_chat(...) → events streamés via emit(run_id, seq++)
    persist_assistant + toolcalls + observations AVANT emit terminal
    UN SEUL terminal par run (completed / cancelled / failed)

# loop.py — Phase 0 DÉTERMINISTE puis 2 couches LLM
Phase 0 (sans modèle) :
  registre = construire depuis Artifact/Blob si attachment_ids
             sinon image_paths (compat, log)
  plan = plan_media_tools(nouveaux_fichiers)                    # R1 : pdf→doc_ocr, image→vision
  SI pas de nouveau fichier ET média dispo :
    SI needs_reinspection(question) :                           # R2 : couches A/B/C en OR
      plan += réobserver avec question comme query
  execution_plan = {intent, selected_tool, selected_model, reason}  # vrai outil, pas "none" muet

Tour orchestrateur (LFM2.5-8B, max 5 iter, timeout 15s/outil, 300s total) :
  messages = [SYSTEM_ORCHESTRATOR, SOURCES DISPONIBLES (registre), historique user↔assistant]
  SI has_any_media → ajouter tool_defs (vision/doc_ocr/get_source/read_text/search_documents ;
    sans média : search_documents seul pour la découverte — annexe 11 Lot A)
  réponse = LLM.orchestrer(messages)
  SI réponse contient "[vision(...)]" textuel → INVALID_DECISION (jamais exécuté, sanitizer strip)
  SI tool_call structuré → exécuteur :
    vision   : clean_media_query(query) → prepare_vision_image 1536px JPEG88 → handler VL-3B
    doc_ocr  : clean_media_query(query) → handler GLM-OCR (garde son mmproj, jamais sans_mmproj)
    get_source SANS source_id → liste TOUS les fichiers de la conv (R3 list-all)
    cache (tool, artifact_id) anti-doublon dans le tour

Brief → générateur (LFM2.5-8B, SANS tools) :
  brief = {question_nettoyée, evidence_id/artifact_id, observations, consignes, limites, format}
  réponse = LLM.générer(SYSTEM_GENERATOR + brief)
  réponse = sanitize(<think> strip, SANS réémettre pensée privée)

Compaction (si budget dépassé) :
  SI count_tokens(historique) > 8000/16000 :
    résumé = LLM.résumer(anciens messages)
    snapshot = ContextSnapshot(revision, covered_through_message_id, summary)
    historique = [Résumé] + recent 15K → SharedContext(revision+1)
```

## 2bis. Schémas ASCII

```
TOUR COMPLET (main.py → loop.py)
─────────────────────────────────
stdin_ligne
  │
  ▼
[normaliser] → {request_id, method, params, proto}
  │
  ├─ fingerprint déjà vu ? ──OUI──▶ rejouer terminal (C11, sans réexécuter)
  │
  NON
  ▼
[transaction 1] Conversation + Message user + MessageAttachment  ◀── JAMAIS après le LLM
  │
  ▼
┌─────────────────────────────────────────────────────────┐
│ Phase 0 DÉTERMINISTE (media_policy, 0 appel LLM)        │
│                                                         │
│  nouveaux fichiers ?                                    │
│   OUI → R1 : pdf→doc_ocr │ image→vision │ img+texte→ocr │
│   NON + média dispo + question ancrée ?                 │
│    └─ A: "cette image/dans l'image/..."  ─┐             │
│    └─ B: "revois/regarde à nouveau/..."  ─┼─ OR ─▶ R2   │
│    └─ C: "couleur/cheveux/fond/tient" ───┘    réobserver│
│    └─ OCR: "lire...écrit en japonais" ──────▶ doc_ocr   │
│  SINON → pas d'outil (chat pur)                         │
└─────────────────────────────────────────────────────────┘
  │
  ▼
┌─────────────────────────────────────────────────────────┐
│ Orchestrateur (LFM2.5-8B, ≤5 iter, 15s/outil, 300s tot) │
│  [SYS_ORCH + SOURCES + historique] (+tool_defs si média)│
│     │                                                   │
│     ├─ texte "[vision(...)]" → INVALID_DECISION (jamais │
│     │   exécuté, sanitizer strip)                       │
│     └─ tool_call{nom,args} → exécuteur → Observation     │
│        cache (tool, artifact_id) anti-doublon            │
└─────────────────────────────────────────────────────────┘
  │
  ▼
[brief 6 champs] → Générateur (SANS tools, SANS chemins) → sanitize <think>
  │
  ▼
[transaction 2] Message assistant + ToolCall + Observation + Run  ◀── AVANT terminal
  │
  ▼
emit terminal UNIQUE (completed/cancelled/failed) — seq++
```

```
FSM VRAM (manager.py) — 1 modèle à la fois
──────────────────────
   ┌──────────┐  use_model()   ┌─────────┐  prêt  ┌─────────┐
   │ unloaded │ ──────────────▶ │ loading │ ─────▶ │  ready  │
   └──────────┘   admission=1   └─────────┘        └────┬────┘
        ▲                         │ OOM                │ run
        │                    ┌────▼────┐                ▼
        │                    │ failed  │          ┌─────────┐
        └────────────────────│ repli : │          │ running │
          unload + sleep     │ n_layers▼, n_ctx/2 │         │
          0.5s + attente 5s  └─────────┘          └────┬────┘
                                                      │ done
                                                      ▼
                                                ┌───────────┐
                                                │ unloading │
                                                └───────────┘

  n_ctx : ≥12GB → 131072 · sinon min(32768, tokens+1024)
  replis : nominal → apres_gc_force → flash_off → sans_mmproj
           (vision garde TOUJOURS son mmproj — fix 09/09)
  OOM si "Failed to load model" OU VRAM basse → 32768→16384→8192 (plancher 4096/2048)
```

## 3. Extraits réels

```python
# backend/main.py — handler C08
run_id = request_id  # 1:1 Phase 1
# persist_user_turn AVANT LLM, terminal unique APRÈS persistance
```

```python
# backend/inference/media_policy.py — R2 en 3 couches OR
EXPLICIT_REFERENCE_TERMS = ("cette image", "dans l'image", ...)
REVISIT_VERBS = ("revois", "regarde à nouveau", ...)
VISUAL_DETAIL_TERMS = ("couleur", "cheveux", "fond", "tient", ...)
# + OCR_REQUEST_TERMS unifié ("lire ... écrit en japonais" → doc_ocr, pas vision)
```

```python
# backend/inference/loop.py — garde-fous tour
MAX_ITER = 5; TOOL_TIMEOUT = 15; TOTAL_TIMEOUT = 300
# cache (tool, artifact_id) anti-doublon, available requis
```

## 4. Manager VRAM (pseudo-code)

```
use_model(nom) :
  acquérir admission (1 seul run lourd, sinon attendre)
  _wait_for_vram_release(3s) + sleep déchargement (0.5s + attente 5s)
  n_ctx = sur VRAM *courante* (plus cache détecteur) : ≥12GB →131072 sinon min(32768, tokens+1024)
  essayer stratégies : nominal → apres_gc_force → flash_off → sans_mmproj
    MAIS vision garde toujours son projector (fix 09/09 : repli OOM-partiel ne coupe plus mmproj)
  SI _looks_like_oom("Failed to load model" OU VRAM basse) → n_gpu_layers réduit + n_ctx /2
    (32768→16384→8192, plancher 4096/2048)
  _generation++ invalide les handles après switch vision (C07)
```

## 5. Tests

`test_media_pipeline.py` 21 (plomberie sans modèle) ·
`test_media_policy.py` 23 (politique + régressions q-r.md T2/T4 + list-all) ·
`test_prompt_tool_parity.py` 8 (prompt ↔ schémas) ·
`test_manager_oom.py` 13 (VRAM basse, offload partiel) ·
`test_loop_sanitizer.py` 6 (`INVALID_DECISION`).
