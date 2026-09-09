# Contexte — Thot Phase 1 (fichier de reprise)

> **Dernière mise à jour :** 08 septembre 2026 — 18h00
> **Projet actif :** `Thot-phase-1/` (standalone, copie élaguée de `Thot v0.8`, **jamais** modifié `Thot v0.8/`)
> **Dépôt :** `hera-prod/Thot-phase-1` — branche `main` (`23fd6cd` docs audit, local `c4f2ed0` + fondations portables 08/09)
> **Référence annexes :** `Depot_Github/annexes/10_pipeline_pieces_jointes_tauri_ipc_python.md` (cible), `09_import_drag_and_drop.md` (machine à états), `q-r.md` (16-19 répondu)
> **Pour reprendre :** lire ce fichier seul suffit. Détail dans `Thot-phase-1/Notes.md`, `Depot_Github/documentation/*`, `q-r.md`.

---

## 1. Objectif Phase 1 — figé

- **Chat seul + historique** (pas `agent` séparé, pas `spaces/gallery/images/settings`)
- **Toolcalls VLM directs** dans le chat : `vision` (LFM2.5-VL-3B, `artifact_id`) + `doc_ocr` (GLM-OCR) + `get_source` + `read_text` — **orchestrateur sémantique** (LFM2.5-8B) décide via `registry`, pas de routeur mots-clés, jamais de `[vision(...)]` textuel (`C04`, Lot 1)
- **Backend 100% IPC** : `Frontend --invoke--> Rust --stdin JSON Lines--> Python (llama-cpp-python) --stdout JSON Lines--> Rust --window.emit--> Frontend` (pas HTTP/SSE), `protocol_version:1`, `run_id/seq` versionnés `C08`
- **Local portable** `Thot/data/` (`database/blobs/previews/derived/temp/trash`) si `portable.json` présent, sinon `~/.local/share/thot/data` (`C19`, `portabilite.md`), `<8 GB VRAM` séquentiel, `PyInstaller` sidecar `thot-sidecar-x86_64-unknown-linux-gnu`
- **Règle** : 1 modèle en VRAM à la fois (FSM `unloaded→loading→ready→running→unloading/failed`, `admission` 1 run lourd `C07`), `Thot v0.8 (Copie)` reste backup.

---

## 2. Stack

- **Frontend** : React 19 + Vite 1420 `strictPort` + Tauri 2 (`@tauri-apps/api` `invoke`/`listen` + `getCurrentWebviewWindow().onDragDropEvent`, `convertFileSrc` pour preview `10`)
- **Rust** : `src-tauri/src/lib.rs` (`CommandChild` stdin/stdout, `resolve_temp_dir`/`resolve_data_dir` portable, `rusqlite 0.31` pour `list_conversations` sans `python3 -c`), `tauri.conf.json` `dragDropEnabled:true`, `csp` restrictive, `externalBin: ["binaries/thot-sidecar"]`, `capabilities/default.json` `shell:allow-execute`
- **Python** : `llama-cpp-python==0.3.34` figé, `sqlalchemy 2`, `pymupdf`, `pillow`, `pyinstaller`, `pytest`
- **DB** : SQLite `WAL` + `foreign_keys=ON` — `backend/db/database.py` lit `THOT_DB_PATH`/`THOT_DATA_DIR`/`THOT_APP_DATA_DIR` env (Rust source vérité) sinon `get_data_dir()/database/thot-phase-1.db` (portable `data/database` sinon XDG), `init_db` crée 12 tables
- **Build** : `pyproject.toml` / `requirements.txt` (`pip --break-system-packages`, `python3`), `cargo check` OK, `portable.json` + `distribution.json` + `licenses/` versionnés

---

## 3. Modèles — présents

`Thot-phase-1/models/` (7 fichiers) + `data/models` portable :

- `LLM/LFM2.5-8B-A1B-UD-Q4_K_M.gguf` 5.0G (8.3B MoE 32 top-4, 128K natif → adaptatif `≥12GB →131072 sinon min(32768, tokens+1024)`, `supports_vision=False`, temp 0.2)
- `Vision-document/GLM-OCR-f16.gguf` 1.7G + `mmproj-GLM-OCR-Q8_0.gguf` 484M (8K, `supports_vision=True`, `flash_attn=False`)
- `Vision-general/LFM2.5-VL-3B-UD-Q4_K_XL.gguf` 1.6G + `mmproj-*-BF16.gguf` 856M (+ F16 854M, Q8 583M) (3.1B dense, 32K, NaFlex 512×512 patches + thumbnail, `supports_vision=True`, `flash_attn=True`)

