# Notes — Thot Phase 1 (31/08/2026)

## Dernières modifications (itération 31/08)

### ✅ Étape 1 — mmproj installés
Tous les projecteurs visuels sont en place :
- `models/Vision-general/mmproj-LFM2.5-VL-3B-BF16.gguf` (856 MB) — SigLIP2 NaFlex 400M (principal)
- `models/Vision-general/mmproj-LFM2.5-VL-3B-F16.gguf` (alternative F16)
- `models/Vision-general/mmproj-LFM2.5-VL-3B-Q8_0.gguf` (alternative Q8_0)
- `models/Vision-document/mmproj-GLM-OCR-Q8_0.gguf` (484 MB) — projector GLM-V

Ces fichiers étaient **absents** → pipeline vision **non fonctionnel** (images silencieusement ignorées).

### ✅ Étape 2 — MODEL_CONFIGS enrichi (`backend/config/defaults.py`)
- Ajout `mmproj` (chemin) par modèle : `None` pour 8B, config pour VL-3B et GLM-OCR
- Ajout `chat_handler` par modèle : `"ggml-vision-chat"` pour VL-3B, `"ggml-ocr-chat"` pour GLM-OCR
- `flash_attn` fixé à `False` pour GLM-OCR (ticket #19721 — flash dégrade ce modèle)
- `flash_attn` maintenu à `True` pour VL-3B (bon pour tiling dynamique 512×512)

### ✅ Étape 3 — Manager chargé les modèles avec mmproj + handlers (`manager.py`)
`_load_model()` construit dynamiquement les kwargs Llama (fix Réponses.md n°5 : `llama_cpp.llama_chat_handler` → `llama_cpp.llama_chat_format`) :
- Vérif en amont `import llama_chat_format` sinon `RuntimeError` immédiat (au lieu de 3 tentatives coûteuses)
- Si `cfg.supports_vision` : **fatal** si `mmproj` absent/fichier manquant ou `chat_handler` None
- `_build_vision_handler()` : mapping `ggml-vision-chat → LFM2VL > Qwen25VL > Llava15/16 > MTMD`, `ggml-ocr-chat → Qwen25VL > Llava15` avec `clip_model_path=mmproj` (0.3.34 n'a pas `LFM2VLChatHandler`, fallback documenté)
- Fallbacks conservés mais inversés en erreurs explicites quand handler incompatible

### ✅ Étape 4 — Drag drop corrigé (`tauri.conf.json`)
- `dragDropEnabled: true` → `false` (une ligne à `src-tauri/tauri.conf.json:12`)
- La sémantique est inversée par rapport à l'intuition : `true` = natif Tauri actif, **DOM drag-drop désactivé**
- Le code React existant (`dragCounterRef`, `dataTransfer.files`, `items.getAsFile()`, overlay dashed gold) fonctionne tel quel avec `false`

### ✅ Étape 5 — onPaste Ctrl+V (`Composer.tsx`)
- Ajout `handlePaste()` qui lit `e.clipboardData.items`, extrait les fichiers collés, appelle `processFiles()`
- Même pipeline que drag-drop : validation types/tailles, previews objectURL
- Connecté sur le `<textarea>` : `onPaste={handlePaste}`
- Supporte Ctrl+V pour images/captures d'écran

### ✅ Étape 6 — OCR marqueurs fermés (`ocr.py` + `loop.py`)
- Nouveau `build_system_ocr(query, request_table)` qui :
  - Choisit `Table Recognition:` si la requête contient "tableau/table/tabuler/grille/csv"
  - Choisit `Text Recognition:` (défaut) sinon
- La consigne de fidélité et de langue reste **uniquement** du côté du générateur (brief) — conformément à l'architecture 2 couches
- `loop.py` : `_execute_doc_ocr` passe le prompt basé sur `build_system_ocr()` au lieu de `SYSTEM_OCR.format(query)`
- Détection de tables dans la query : `any(kw in query.lower() for kw in ("tableau", "table ", "table?", "tabuler", "grille", "csv"))`

### ✅ Étape 7 — DB path via env Tauri (`database.py`)
- Résolution : `THOT_DB_PATH` env injecté par Rust Tauri (`app.path().app_data_dir()`) → priorité absolue
- Fallback : `data/thot-phase-1.db` local (si env non défini)
- Logging de chaque résolution

### ✅ Étape 8 — check_models() startup (`check_models.py`)
- Vérifie présence + taille des 5 fichiers (3 GGUF + 2 mmproj)
- Retourne summary dict avec path_ok + mmproj_ok + path_size_mb
- Raise RuntimeError avec message actionnable si fichier manquant
- Résolu depuis `Path(__file__).resolve().parent.parent` (projet root)

### 🔄 Réponses.md — "Réponds dans la langue de l'utilisateur"
Maintenu sur tous les prompts (orchestrator, generator, vision). 
Note : moved language instruction to generator (brief) only. OCR n'a plus de "langue" car GLM-OCR est un marqueur fermé (Text/Table Recognition).

### ✅ Itération 01/09 — Correctifs UI/UX Réponses.md n°2 (build bloquant + phases)

**Blocage** : `Composer.tsx:314` — apostrophe mal échappée + caractères parasites `'}i…'`
→ `npm run build` échouait. **Patch minimal** :
```tsx
placeholder={
  droppedFiles.length > 0
    ? "Que souhaitez-vous analyser avec ce fichier ?"
    : "Ecrivez votre message..."
}
```
Guillemets doubles, condition explicite `>0`, brutalité zéro conflit d'encodage.

**Composer.tsx** :
- Retrait du listener `tauri://drag-drop` (inutile avec `dragDropEnabled:false`, source de complexité chemins vs File)
- `handlePaste` simplifié : `Array.from(e.clipboardData.files)` → `processFiles(files)`
- Ajout `useEffect` cleanup des `objectURL` à l'unmount
- Déjà OK : `dragDropEnabled:false` + `onPaste` sur `<textarea>`, `processFiles` unifié

**ChatConversation.tsx** :
- `ChatState` : `idle|sending|streaming` → `idle|preparing|orchestrating|tool_running|generating`
- `onMessageStarted` → `preparing`, `onThinking` → déduit `phase`/`iteration` (generator=99) → `orchestrating`/`generating`, `onToolCall` → `tool_running`, `onAnswer` → `generating`, `onMessageCompleted` → `idle` (fini le toast "Réponse terminée")
- Retrait du `setTimeout 600ms` — `setChatState('preparing')` direct après `chatSend`, plus de désync vision longue
- Retrait toasts `Analyse en cours…` et `Outil terminé` (succès), gardé seulement `Outil échoué` et `Erreur d'envoi`
- `isBusy = chatState !== 'idle'`, libellés dérivés de `phase` (Préparation/Analyse/Outil/Génération)
- Nouveau composant `AssistantTurn.tsx` créé (phase centralisée, article unique par tour, activity + toolCards + thinking debug + bubble)

**ToolCallDisplay.tsx** :
- Fermé : `✓ Vision générale · LFM2.5-VL-3B · 416 car. · 1.24 s` (model + chars + latence visible)
- Ouvert : Requête + Source `hash 12 chars` + Résultat technique replié + preview 500 chars
- `spinner` via classe `.spinner`, `enterSoft` animation

**tokens.css** :
- `--motion-fast/next` + `--ease-out`, `.enterSoft`, `.spinner`, `@keyframes pulse/spin`, `reduced-motion` déjà OK (étendu)

**ChatConversation.css (nouveau)** :
- `.conversationLayout { grid: minmax(0,820px) 260px; justify:center; gap:32px; width:min(1240px,100%); padding:24px 32px 144px; }`
- `.chatColumn` + `.chatColumnInner max-width:820px`, `.detailsRail sticky top:24px`, `.composerDock width:min(820px,calc(100vw-64px))`
- `@media (max-width:1040px)` : block + rail `display:none` + composer `calc(100vw-40px)` — évite la colonne de 600px trop étroite

> Validation : `npm run build` ✓ (233 modules, 295kB) puis `npm run tauri dev` à tester (drop HTML5 + paste + phases).

### ✅ Itération 01/09 — Réponses.md n°3 (contrat pièce jointe)

**Diagnostic** : image jointe visible dans bulle user mais générateur répond "je n'ai pas accès à l'image" → pipeline fragile (orchestrateur décide seul d'appeler vision, parsing textuel LFM défaillant, réflexion trop visible).

**Composer.tsx** :
- `handleDrop` simplifié strict spec Réponses.md (`Array.from(e.dataTransfer.files)` → `processFiles`)
- `maxWidth 960 → 820` (alignement avec grille)

**Backend `loop.py`** :
- `select_attachment_tool(image_paths)` déterministe : `pdf→doc_ocr`, `image→vision`, `None` sinon
- Routage **forcé** avant orchestrateur : si `image_paths>0`, `emit thinking tool_running` + `tool_call forced_*` + `execute vision/doc_ocr` + `tool_result` + `tool_results` + `tool_observations` (même en cas d'échec)
- `image_paths` log/assert avant run (`valid cnt`, `emit error` si 0 valide)
- `_format_tool_observation()` : `<tool_observation> tool/status/attachment/result </tool_observation>`
- Append aussi `tool_observations` depuis boucle orchestrateur (si outils supplémentaires)
- Générateur : `obs_block = join(tool_observations)` prioritaire sur `json.dumps(prepared)`, injecté `Observations d'outils:` + `Brief:` → plus jamais "je n'ai pas accès" si observation fournie

**Backend `generator.py`** :
- `SYSTEM_GENERATOR` remplacé par règles Réponses.md : 1) fonde sur observation si fournie 2) ne jamais nier accès 3) explique échec sans inventer 4) langue utilisateur 5) cache chemins/raisonnement

