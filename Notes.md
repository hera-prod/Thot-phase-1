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
`_load_model()` construit dynamiquement les kwargs Llama :
- Si `cfg.mmproj` : passe `mmproj` au Llama
- Si `cfg.chat_handler` : instancie `AutoChatHandler.from_model_path()` via `llama_cpp.llama_chat_handler`
- Fallbacks en cascade (logging détaillé) :
  1. config idéale (flash_attn figure + mmproj + handler)
  2. OOM → partial GPU layers sans mmproj
  3. Flash désactivé + mmproj handler gardés
  4. Sans mmproj/chat_handler (si handler provoque échec) → warning "images seront ignorées"

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

## Architecture système en cours