**Gestion mémoire** : `manager.use_model(name)` FSM + `admission` sémaphore, `_wait_for_vram_release(3s)` + `torch.empty_cache()`, `mmap` 300ms, `KV Q8_0`, `_generation` invalide handles après vision `C07`.

---

## 4. Architecture IPC + Loop (annexe 10)

```
Entrée UI (drag/trombone/collage/capture)
  → staging Rust stage_dropped_paths({request_id,paths}) / stage_picker_bytes({request_id,filename,bytes})
    → {stageId, items:[{stageItemId:sti_..., sha256, mimeDetected, preview:{token}}], rejected}
  → brouillon React stageItemId + previewToken (convertFileSrc)
  → attachments.prepare({conversation_id, stage_item_ids}) → Rust attachments_prepare → Blob/Artifact/ArtifactLink
  → chat.send {id, method:"chat.send", params:{message, mode, attachment_ids:[art_...], conv_id}} --stdin JSON Lines--> Python
  → Python --stdout {event, payload, protocol_version, run_id, seq} --> Rust --window.emit--> Frontend on*
Fichiers: data/temp/staged/{hash}.ext (volatil) → data/blobs/sha256/ab/...bin (durable) → preview data/previews
```

`backend/inference/ipc.py` (212L) : `writer_lock` `stdout` unique, `seq` croissant par `run_id`, `protocol_version` validation, `MAX_LINE 256KB`, `MAX_QUEUE 32` backpressure, lecteur dédié → queue → worker 1 run lourd (annulation `C09` possible pendant OCR).

`backend/inference/loop.py` (880L) + `SharedContext(revision)` `C03` :
- **Registry** : `attachmentRegistry` depuis `Artifact/Blob` si `attachment_ids` (cible annexe 10) sinon `image_paths` compat, `format_attachment_registry()` injecté 2e bloc système, `tool_defs` si `has_any_media`.
- **Tools** : `vision.inspect` (`artifact_id` + `query`) + `doc_ocr` + `get_source` + `read_text` via `BaseTool.call` (validation Pydantic, jamais `[vision(...)]` textuel, `C04`), `MAX_ITER 5, TOOL_TIMEOUT 15, TOTAL_TIMEOUT 300`.
- **Sanitizer** Lot 1 : strip `<think>` + `[vision(...)]` **sans** réémettre pensée privée `C05`, `INVALID_DECISION` si textuel.
- **Compaction** `ContextSnapshot(revision, covered_through_message_id)` `C06` `prune` avant `SharedContext`, budget `count_tokens` (pas `len//4`), `8000/16000` + résumé LLM.
- **Vision** : `prepare_vision_image 1536px JPEG88` tracé `NaFlex 512`, `data:image/jpeg;base64` pour handler `LFM2.5-VL-3B`.

---

## 5. DB & Persistance (annexe 10)

`backend/db/models.py` 12 tables :

- `conversations(id, title, mode, created_at, updated_at)`
- `messages(id, conv_id, role, content, thinking, run_id, status, tokens_in/out)`
- `toolcalls(id, msg_id, tool_call_id, tool, args JSON, result, ok, latency_ms)`
- `sources(id, msg_id, conversation_id, source_type, path, hash, media_kind, sha256, relative_path, size_bytes)` (legacy `media/` enrichie)
- `runs(id, conv_id, request_id, kind, status, fingerprint, seq)` `C09/C11`
- `context_snapshots(id, conversation_id, revision, covered_through_message_id, summary)` `C06`
- `blobs(id, sha256 unique, relative_path blobs/sha256/..., size_bytes, mime_detected, state)` dédupliqués
- `artifacts(id:art_..., blob_id, kind, original_filename, display_name)`
- `artifact_links(id, artifact_id, scope_kind, scope_id, role)` `conversation|workspace|library` `C18`
- `message_attachments(id, message_id, artifact_id, ordinal)` `10:125`
- `artifact_cards(id, artifact_id, revision, summary)` + `observations(id, artifact_id, tool_name, status, payload_ref)` `10:441`

**Source vérité** : Rust `resolve_data_dir` (`portable.json` → `Thot/data` sinon XDG) + `resolve_db_path` (`data/database/thot-phase-1.db`), `setup` spawn sidecar avec `THOT_DATA_DIR/THOT_DB_PATH/THOT_APP_DATA_DIR`. Python `database.py` lit env prioritaire. `list_conversations` via `rusqlite` direct (plus `python3 -c`).