**Frontend `ChatConversation.tsx`** :
- `debugMode = localStorage thot_debug===1` — réflexion `msg.thinking` affichée **uniquement en dev** (renommé "Détails d'exécution")
- Bulle assistant : `borderRadius 18`, `padding 16 18 34`, `shadow 0 8px 28px`, `position:relative` + bouton `Copier` absolu `right:12 bottom:8` (plus de ligne détachée)
- Rail conservé sticky mais caché via `ChatConversation.css @media 1040px` (déjà OK)

> Validation : `npm run build` ✓ (233 modules, 295kB) + `py_compile loop.py/generator.py` ✓

### ✅ Itération 01/09 — Réponses.md n°4 (mmproj + drag global)

**Diagnostic** : `Requested tokens (1885477) exceed context window of 32768` → base64 traitée comme texte ordinaire (pas de tokens visuels), pas un problème de taille.

**Inventaire** :
```
models/Vision-general/LFM2.5-VL-3B-UD-Q4_K_XL.gguf 1.7G + mmproj BF16 856M / F16 854M / Q8 583M
models/Vision-document/GLM-OCR-f16.gguf 1.7G + mmproj Q8 484M
```
→ GGUF + mmproj présents mais handler `ggml-vision-chat`/`ggml-ocr-chat` possiblement incompatible (Llava générique ≠ SigLIP2/NaFlex). Cause racine : handler non branché → `image_url` rendue dans prompt texte → 1.8M tokens.

