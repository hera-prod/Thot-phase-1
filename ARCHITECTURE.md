# Architecture — Thot Phase 1 (pipeline global)

> Lire en premier pour une reprise. Détail par bloc dans les `README.md` de chaque dossier.

## 1. Objectif Phase 1 (figé)

Chat seul + historique. Toolcalls VLM directs dans le chat (`vision` + `doc_ocr` + `get_source` + `read_text`).
Orchestrateur sémantique (LFM2.5-8B) décide via registry, jamais de `[vision(...)]` textuel.
Backend 100% IPC, local portable, 1 modèle en VRAM à la fois.

## 2. Dossiers englobés

```
Thot-phase-1/
├── ARCHITECTURE.md          ← ce fichier (pipeline global)
├── backend/inference/README.md  ← loop + manager + media_policy + ipc + prompts
├── backend/tools/README.md      ← vision / doc_ocr / get_source / read_text
├── backend/db/README.md         ← SQLite 12 tables + blobs/artifacts
├── src-tauri/README.md          ← pont Rust : staging, sidecar, portable
├── frontend/README.md           ← React : chat, composer, drag&drop, historique
├── models/README.md             ← stockage modèles + data/ portable + build
├── Contexte.md / Notes.md / q-r.md / SPEC.md  ← mémoire projet (pas du code)
```

## 3. Pseudo-code global (comment c'est fait)

```
# ENTRÉE UI (drag / trombone / collage)
fichier_utilisateur
  → Rust.stage_dropped_paths({request_id, paths})
      OU Rust.stage_picker_bytes({request_id, filename, bytes})
  → {stageId, items:[{stageItemId: sti_..., sha256, mimeDetected, preview:{token}}]}
  → brouillon React (vignette via convertFileSrc / objectURL)

# PROMOTION (durable avant le run)
SI utilisateur envoie le message :
  React.attachments.prepare({conversation_id, stage_item_ids})
    → Rust.attachments_prepare
    → Python : data/temp/staged/{hash}.ext → data/blobs/sha256/ab/...bin
    → retourne artifact_ids: [art_...]

# CHAT (un seul tuyau IPC, pas de HTTP)
React.chatSend({message, mode, attachment_ids:[art_...], conv_id})
  --invoke--> Rust.chat_send
  --stdin JSON Lines--> Python.backend/main.py
  --stdout JSON Lines--> Rust.window.emit
  --listen--> React.on* (thinking / tool_call / answer / completed)

# CÔTÉ PYTHON (Phase 0 → orchestrateur → générateur)
Phase 0  : plan_media_tools() DÉTERMINISTE (sans LLM, cf. inference/README)
           nouveau fichier → observer / follow-up ancré → réobserver
Tour LLM : orchestrator (avec tool_defs SI média) → tool_call → exécuteur
           → brief 6 champs → generator (SANS tools, SANS chemins)
Persist  : Message + MessageAttachment + ToolCall + Observation AVANT emit terminal
```

## 3bis. Schémas ASCII (vue d'ensemble)

```
┌──────────┐  drag/trombone/collage   ┌────────┐  stage   ┌──────────────┐
│ FRONTEND │ ────────────────────────▶ │  RUST  │ ───────▶ │ data/temp/   │
│  React   │                           │ lib.rs │  sti_...│ staged/{h}   │
└────┬─────┘                           └───┬────┘          └──────┬───────┘
     │ envoyer                             │ prepare              │ promotion
     │ attachment_ids                      ▼                      ▼
     │                               ┌──────────┐          ┌──────────────┐
     │                               │ Artifact │◀─────────│ data/blobs/  │
     │                               │ art_...  │          │ sha256/ab/.. │
     │                               └────┬─────┘          └──────────────┘
     ▼                                      │
┌──────────────────────────────────────────────────────────────────────┐
│ CHAT — un seul tuyau IPC (pas de HTTP/SSE)                           │
│                                                                      │
│  React.chatSend ──invoke──▶ Rust.chat_send ──stdin JSONL──▶ Python   │
│                                                                      │
│   {message, attachment_ids, conv_id}    {id, method:"chat.send"}      │
│                                                                      │
│  React.on* ◀──emit── Rust.window.emit ◀──stdout JSONL── Python.emit  │
│                                                                      │
│   thinking → tool_call → tool_result → answer → completed             │
│   {event, payload, protocol_version:1, run_id, seq++}                 │
└──────────────────────────────────────────────────────────────────────┘
     ┌─────────┐   Phase 0 (sans LLM)   ┌──────────────┐
     │ PYTHON  │ ─────────────────────▶ │ media_policy │
     │ loop.py │  plan déterministe R1  │  R1/R2/R3    │
     └────┬────┘  nouveau→observer      └──────────────┘
          │       follow-up ancré→réobserver
          ▼
     ┌─────────────┐  tool_call   ┌────────┐  brief 6ch   ┌───────────┐
     │ ORCHESTR.   │ ────────────▶ │ TOOLS  │ ───────────▶ │ GENERATOR │
     │ LFM2.5-8B   │  max 5 iter   │4 outils│  SANS tools  │ LFM2.5-8B │
     └─────────────┘               └────────┘             └─────┬─────┘
                                                               │ persist AVANT terminal
                                                               ▼
                                                        ┌─────────────┐
                                                        │ SQLite 12T  │
                                                        │ + blobs     │
                                                        └─────────────┘
```

```
ENVELOPPE IPC (à ne pas casser)
  stdin  : {"id":"...","method":"chat.send","params":{...}}
  stdout : {"event":"answer","payload":{...},"protocol_version":1,"run_id":"...","seq":3}
  run_id = request_id (1:1 Phase 1) · seq croissant · 1 seul terminal/run
```

## 4. Extraits réels (contrats à ne pas casser)

Envoi chat (frontend → Rust → Python) :
```ts
// frontend/src/lib/tauri.ts
chatSend({ request_id, message, mode, attachment_ids, conv_id })
// Rust mappe snake_case -> camelCase : request_id -> requestId
```

Ligne IPC Python (stdin → stdout) :
```json
{"id":"test","method":"chat.send","params":{"message":"Bonjour","mode":"chat"}}
{"event":"answer","payload":{"text":"..."},"protocol_version":1,"run_id":"...","seq":3}
```

Règle VRAM :
```
1 modèle en VRAM à la fois (FSM unloaded→loading→ready→running→unloading/failed)
admission = sémaphore 1 run lourd (cf. backend/inference/README.md)
```

## 5. Pièges connus

- `image_paths` = compat dépréciée (log `[compat]`), cible = `attachment_ids` (`art_...`).
- `[vision(...)]` textuel = jamais exécuté → `INVALID_DECISION` + strip sanitizer.
- VLM sans contexte LLM → `clean_media_query()` strippe la déixis avant SYSTEM_VISION/OCR.
- SKIP test VL manuel si < 6 Go libres (conflit 8B ≈ 9 Go entre 2 processus, pas un bug).