**Cycle** : `data/temp/staged/{hash}.ext` (volatil, `stage_item_id`) → `attachments_prepare` → `data/blobs/sha256/ab/...bin` (durable, adressé hash) + `data/previews` (reconstructible) → `MessageAttachment` ordre, survit `trash` 7 jours `10:545`, `purge` retire `ArtifactLink` puis GC blob si orphelin.

---

## 6. Manager VRAM

`backend/inference/manager.py` FSM `C07` + `threading` : `_state/_generation/_admission` sémaphore 1, `acquire_admission/release_admission`, `_set_state`, `_load_model` 4 stratégies `nominal/apres_gc_force/flash_off/sans_mmproj`, `mmap` etc.

---

## 7. Prompts

- `orchestrator.py` : `SYSTEM_ORCHESTRATOR` mention `SOURCES DISPONIBLES` + `artifact_id` (pas chemin), `vision` si détail visuel.
- `generator.py` : `SYSTEM_GENERATOR` 5 règles + preuve visuelle : ne reçoit que `brief` 6 champs + `evidence_id`/`artifact_id` + observations, jamais `tool_defs`/`chemin`/`think`/`[vision]` `10:463`.
- `vision.py` : `SYSTEM_VISION` 3-5 phrases + `prepare_vision_image 1536`
- `ocr.py` : `Text/Table/Formula Recognition` + `resolve_ocr_task`
- `compaction.py` : `[Résumé] + recent 15K` + `ContextSnapshot`

---

## 8. Tools

`backend/tools/registry.py` `vision` (`VisionGeneralTool` `artifact_id` + `query`, `image_path` compat déprécié, `20 Mo` + traversal `data/blobs|temp`) + `doc_ocr` + `get_source` + `read_text` via `BaseTool.call` (timeout 15, `max_output 8000`).

---

## 9. Frontend

- **Base** : `main.tsx`, `styles/tokens.css`, `components/ui/*`, `shell/AppShell`, `composer/Composer`, `pages/chat/ChatHome/ChatConversation`, `history/HistoryPage`
- **Annexe 09/10** : `features/attachments/*` `MAX_ATTACHMENTS 4` (trombone 10 `10:237`), `AttachmentDraftContext` `stageItemId/stagedPath/previewToken/needsResize` (survit `ChatHome→ChatConversation`), `useAttachmentDrop` stub, `attachmentValidation` `20 Mo` + `WARN 5 Mo` → `resize 1536` tracé `NaFlex` (plus `5 Mo` dur), `ChatConversation.css` grid, `Composer.css` halos `prefers-reduced-motion` + `composerShellRefus` rouge
- **State** : `ChatConversation` `ChatState idle|preparing|orchestrating|tool_running|generating`, `debugMode` `thot_debug`, `execution_plan`, `q-r.md` 16-19 répondu

---

## 10. Drag & Drop (annexe 09 + 10)

**Natif pur** `dragDropEnabled:true` + `onDragDropEvent` fenêtre, Hus `OLE` exclusif Windows (HTML5 inactif en Tauri, fallback Vite) :

- `App.tsx` `AttachmentDraftProvider` + `GlobalDragGuard` `preventDefault` sans `stopPropagation`
- `Composer.tsx` machine `repos/détecté/ciblé/hors cible/refus/dépôt/annulé` `10:248` (voile discret `glisser_detecte`, `hors_cible`, `ciblé` bordure/halo/échelle, `refus` rouge + libellé `>4`/`dossier/.lnk`/`type`), `prefers-reduced-motion` bordure statique, `aria-live` `Dépôt possible`/`Fichiers reçus`, `isDraggingGlobal` + `isDraggingOverComposer` (`dragCounterRef` + `dataTransfer.types Files` + `isInsideComposer(clientX)` / natif `position/scaleFactor` + `validateHover` pré-validation)
- `stage_dropped_paths` → `File` placeholder avec vrais bytes via `get_staged_preview_bytes` → `URL.createObjectURL` (fini `?` `10:212`, même `AttachmentPreview` drag/trombone), `thot:add-files` → `addFiles` dedup `nom` (évite 3× `window+container+natif`), `lastNativeDropRef 250ms`, `stage_picker_bytes` pour trombone/collage (même `StagingResponse`)

---

## 11. Correctifs Réponses.md n°2→n°11 + Annexe 10 Lots 1-3