**Manager (`manager.py`)** :
- `_build_llm_kwargs` : si `supports_vision` → **fatal** si `mmproj` absent ou fichier inexistant, et si `chat_handler` absent ou `AutoChatHandler` retourne `None` → `RuntimeError` (plus de fallback silencieux)

**Loop (`loop.py`)** :
- `MAX_VISION_SIDE=1536` + `prepare_vision_image()` : EXIF transpose + thumbnail 1536 + JPEG 88 (protège VRAM/latence, pas de contournement base64)
- `_execute_vision` : prétraite avant base64, mime toujours `image/jpeg`, log si b64 >4M, catch `exceed context` → `VISION_MULTIMODAL_NOT_INITIALIZED`
- `backend/tests/manual/test_lfm25_vl.py` : smoke test hors orchestrateur (red_square 64px → "rouge"/"red", assert tokens <<32768)

**Drag global (`frontend`)** :
- `features/attachments/` **(nouveau)** : `attachmentTypes.ts`, `attachmentValidation.ts` (4 / 5M / 20M), `AttachmentDraftContext.tsx` (Provider au-dessus du Router, survit `ChatHome→ChatConversation`), `useAttachmentDrop.ts`, `AttachmentTray.tsx`
- `App.tsx` : `AttachmentDraftProvider` + `GlobalDragGuard` (`dragover/drop preventDefault` sans `stopPropagation`)
- `Composer.tsx` : utilise `useAttachmentDraft` (attachments/addFiles/remove/clear), `validateAttachments`, `showToast`, `maxWidth 820`
- `ChatHome.tsx` : `onDragEnter/Over/Leave/Drop` sur colonne principale + overlay "Déposez une image ou un PDF" + même `processFiles`
- `ChatConversation.tsx` : idem sur `conversationLayout` + overlay doré

