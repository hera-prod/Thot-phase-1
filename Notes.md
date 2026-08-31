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

## Architecture système en cours