- **n°2-11** : voir `Notes.md` (mmproj, `SYSTEM_*`, `drag`, `Source` enrichie, `registry` sémantique, `VRAM` FSM, `persist_user_turn` avant LLM, DB unique)
- **Annexe 10 Lot 1** : supprime regex `[vision(...)]` (`INVALID_DECISION`), `BaseTool.call`, ne transmet plus `image_paths` aux prompts/events publics `10:570`
- **Lot 2** : `save_upload` + `stage_dropped_files` → même `stageItemId/previewToken` `10:578`, même `AttachmentDraftContext`/`AttachmentPreview` `10:578`, `image_paths` → `attachment_ids` `10:581`
- **Lot 3** : `Blob/Artifact/ArtifactLink/MessageAttachment/Observation` `10:585`, promotion avant run `10:302`, scope `C18` `ATTACHMENT_NOT_IN_SCOPE`, `Vision` sur `artifact_id` `10:406`

---

## 12. Fichiers clés

- `Thot-phase-1/q-r.md` — 16-19 répondu (enveloppe `image_paths` vs `attachment_ids`, IPC `stdin/stdout`, `vision` structuré, `SharedContext` brief)
- `backend/main.py` — `attachments.prepare` compat `image_paths` → `artifact_id`, `chat.send` `attachment_ids` + `MessageAttachment` scope
- `backend/inference/loop.py` — `SharedContext`, `attachmentRegistry` depuis `Artifact`, `vision` `artifact_id`, `prepare_vision_image 1536` tracé, `VISION_MULTIMODAL_NOT_INITIALIZED`
- `backend/inference/manager.py` — FSM + admission
- `backend/tools/vision_general.py` — `artifact_id` durable + `20 Mo`
- `backend/db/models.py` — 12 tables, `database.py` `THOT_*` env, `media_cache.py` `data/blobs`
- `frontend/src/lib/tauri.ts` — `chatSend({attachment_ids})/stageDroppedPaths/stagePickerBytes/attachmentsPrepare`, `frontend/src/features/attachments/*`
- `src-tauri/src/lib.rs` — `resolve_*` portable, `stage_dropped_paths/stage_picker_bytes/attachments_prepare/get_staged_preview_bytes`, `chat_send` `attachment_ids`
- `src-tauri/tauri.conf.json` — `csp` restrictive, `resources [licenses,distribution.json,portable.json]`, `portable.json` `Thot/data`
- `models/` — 3 GGUF + 4 mmproj

---

## 13. Commandes

```bash
cd "Thot-phase-1"
pip install --break-system-packages -r requirements.txt
python3 -m pytest backend/tests/ -v  # 17 passed + 1 mmproj manquant attendu (sanitizer 6 OK)
python3 -c "from backend.inference.hardware import HardwareDetector; print(HardwareDetector().detect())"
echo '{"id":"test","method":"chat.send","params":{"message":"Bonjour","mode":"chat"}}' | python3 -m backend.main
cargo check --manifest-path src-tauri/Cargo.toml
npm --prefix frontend install && npm run build  # 242 modules 309kB
npm run tauri dev  # Vite 1420 + Tauri (drag natif, preview token, Vision artifact_id)
find ~/.local/share -name "thot-phase-1.db" | xargs ls -lh
sqlite3 ~/.local/share/thot/data/database/thot-phase-1.db ".tables"  # 12 tables
sqlite3 ~/.local/share/thot/data/database/thot-phase-1.db "SELECT id FROM blobs LIMIT 3;"
```

---

## 14. Tests

- `test_hardware.py`, `test_manager.py`, `test_loop_sanitizer.py` 6 (annexe 10 `INVALID_DECISION`), `test_ocr_policy.py` 8, `test_manager_switch.py`, `manual/test_lfm25_vl.py` (red_square), `T-ATT-01` trombone=drag même vignette, `T-ATT-02` `MessageAttachment` avant run, `T-ATT-15` `[vision(...)]` jamais exécuté, `T-ATT-19` `prefers-reduced-motion` + `aria-live`
- **P2** : `T-ATT-05` réutilisation observation + `T-ATT-18` déplacement portable (blobs survivent) à qualifier `Lot 4`

---

## 15. État actuel & Next

- **Build** : `vite build` 309kB ✓, `cargo check` ✓, `pytest` 17 passed, `q-r.md` à jour
- **Drag** : 1 fichier (plus 3×), 1 toast (plus 4), vignette `convertFileSrc`/`objectURL` OK, `preview ?` fixé
- **Vision** : `artifact_id` durable `data/blobs` OK, `stage_item_id introuvable` log `staged_dir contient` ajouté, `image_paths` compat encore accepté mais log `compat`
- **Next Lot 4** : qualifier `T-ATT` matrice Windows/Linux/macOS, `prefers-reduced-motion`/`lecteur d'écran`/`clavier` `T-ATT-19`, `T-ATT-20` drag natif par plateforme, `trash` 7 jours + GC orphelin `10:543`