> Validation : `npm run build` ✓ 236 modules (299kB) + `py_compile` ✓ — critère : image déposée dans ChatHome ou ChatConversation → preview épinglée avant envoi, aucun 1.8M tokens, smoke test vert.

### ✅ Itération 01/09 — Réponses.md n°5 (handler import + drag animation)

**Diagnostic** : `RuntimeError chat_handler ggml-vision-chat non chargé: No module named 'llama_cpp.llama_chat_handler'` → 3 tentatives échouent identiquement avec même `ImportError` (faux module). Vrai module = `llama_cpp.llama_chat_format` (deepwiki 5.2). Version 0.3.34 expose `Llava15/16, Qwen25VL, MTMD...` mais **pas `LFM2VLChatHandler`** → besoin fallback/version plus récente.

**Manager (`manager.py`)** :
- `import llama_cpp.llama_chat_format` en amont → `RuntimeError` immédiat si absent
- `_build_vision_handler(cfg, mmproj_path)` : `ggml-vision-chat → [LFM2VL, Qwen25VL, Llava15, Llava16, MTMD]`, `ggml-ocr-chat → [Qwen25VL, Llava15, MTMD]`, `clip_model_path=mmproj`

**Drag animation (`Composer.tsx` + `Composer.css` (nouveau))** :
- `isDraggingGlobal` (window `dragenter/leave/over/drop`) + `isDraggingOverComposer` (local) — spec PageAssist
- `composerShell` + `composerShellAmbientHighlight` (border pulse 1.4s) + `composerShellActiveDrop` (scale 1.015, dashed gold) + `composerDropOverlay` (＋ "Déposez ici")
- `pageDragAmbientOverlay` (fixed bottom 140px, "Déposez votre fichier dans la barre de saisie") quand `global && !overComposer`
- `App.tsx` garde `dragDropEnabled:false` (redémarrage Tauri requis)

> Validation : `npm run build` ✓ 237 modules (299kB, +1.1kB CSS) + `py_compile` ✓ + `Llava15ChatHandler(clip_model_path=mmproj BF16)` ✓

### ✅ Itération 01/09 — Réponses.md n°6 (4 bugs : OCR, tool policy, scroll, drag)

**Diagnostic** : Vision générale ✓ 215 car. 4.70s (VLM OK), mais OCR appelé après vision → `build_system_ocr() takes 1 pos but 2 given` + scroll bloqué + drag absent.

**OCR (`ocr.py`)** :
- `build_system_ocr(*, task: str)` stable (plus de `query, request_table` positionnel) — `task=text|table|formula` → `Text/Table/Formula Recognition:` + `resolve_ocr_task(query)` + `SYSTEM_OCR=build_system_ocr(task="text")`
- `loop.py` : `ocr_task=resolve_ocr_task(query)` → `build_system_ocr(task=ocr_task)`

