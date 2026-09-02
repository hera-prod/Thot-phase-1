# Contexte — Thot Phase 1 (fichier de reprise)

> **Dernière mise à jour :** 02 septembre 2026 — 01h30
> **Projet actif :** `Thot-phase-1/` (standalone, copie élaguée de `Thot v0.8`, **jamais** modifié `Thot v0.8/`)
> **Dépôt :** `hera-prod/Thot-phase-1` — branche `main` (dernier `9f5543c` → `c1e3a91` fix DB unique)
> **Pour reprendre :** lire ce fichier seul suffit. Détail exhaustif dans `Thot-phase-1/Notes.md` (272 lignes) et `Futur/Notes.md` (772 lignes). Spec vivante : `Futur/archive/Réponses.md` (n°2→n°11).

---

## 1. Objectif Phase 1 — figé

- **Chat seul + historique** (pas `agent` séparé, pas `spaces/gallery/images/settings`)
- **Toolcalls VLM directs** dans le chat : `vision` (LFM2.5-VL-3B) + `doc_ocr` (GLM-OCR) + `get_source` + `read_text` — **orchestrateur sémantique** (LFM2.5-8B) décide, pas de routeur mots-clés
- **Backend 100% IPC** : `Frontend --invoke("chat_send")--> Rust --stdin JSON--> Python (llama-cpp-python) --stdout JSON Lines--> Rust --window.emit--> Frontend` (pas HTTP/SSE)
- **Local** : `~/.local/share/thot/data/thot-phase-1.db` + `~/.local/share/thot/media/{sha256}.ext` (4 tables → enrichie `sources`), `<8 GB VRAM` séquentiel, `PyInstaller` sidecar `thot-sidecar-x86_64-unknown-linux-gnu` 7.8 MB
- **Règle** : 1 modèle en VRAM à la fois, `Thot v0.8 (Copie)` reste backup.

---

## 2. Stack

- **Frontend** : React 19 + Vite 1420 `strictPort` + Tauri 2 (`@tauri-apps/api` `invoke`/`listen` + `getCurrentWebviewWindow().onDragDropEvent`)
- **Rust** : `src-tauri/src/lib.rs` (`CommandChild` stdin/stdout, `BufReader::lines() → window.emit`), `tauri.conf.json` `externalBin: ["binaries/thot-sidecar"]`, `capabilities/default.json` `shell:allow-execute`
- **Python** : `llama-cpp-python==0.3.34` figé (0.3.35 SIGILL H370), `sqlalchemy 2`, `pymupdf`, `pillow`, `pyinstaller`, `pytest`
- **DB** : SQLite `WAL` + `foreign_keys=ON` — `backend/db/database.py` lit `THOT_DB_PATH` env (Rust source vérité) sinon `~/.local/share/thot/data/thot-phase-1.db`
- **Build** : `pyproject.toml` / `requirements.txt` (`pip --break-system-packages`, `python3` pas `python`), `cargo check` OK

---

## 3. Modèles — présents

`Thot-phase-1/models/` (7 fichiers) :