---

## 16. Stratégie toolcalls média (08-09/09/2026, logs q-r.md)

- **Constat** : function-calling natif du GGUF LFM2.5-8B défaillant (template non reconnu → `[vision(...)]` textuel stripé). La décision d'observer un fichier ne dépend JAMAIS du LLM.
- **R1** nouveau fichier au tour → `plan_media_tools()` déterministe (pdf→`doc_ocr`, image→`vision`, image+demande texte→`doc_ocr`), `backend/inference/media_policy.py`
- **R2** follow-up sans nouveau fichier + média dispo + question visuellement ancrée → réobserve avec la question comme query. Détection 3 couches OR : (A) référence explicite, (B) verbes de réexamen (`revois`, `regarde à nouveau`...), (C) détail visuel (`couleur`, `cheveux`, `fond`, `tient`... — cas q-r.md T2/T3 qui manquaient)
- **R3** registre `SOURCES DISPONIBLES` = source de vérité (courant + historique via `MessageAttachment`) ; `get_source` SANS `source_id` liste TOUS les fichiers de la conv ; prompt orchestrateur aligné R1-R3
- **Garde-fous** : `available` requis, max `MAX_ITER`, cache `(tool, artifact_id)` anti-doublon, `execution_plan` avec vrai `selected_tool`
- **Tests** : `test_media_pipeline.py` 21 (plomberie sans modèle) + `test_media_policy.py` 23 (politique + régressions q-r.md T2/T4 + list-all) + `test_prompt_tool_parity.py` 8 (prompt/orchestrateur <-> schémas outils) + `test_manager_oom.py` 8 (VRAM basse, offload partiel, sans GPU)
- **Manager VRAM** (q-r.md : 8B inchargeable après allers 8B<->VL, 4.6 Go libres) : `n_ctx` sur VRAM *courante* (plus cache détecteur), attente pré-chargement 8s, `_looks_like_oom()` traite `Failed to load model` + VRAM basse comme OOM → repli `n_gpu_layers` réduit ; unload : sleep 0.5s + attente 5s
- **Prompt** : `SYSTEM_ORCHESTRATOR` contient le mode d'emploi d'appel (args par outil, un seul par tour, anti-hallucination ids/chemins, anti `[vision(...)]` textuel) ; `VisionOCRArgs.artifact_id` ajouté au schéma ; descriptions `vision`/`doc_ocr`/`read_text` nomment explicitement `artifact_id`/`source_id`
- **Cadrage VLM** (09/09) : le VLM n'a pas le contexte LLM → `clean_media_query()` strippe la déixis ("la précédente image", "et maintenant", "revoit l'image") avant SYSTEM_VISION/OCR, appliqué dans les exécuteurs (couvre routage déterministe + LLM) ; routage OCR unifié `OCR_REQUEST_TERMS` ("lire ... écrit en japonais" → `doc_ocr`, plus `vision` qui déraillait)
- **Historique UI** (09/09) : `list_messages` renvoie `attachments[]` (artefacts + sources legacy, ids seuls) ; `get_attachment_bytes` fournit les bytes (pas de pari asset://) → vignettes reconstruites au rechargement, chips 📄 pour les PDF
- **Commentaires FR** : d'où-vient/où-va/pourquoi dans `media_policy.py`, `loop.py` (Phase 0, registre, exécuteurs), `main.py` (persistance, scope C18), `lib.rs` (pont IPC, vignettes), `tauri.ts`/`ChatConversation.tsx` (envoi, historique)
- **OCR mmproj** (09/09) : fichiers présents (`GLM-OCR-f16.gguf` + `mmproj-Q8_0`), handlers dispos (`Qwen25VL`...) — le "mmproj_path requis" venait de DEUX endroits : repli `sans_mmproj` (sauté pour vision) ET repli OOM-partiel qui passait `mmproj=False` (corrigé : vision garde son projector). Dégradation progressive : `llama_context` = OOM, `n_ctx` divisé par 2 à chaque repli (32768→16384→8192, plancher 4096/2048). Tests `test_manager_oom.py` 13
- **Test manuel VL** : SKIP si < 6 Go libres (l'app ouverte garde le 8B ≈ 9 Go — conflit de processus, pas bug). En-app pas de conflit (un seul sidecar, chargements séquentiels)

*Ce fichier remplace `Contexte.md` du 02/09. Pour détail, `Thot-phase-1/Notes.md`, `Depot_Github/annexes/10.md` font foi.*