**Outils (`loop.py`)** :
- `asks_for_text_extraction()` + `resolve_allowed_tools(message, image_paths)` : `pdf→doc_ocr`, `image+OCR intent→doc_ocr`, `image→vision`, `sinon []`
- `tool_defs` filtrés par `allowed_tools` (plus de `vision+doc_ocr` systématique)
- `execution_plan` event émis (attachmentKind/intent/selectedTool/reason) — rendu `details` ouvert "Orchestrateur — Plan d'exécution" dans `ChatConversation`
- `skip_orchestrator` : si `allowed len==1` et `forced ok` → `brief=build_attachment_brief` + `orchestrator_done=True` + `for range(0 if skip else MAX_ITER)` (évite vision→OCR parasite)

**Scroll (`global.css` + `AppShell.tsx` + `ChatConversation.tsx/css`)** :
- `html,body,#root 100% overflow hidden`, `.appShell flex 100dvh`, `.appMain min 0 flex1`, `.chatPage grid auto minmax(0,1fr) auto`, `.messagesViewport overflow-y auto`
- `AppShell` : `className appShell/appMain`, `ChatConversation` : `chatPage` + `conversationLayout messagesViewport`
- `ChatConversation.css` : `conversationLayout` padding 144→36, overflow hidden→min-height 0

**Drag natif (`Composer.tsx` + `tauri.ts` + `ChatHome/ChatConversation`)** :
- `Composer` : `lastNativeDropRef` dedup 250ms, `onDragDropEvent` Tauri + `stage_dropped_files` invoke (staging Rust si dispo), `console.debug` sinon
- `tauri.ts` : `onExecutionPlan` ajouté, `App.tsx` garde `dragDropEnabled:false` + `GlobalDragGuard` sans `stopPropagation`

**Tests (`tests/test_ocr_policy.py` 8 tests)** :
- `build_system_ocr_text/table/formula`, `image→vision`, `image+OCR intent→doc_ocr`, `pdf→doc_ocr`, `no file→[]`, `resolve_ocr_task`

> Validation : `npm run build` ✓ 242 modules (303kB) + `py_compile` ✓ + `pytest test_ocr_policy.py` 8 passed

### ✅ Itération 01/09 — Réponses.md n°7 (images passées + drag natif pur)

**Diagnostic** : Vision OK mais "Est-ce qu'il y a du texte dans la première image ?" → réponse "Il n'y a aucun texte" sans avoir revu l'image (`Fichier détecté : aucun`). VLM confond Spike/Luffy, suit affirmation Wakfu sans preuve.

**Persistance (`models.py` + `media_cache.py` + `main.py`)** :
- `Source` enrichie : `media_kind, original_name, mime_type, sha256, relative_path, size_bytes, created_at` + `idx_sources_sha256`
- `media_cache.py` : `get_media_root() = ~/.local/share/thot/media`, `persist_attachment(tmp_path)` → `media/{sha256}.ext` (copy2 si absent), hash 64 hex
- `main.py` : après `append_message(user)`, `persist_attachment` pour chaque `image_paths` + `Source` insert (attachment, hash, relative_path)

**Ré-inspection (`loop.py`)** :
- `requires_media_reinspection(message)` : détecte "cette image", "première image", "vérifie", "relis"...
- `resolve_reinspection_tool(message)` : `texte/écrit/lis/ocr → doc_ocr` sinon `vision`
- `_get_past_source_path(conv_id, message)` : query `Source` join `Message` ordonné, résout ordinal (première→images[0], deuxième→[1], dernière→[-1], ambigu "cette image" avec 3 images → None → clarification)
- `run_agent_loop` : si `!image_paths && requires` → `image_paths=[past_path]` avant `resolve_allowed_tools` → `vision/OCR` forcé sur source persistée (évite "j'ai vérifié" sans preuve)
- `generator.py` : ajout **Règle preuve visuelle** (pas de "j'ai vérifié" sans observation réussie du tour courant)