- `LLM/LFM2.5-8B-A1B-UD-Q4_K_M.gguf` 5.0G (8.3B total, MoE 32 experts top-4, 128K natif → adaptatif `≥12GB →131072 sinon min(32768, tokens+1024)`, `supports_vision=False`, temp 0.2)
- `Vision-document/GLM-OCR-f16.gguf` 1.7G + `mmproj-GLM-OCR-Q8_0.gguf` 484M (8K, `supports_vision=True`, `flash_attn=False` #19721)
- `Vision-general/LFM2.5-VL-3B-UD-Q4_K_XL.gguf` 1.6G + `mmproj-*-BF16.gguf` 856M (+ F16 854M, Q8 583M) (3.1B dense, 32K, `supports_vision=True`, `flash_attn=True`)

**Gestion mémoire** : `manager.use_model(name)` séquentiel, `del + close + gc + _wait_for_vram_release(3s)` + `torch.empty_cache()`, borne 7.5GB, `mmap` 300ms, `KV Q8_0` (`type_k/v=8`).

---

## 4. Architecture IPC + Loop

```
Frontend --invoke chat_send{request_id, message, mode, image_paths, conv_id}--> Rust --stdin {"id","method":"chat.send","params"}--> Python backend/main.py
Python --stdout {"event":"thinking|tool_call|tool_result|answer|execution_plan|message_completed","payload":{request_id,...}}--> Rust --window.emit--> Frontend onThinking/onAnswer/...
Fichiers: save_upload Rust (5M image/20M PDF, magic 89PNG/FFD8FF/%PDF/RIFF-WEBP, sha256) → /tmp/thot/{request_id}/{hash}.ext → cleanup_request en finally → promote vers ~/.local/share/thot/media/{sha256}.ext
```

`backend/inference/loop.py` (650L) :

- **Registry** : `build_attachment_registry(conv_id, image_paths)` → `[{id, ordinal, kind, name, path, message_index, available, previous_analyses:[{tool,status,summary}]}]` depuis `Source` + `ToolCall` + `media/{sha256}.ext exists`, `format_attachment_registry()` → `SOURCES DISPONIBLES … + JSON` injecté comme **2e bloc système** avant `history` (`messages_orch = [SYSTEM_ORCHESTRATOR, registry_block] + history + [user_content]`).
- **Tools** : `has_any_media = bool(registry)` → `tool_defs = registry.definitions() if has_any_media else []` (4 outils : `vision, doc_ocr, get_source, read_text`, `tool_choice auto`). Plus de `select_attachment_tool` forcé ni `skip_orchestrator` — l'orchestrateur décide.
- **Exécution** : `MAX_ITER 5, TOOL_TIMEOUT 15, TOTAL_TIMEOUT 300, MAX_RETRY 1`, `_execute_vision` (base64 `data:image/jpeg` après `prepare_vision_image 1536px JPEG88`, `VL-3B 32K`) / `_execute_doc_ocr` (200 DPI page par page, `Text/Table/Formula Recognition:`), `_execute_get_source/_read_text` (DB `Source`), `tool_results` en mémoire → `_prepare_tool_results (≤8K brut, 8-16K tronqué, >16K résumé LLM 500 mots)` → `_format_tool_observation` → `brief` → `Générateur` (8B, jamais tools, `obs_block`).
- **Historique** : `history_shared` `user↔assistant` seul (jamais tool traces passées), `execution_plan` émis, `sanitizer backend` `<think>` + `[vision(...)]` textuel (ticket #23852).
- **Compaction** : sur historique partagé, `40K prune / 85% (109K) compact / 15K preserve`.

---

## 5. DB & Persistance

`backend/db/models.py` :

- `conversations(id, title, mode, created_at, updated_at, messages→CASCADE)`
- `messages(id, conv_id, role, content, thinking, run_id, status, tokens_in/out, created_at, toolcalls/sources)`
- `toolcalls(id, msg_id, tool_call_id, tool, args JSON, result, ok, latency_ms)`
- `sources(id, msg_id, conversation_id FK, source_type, path, hash, media_kind, original_name, mime_type, sha256, relative_path, size_bytes, parent_source_id, analysis_version, created_at, last_accessed_at)` + `idx_sources_conversation_created, idx_sources_sha256`

**Source vérité DB** : Rust `resolve_db_path(app: AppHandle) -> app_data_dir/data/thot-phase-1.db` (`~/.local/share/com.thot.phase1` en prod), `setup` spawn sidecar avec `env THOT_DB_PATH + THOT_APP_DATA_DIR` (debug `python3 -m backend.main` et release `sidecar`). Python `database.py` lit `THOT_DB_PATH` prioritaire (`Path(env).expanduser().resolve()`), log `Thot DB path: ...` des deux côtés (`println!` + `log.info`). `list_conversations/get_conversation/list_messages` prennent `app: AppHandle` et appellent `resolve_db_path`.

**Persistance** : `backend/db/media_cache.py` `get_media_root()=~/.local/share/thot/media`, `persist_attachment(tmp_path)` → `media/{sha256}.ext` (hash 64 hex, `copy2` si absent). `backend/main.py` `handle chat.send` : `ensure_conversation(convId)` → `append_message(user)` → `persist_attachment` pour chaque `image_paths` → `Source(..., conversation_id, sha256, relative_path)` → `session.commit()` **avant** `run_agent_loop` (survit crash Vision) → après génération `append_message(assistant) + ToolCall` → `commit`.

**Cycle** : `/tmp/thot/{request_id}/{hash}.ext` (transit, supprimé après `message_completed`) → `~/.local/share/thot/media/{sha256}.ext` (persistant, `get_source` → `working_path`).

---

## 6. Manager VRAM

`backend/inference/manager.py` (251L, fix n°9) :

- `__init__` : `_current_model/_chat_handler/_name`, `HardwareDetector`, `_min_required_vram_mb=6000`
- `_get_free_vram_mb()` : `nvidia-smi --query-gpu=memory.free` → `pynvml` → `backend.free_vram`
- `_wait_for_vram_release(3s)` : poll `free >= min` sinon `warning`
- `use_model(name)` : si même modèle → `yield`, sinon `VRAM avant X Mo` → `_unload_current()` → `VRAM après Y Mo` → `_load_model`
- `_unload_current()` : `self._current_* = None` **avant** `del`, `if hasattr(model,"close"): close() else del`, `del handler`, `gc.collect()`, `_wait_for_vram_release()`, `torch.empty_cache()`
- `_build_llm_kwargs` : vérifie `llama_cpp.llama_chat_format`, `if supports_vision` → **fatal** si `mmproj` absent/fichier manquant ou `chat_handler` None, `handler = _build_vision_handler(cfg, mmproj)` → `kwargs["chat_handler"]=handler`, stocke `self._current_chat_handler`
- `_build_vision_handler` : `ggml-vision-chat → [LFM2VL, Qwen25VL, Llava15, Llava16, MTMD]` (0.3.34 n'a pas LFM2VL → fallback `Llava15ChatHandler(clip_model_path=mmproj)` testé OK), `ggml-ocr-chat → [Qwen25VL, Llava15, MTMD]`
- `_load_model` : `LOAD_STRATEGIES = [nominal, apres_gc_force 0.5s, flash_off, sans_mmproj]` + `pre_delay gc+wait`, si OOM déjà vu → `n_gpu_layers réduit` via `estimate_layers(5GB→1GB, 24)`

---

## 7. Prompts

- `orchestrator.py` : `SYSTEM_ORCHESTRATOR` = "Tu es Thot Orchestrateur… Des fichiers peuvent être disponibles (voir SOURCES DISPONIBLES). Si répondre nécessite détail visuel précis — qu'il s'agisse d'un fichier tout juste envoyé (first_seen=current) avec 'Qu'en penses-tu ?' ou d'un fichier plus ancien 'là, sa peau, tatouage' — appelle get_source si nécessaire puis vision/doc_ocr avant de répondre, même si déjà décrit. Ne considère jamais description antérieure comme preuve à jour."
- `generator.py` : `SYSTEM_GENERATOR` = 5 règles + **Règle preuve visuelle** : "Tu ne peux affirmer avoir vérifié... que si tour courant contient observation Vision/OCR réussie... Une description antérieure n'est pas vérification... Si aucune source résolue et question dépend visuel, demande clarification."
- `vision.py` : `SYSTEM_VISION` = 3-5 phrases factuelles, "Réponds dans la langue de l'utilisateur" + `prepare_vision_image 1536 JPEG88`
- `ocr.py` : `OCR_TEXT/TABLE/FORMULA_MARKER`, `resolve_ocr_task(query)` → `table/formula/text`, `build_system_ocr(*, task: str)` → `"Text Recognition:"` etc, `SYSTEM_OCR = build_system_ocr(task="text")`
- `compaction.py` : `[Résumé] + recent 15K`

---

## 8. Tools

`backend/tools/registry.py` : `ToolRegistry` 4 outils :

- `vision` (`VisionGeneralTool`) : `image_path, query, image_hash?` — LFM2.5-VL-3B 32K, description sémantique "Analyse visuelle... y compris image déjà envoyée... n'est jamais preuve suffisante... rappelle plutôt que répondre de mémoire."
- `doc_ocr` (`VisionOCRTool`) : `pdf_path, query?, image_hash?` — GLM-OCR 8K, "Extraction fidèle... y compris fichier déjà envoyé... Ne réponds jamais sur présence/absence de texte sans avoir appelé."
- `get_source` (`GetSourceTool`) : `source_id, reason` — retrouve `Source` historique, `working_path` persistant, `last_accessed_at` update, `max_output 2000`
- `read_text` (`ReadTextTool`) : `source_id, purpose, max_chars 500-30000` — lit TXT/MD/JSON/CSV/code UTF-8 tronqué, erreur si image/pdf (→ vision/doc_ocr)

`ToolPermission`, `ToolContext(conversation_id, image_paths, readonly_db, run_id)`, `ToolResult(ok, content, error, display_summary)`, `BaseTool(name, description, input_model, timeout 15, max_output)`.

---

## 9. Frontend

- **Copié v0.8** : `main.tsx`, `styles/tokens.css/global.css`, `components/ui/Toast,Tag,Button`, `components/shell/AppShell/Sidebar/TitleBar` (nav `Chat` seul), `components/composer/Composer`, `pages/chat/ChatHome/ChatConversation`, `pages/history/HistoryPage`
- **Créé** : `components/chat/ToolCallDisplay.tsx` ( `vision → Vision générale · LFM2.5-VL-3B · 215 car. · 4.70s`, expand `Requête, Source hash 12, Résultat technique` ; `get_source/read_text` labels), `AssistantTurn.tsx` (1 bulle/ tour, `phase` `preparing/orchestrating/tool_running/generating`, `activity`), `features/attachments/*` (`attachmentTypes`, `attachmentValidation 4/5M/20M`, `AttachmentDraftContext` Provider au-dessus Router (survit `ChatHome→ChatConversation`), `useAttachmentDrop`, `AttachmentTray 72×72`, `useNativeFileDrop` stub), `ChatConversation.css` (grid 820+260), `Composer.css` (drag animation)
- **Fix visuels** : `vite.config 1420 strictPort`, `Thot_logo.svg`, `maxWidth 820` (960→820), bulles `surface-1 minHeight 48`, `Composer 820 sticky`
- **State** : `ChatConversation` `ChatState idle|preparing|orchestrating|tool_running|generating`, `isBusy = !==idle`, `debugMode = localStorage thot_debug===1` (réflexion dev-only, `details` "Orchestrateur — Plan d'exécution"), `execution_plan` listener, `bottomRef` scroll, `showRail` (detailsRail sticky, `@media 1040px display:none`)
- **Layout scroll** : `html,body,#root 100% overflow hidden`, `.appShell flex 100dvh`, `.chatPage grid auto minmax(0,1fr) auto`, `.messagesViewport overflow-y auto`, `.conversationLayout grid minmax(0,820px) 260px`

---

## 10. Drag & Drop

**Choix final** : **Tauri natif pur** pour desktop (`tauri.conf.json dragDropEnabled: true`, redémarrage `cargo tauri dev` requis) — HTML5 `File[]` garde fallback Vite dev.

- `App.tsx` : `AttachmentDraftProvider` + `GlobalDragGuard` (`dragover/drop preventDefault` sans `stopPropagation`)
- `Composer.tsx` : `isDraggingGlobal` (window `dragenter/leave/over/drop` → `setDraggingFiles`) + `isDraggingOverComposer` (local counter `dragCounterRef` + `dataTransfer.types Files`) → `composerShell` / `composerShellAmbientHighlight` pulse 1.4s / `composerShellActiveDrop` scale 1.015 dashed gold + `composerDropOverlay` (＋ "Déposez ici") / `pageDragAmbientOverlay` ("Déposez votre fichier dans la barre de saisie" fixed bottom 140px) — `@media prefers-reduced-motion`
- **Tauri natif** : `getCurrentWebviewWindow().onDragDropEvent` → `payload.paths[]` → `invoke("stage_dropped_files", {paths, draftId})` (Rust `stage_dropped_files` copie vers `drafts/{draftId}/{sha256}.ext` + `magic bytes`/`size` check, retour `stagedPath, hash, preview`) → `AttachmentDraftProvider` → preview épinglée **avant** `Envoyer` (jamais auto-send, `×` supprime staged + `revokeObjectURL`), dedup `lastNativeDropRef` 250ms (`Date.now() - lastNativeDrop <250 → return`) pour DOM vs natif
- **Validation** : même `processFiles` + `validateAttachments` (4 / 5M / 20M) pour `ChatHome` (overlay grand "Déposez une image ou un PDF") et `ChatConversation` (doré compact), `Composer` barre toujours cible visuelle (`pointer-events none` overlay)

---

## 11. Correctifs Réponses.md n°2→n°11 — résumé

- **n°2** : mmproj 0 → 856M BF16 + 484M, `MODEL_CONFIGS mmproj/chat_handler/flash_attn`, `manager` mmproj+handler, `dragDropEnabled false` (devenu true n°11), `onPaste`, `build_system_ocr`
- **n°3** : placeholder `Composer:314` `'n\'importe' + }i…` → `"Que souhaitez-vous analyser ?"` (build bloquant), `handlePaste files`, `ChatState orchestrating/tool_running`
- **n°4** : `SYSTEM_*` langue utilisateur, `ToolCallDisplay` modèle, `resolve_n_ctx` adaptatif
- **n°5** : `llama_chat_handler` → `llama_chat_format`, fallback `Llava15`, drag animation `composerShell`
- **n°6** : `build_system_ocr(*, task)` + `resolve_ocr_task`, `resolve_allowed_tools` + `tool_defs` filtrés, `skip_orchestrator` (vision→générateur), `global.css` grid, `execution_plan`, `lastNativeDropRef` dedup
- **n°7** : `Source` enrichie `conversation_id/media_kind/sha256/relative_path`, `media_cache` `~/.local/share/thot/media`, `requires_media_reinspection` + `_get_past_source_path` ordinal, `generator` règle preuve visuelle, `tauri dragDropEnabled true`
- **n°8** : **retrait routeur mots-clés** → `build_attachment_registry` + `format_attachment_registry` injecté 2e bloc système, `has_any_media → both tools`, `vision/doc_ocr` descriptions sémantiques, `SYSTEM_ORCHESTRATOR` fiabilité, `get_source/read_text` 4 outils, `get_source→vision` 2 itérations
- **n°9** : VRAM leak `torch.empty_cache` inutile → `_current_chat_handler`, `_get_free_vram_mb` `nvidia-smi/pynvml`, `_wait_for_vram_release`, `close() avant del`, `LOAD_STRATEGIES`
- **n°10** : manifest riche `ordinal/previous_analyses/available`, `Source + conversation_id`, `persist` avec `conversation_id`, `ToolCallDisplay` labels `get_source/read_text`
- **n°11** : **DB unique** `resolve_db_path(app_data_dir/data/thot-phase-1.db)` source vérité, `THOT_DB_PATH` env au sidecar, `list_*` via `app: AppHandle`, `persist_user_turn` avant LLM (`commit` avant `run_agent_loop`), logs `Thot DB path` des deux côtés (`cargo check` ✓)

---

## 12. Fichiers clés

- `Thot-phase-1/backend/main.py` — sidecar JSON Lines, `persist_user_turn` avant LLM
- `backend/inference/loop.py` — registry + orchestrator sémantique, `prepare_vision_image 1536`, `VISION_MULTIMODAL_NOT_INITIALIZED`
- `backend/inference/manager.py` — séquentiel + VRAM, `build_vision_handler` fallback
- `backend/prompts/*` — orchestrator/generator/vision/ocr/compaction
- `backend/tools/*` — registry 4 outils
- `backend/db/*` — `models.py` enrichie, `database.py` `THOT_DB_PATH`, `media_cache.py`
- `frontend/src/lib/tauri.ts` — `chatSend/saveUpload + onExecutionPlan`, `frontend/src/features/attachments/*`
- `src-tauri/src/lib.rs` — `resolve_db_path`, `save_upload`, `stage_dropped_files` (à compléter), `chat_send`
- `models/` — 3 GGUF + 4 mmproj
- `Notes.md` (272L) + `Futur/Notes.md` (772L) — specs, `archive/Réponses.md` — spec vivante

---

## 13. Commandes

```bash
cd "Thot-phase-1"
pip install --break-system-packages -r requirements.txt  # 0.3.34 cu121 si besoin --extra-index-url https://abetlen.github.io/llama-cpp-python/whl/cu121 --force-reinstall

python3 -m pytest backend/tests/ -v  # 5 passed (hardware, manager, loop_sanitizer, ocr_policy 8, manager_switch 1)
python3 -c "from backend.inference.hardware import HardwareDetector; print(HardwareDetector().detect())"  # cuda 4070 SUPER 12.8G
echo '{"id":"test","method":"chat.send","params":{"message":"Bonjour","mode":"chat"}}' | python3 -m backend.main
python3 -m backend.tests.manual.test_lfm25_vl  # smoke VLM red_square → rouge

cargo check --manifest-path src-tauri/Cargo.toml
npm --prefix frontend install && npm run build  # 242 modules 303kB
npm run tauri dev  # Vite 1420 + Tauri (redémarrage requis après tauri.conf dragDropEnabled changement)
# Vérif DB unique
find ~/.local/share -name "thot-phase-1.db" | xargs ls -lh
sqlite3 ~/.local/share/thot/data/thot-phase-1.db ".tables"  # conversations messages sources toolcalls
sqlite3 ~/.local/share/thot/data/thot-phase-1.db "SELECT id,title FROM conversations ORDER BY updated_at DESC LIMIT 5;"
```

---

## 14. Tests

- `test_hardware.py` (mock nvidia-smi)
- `test_manager.py` (sequential, supports_vision guard)
- `test_loop_sanitizer.py` (ticket #23852 `<think>` + `[vision(...)]`)
- `test_ocr_policy.py` 8 tests (`build_system_ocr_text/table/formula`, `image→vision` etc — dépréciés mais passent)
- `test_manager_switch.py` (VL→8B mock close)
- `manual/test_lfm25_vl.py` (red_square 64px → "rouge", no 1.8M tokens)
- **P2 prévus** : `DIRECT_ANSWER vs TOOL_CALL` comportementaux (5 cas spec n°8), `persisted_attachment_survives_tmp_cleanup`, `history_reads_same_db`

---

## 15. État actuel & Next P0

- **Build** : `vite build` 242 modules 303kB ✓, `cargo check` ✓, `py_compile` ✓, `pytest` 9 passed
- **Vision** : 215 car. 4.70s OK (VLM chargé, handler `Llava15` fallback, mmproj BF16) — mais confond Spike/Luffy, suit Wakfu sans preuve (besoin réanalyse via `get_source` + `registry`)
- **Persistance** : `c1e3a91` devrait fixer Historique vide (à confirmer `sqlite3` après 1er message)
- **Drag** : natif pur `true` + `onDragDropEvent` + `stage_dropped_files` Rust à valider (logs `[native-drop]`, `stage_dropped_files count`)
- **Next P0** : tester `cargo tauri dev` → image + "Qu'en penses-tu ?" → `vision` via registre, puis "tatouage sur sa peau" → `get_source(src_001) → vision`, vérifier `Source` DB et Historique après restart, ajuster `SYSTEM_ORCHESTRATOR` si `DIRECT_ANSWER` persiste.

---

*Ce fichier remplace `Contexte.md` du 30/08 (agent 1 couche). Pour tout détail, `Thot-phase-1/Notes.md` et `Futur/Notes.md` font foi.*