**Drag natif pur (`tauri.conf.json` + `Composer.tsx`)** :
- `dragDropEnabled: false → true` (opposé à n°4 — choix **Tauri natif pur** pour desktop, DOM seul pour Vite dev)
- `Composer` : `lastNativeDropRef` dedup 250ms déjà, `onDragDropEvent` + `stage_dropped_files` (Rust copie vers `drafts/{draftId}/{sha256}.ext` + validation magic bytes/taille) → `AttachmentDraftProvider`
- Spec : `AppShell` futur `NativeDropProvider` unique, `window_entered/over_composer/staging/accepted` + voile `pageDragAmbientOverlay`

> Validation : `npm run build` ✓ 242 modules (303kB) + `py_compile` ✓ (models, media_cache, loop, generator)

### ✅ Itération 01/09 — Réponses.md n°8 (orchestrateur sémantique vs mots-clés)

**Diagnostic** : routeur mots-clés `if "écrit" in message` fragile (synonymes, langues, tournures indirectes). "sa main" → aucun outil car mot manquant, alors que c'est le contenu visuel qui justifie réanalyse.

**Changement de paradigme** :
- Retrait `resolve_allowed_tools()` + `select_attachment_tool()` + `requires_media_reinspection()` comme **filtre outil** — le modèle décide, pas les regex (netfoundry tool-calling sémantique)
- `vision_general.py` / `vision_ocr.py` : descriptions enrichies — *"Une description textuelle donnée plus tôt n'est jamais une preuve suffisante : si la question porte sur un détail visuel (couleur, objet, texte...), rappelle cet outil sur l'image concernée plutôt que de répondre de mémoire."* + `doc_ocr` : *"Ne réponds jamais sur la présence/absence de texte sans avoir appelé cet outil."*
- `orchestrator.py` : `SYSTEM_ORCHESTRATOR` + règle fiabilité : *"Si répondre nécessite un détail visuel précis d'un de ces fichiers — qu'il s'agisse d'un fichier tout juste envoyé ou d'un fichier envoyé plus tôt — appelle vision/doc_ocr avant de répondre, même si tu as déjà décrit ce fichier."*
- `loop.py` : `build_attachment_registry(conv_id, image_paths)` → liste `{index,kind,name,path,first_seen}` depuis DB `Source` + `image_paths` courants, `format_attachment_registry()` injecté comme **bloc système** avant `history` (`messages_orch = [SYSTEM_ORCHESTRATOR, registry_block] + history + [user_content]`), `has_any_media = bool(registry)` → `tool_defs = registry.definitions() if has_any_media else []` (`tool_choice auto` laisse le modèle choisir vision/doc_ocr/aucun), plus de routage forcé `forced_*` ni `skip_orchestrator`
- Persistance (`media_cache.py`, `main.py`) et `Source` gardés — le registre rend les images historiques visibles à chaque tour, l'orchestrateur peut rappeler `vision(image_path="media/...")` sur n'importe quelle source listée

**Ancien code conservé** : `asks_for_text_extraction`, `resolve_allowed_tools`, `_get_past_source_path` restent dans `loop.py` pour tests `test_ocr_policy.py` mais ne gouvernent plus la décision outil — ils sont dépréciés.

> Validation : `npm run build` ✓ 242 modules (303kB) + `py_compile` ✓ + `pytest test_ocr_policy.py` 8 passed — attente `cargo tauri dev` pour test "Qu'est-ce qu'il y a d'écrit sur sa main ?" → `doc_ocr` sur `spike_bebop.png` via sémantique

### ✅ Itération 01/09 — Réponses.md n°9 (VRAM leak manager)

**Diagnostic** : `Impossible de charger LFM2.5-8B après 3 tentatives: [('config idéelle', ValueError)...` même erreur sur `flash_attn`/`sans mmproj` → cause hors config, VRAM occupée. Tour 1 `Coucou` OK (8B), Tour 2 `Décrit image` → VL-3B OK puis **rechargement 8B échoue** → `En attente`.

**Cause** : `manager.py` faisait `del + gc + torch.cuda.empty_cache()` — `llama-cpp-python` alloue via `llama.cpp` C++ CUDA direct, **pas** PyTorch → `empty_cache()` ne libère rien. Référence résiduelle `chat_handler` gardait CLIP chargé, `gc` ne collectait pas, VRAM VL-3B (~2.5G) restait → 8B (~7G) OOM `ValueError: Failed to load model from file`.

**Correctifs (`manager.py`)** :
- `__init__` : `+ _current_chat_handler`, `_min_required_vram_mb=6000`
- `_get_free_vram_mb()` : `nvidia-smi --query-gpu=memory.free` → `pynvml` → `backend.free_vram` fallback
- `_wait_for_vram_release(timeout 3s)` : poll `free >= min` sinon `logger.warning`
- `use_model` : `VRAM avant unload {name}: X Mo` / `après unload: Y Mo`, `_unload_current()` avant `_load_model`
- `_unload_current()` : nullifie `self._current_model=None`, `handler=None`, `name=None` **avant** `del/close`, `if hasattr(model,"close"): model.close() else: del`, `del handler`, `gc.collect()`, `_wait_for_vram_release()`, `torch.empty_cache()` en dernier
- `_build_llm_kwargs` : stocke `self._current_chat_handler` pour libération conjointe
- `_load_model` : `LOAD_STRATEGIES = [nominal, apres_gc_force(0.5s), flash_off, sans_mmproj]` + `pre_delay gc+wait`, si OOM déjà vu → `estimate_layers` `n_gpu_layers réduit` en priorité (vrai fix mémoire vs config)

**Test** : `backend/tests/test_manager_switch.py::test_switch_vl_then_text_model_succeeds` mock `Llama` + `close` → `unload` nullifie handler + `use_model` VL→8B OK

> Validation : `py_compile manager.py` ✓, `get_free_vram_mb() = 4448 Mo`, `pytest test_manager_switch.py` passed (5.11s)

### ✅ Itération 01/09 — Réponses.md n°10 (registre manifest + get_source) — adapté code réel

**Diagnostic** : "Qu'en penses-tu ?" + image → registre `1 fichier, orchestrateur décide` mais `DIRECT_ANSWER` sans `vision` → "Je n'ai pas accès". "Revois-là tatouage" → `Fichier détecté : aucun` → pas de référence historique résolue.

**Adaptation code réel** :
- `models.py` : `Source` + `conversation_id`, `parent_source_id`, `analysis_version`, `last_accessed_at`, `idx_sources_conversation_created` — persiste `conversation_id` dès `main.py` (`Source(..., conversation_id=conv_id)`) pour `build_attachment_registry` sans join lourd
- `loop.py` : `build_attachment_registry(conv_id, image_paths)` enrichi — `ordinal, id, kind, name, path, message_index, available, previous_analyses` (query `ToolCall` par `msg_id` + `sha256` fallback, `available` via `media/{sha256}.ext` exists), `format_attachment_registry()` → `SOURCES DISPONIBLES` + JSON compact, injecté comme **2e bloc système** avant `history` (`messages_orch = [SYSTEM_ORCHESTRATOR, registry_block] + history + [user_content]`)
- `loop.py` : **Retrait routage forcé** `select_attachment_tool` + `skip_orchestrator` + `allowed_tools` lexical — `has_any_media = bool(registry)` → `tool_defs = registry.definitions() if has_any_media else []` (`tool_choice auto`), l'orchestrateur avec 4 outils (`vision, doc_ocr, get_source, read_text`) décide sémantiquement
- `tools/get_source.py` + `read_text.py` **(nouveaux)** : `get_source(source_id, reason)` → `working_path` persistant + `last_accessed_at` update, `read_text(source_id, purpose, max_chars)` UTF-8 tronqué, enregistrés dans `registry.py` `build_default_registry()` → 4 outils
- `loop.py` : `+ _execute_get_source/read_text` dans boucle orchestrateur (`tool_name == "get_source"/"read_text"`), `get_source → vision` en 2 itérations (séquence `get_source` puis `vision` dans `MAX_ITER=5`)
- `vision_general.py` / `vision_ocr.py` / `orchestrator.py` : descriptions déjà enrichies n°8, précisé `first_seen=current` pour image nouvellement jointe ("Qu'en penses-tu ?" + image → registre `available oui` → vision obligatoire)

> Validation : `py_compile loop/get_source/read_text/registry/orchestrator` ✓, `npm run build` ✓ 242 modules (303kB) — test attendu : "Qu'en penses-tu ?" + image → `vision(src_pending)` puis "tatouage sur sa peau" → `get_source(src_001) → vision`

### ✅ Itération 01/09 — Réponses.md n°11 (persistance DB unique) — adapté

**Diagnostic** : Historique vide après image analysée, `Fichier détecté : aucun` au tour suivant — capture prouve conversation `1e54db31` visible en session mais absente de Historique. `~/.local/share/thot/media` existe mais `Fichier détecté : aucun` → registre lit DB différente de celle où `Source` écrite.

**Hypothèse confirmée** : 2 DB divergentes — Rust `list_conversations` lisait `/home/hera/Musique/Thot v0.8/Thot-phase-1/data/thot-phase-1.db` hardcodé, Python sidecar écrivait `THOT_DB_PATH` ou `~/.local/share/thot/data/thot-phase-1.db`/`Thot-phase-1/data` selon env — adapt. réel : chemins avec espaces + `app_data_dir` Tauri (`~/.local/share/com.thot.phase1` en prod) vs `Path.home()`.

**Correctifs (`src-tauri/src/lib.rs`)** :
- `resolve_db_path(app: &AppHandle) -> PathBuf` : `app.path().app_data_dir()/data/thot-phase-1.db`, `create_dir_all`, `println!("[rust] Thot DB path resolved: {}", display)` — **source vérité unique**
- `list_conversations/get_conversation/list_messages` : `fn(..., app: AppHandle, ...)` → `resolve_db_path(&app)` avec fallback hardcodé pour CLI, `Path::new(&db_path).exists()`
- `setup` spawn sidecar : `let db_path = resolve_db_path(handle)`, `println!`, `Command::new(...).env("THOT_DB_PATH", db_path).env("THOT_APP_DATA_DIR", app_data_dir)` pour `python3 -m backend.main` (debug) et `sidecar("thot-sidecar")` (release)

**Python (`backend/db/database.py` + `main.py` + `media_cache.py`)** :
- `database.py` déjà `THOT_DB_PATH` env prioritaire (`Path(env).expanduser().resolve()`), fallback `home/.local/share/thot/data/thot-phase-1.db` — log `log.info("Thot DB path: %s", get_db_path())`
- `main.py` : `persist_user_turn` avant LLM — `ensure_conversation(convId)` → `create user Message` → `promote_attachment_to_media_cache(/tmp→media/{sha256}.ext)` → `Source(conversation_id, message_id, sha256, relative_path)` → `session.commit()` **avant** `run_agent_loop` (survit même si Vision crash) — `append assistant` + `ToolCall` commit après génération
- `Source` déjà enrichie n°10 avec `conversation_id` index, `media_cache.py` déjà `~/.local/share/thot/media` (adapt. réel : `Path.home()/".local/share/thot/media"` vs `THOT_APP_DATA_DIR/media` — cohérent via `get_media_root()`)

**Validation** : `cargo check` ✓, `py_compile main/models/media_cache` ✓, `THOT_DB_PATH` même valeur Rust/Python dans logs (`println!` + `log.info`), `sqlite3 ~/.local/share/.../thot-phase-1.db` doit montrer `conversations/messages/sources` après image

## Architecture système en cours
