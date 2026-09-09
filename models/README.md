# Modèles + Stockage portable + Build

## 1. Dossiers englobés

```
models/                          ← versionné ou copié à côté de l'app (7 fichiers)
├── LLM/LFM2.5-8B-A1B-UD-Q4_K_M.gguf (5.0G, MoE 8.3B 32 top-4, 128K natif, supports_vision=False)
├── Vision-document/GLM-OCR-f16.gguf (1.7G) + mmproj-GLM-OCR-Q8_0.gguf (484M, 8K)
└── Vision-general/LFM2.5-VL-3B-UD-Q4_K_XL.gguf (1.6G) + mmproj-*-BF16.gguf (856M + F16 854M, Q8 583M)
    (3.1B dense, 32K, NaFlex 512×512 patches + thumbnail, supports_vision=True, flash_attn=True)

data/ (portable si portable.json à la racine, sinon ~/.local/share/thot/data)
├── database/thot-phase-1.db     ← SQLite WAL 12 tables (cf. backend/db/README.md)
├── blobs/sha256/ab/...bin       ← durable, dédupliqué, jamais renommé
├── previews/                    ← reconstructible (vignettes)
├── derived/ temp/staged/ trash/ ← volatil / 7 jours / GC orphelin
└── models/                      ← miroir portable des GGUF ci-dessus

racine : portable.json + distribution.json + licenses/ (versionnés)
         pyproject.toml + requirements.txt (llama-cpp-python==0.3.34 figé, pas de venv/uv)
         thot-sidecar*.spec (PyInstaller --onefile --collect-all backend)
```

## 2. Pseudo-code (comment c'est fait)

```
# Résolution stockage (Rust source vérité, Python suit via env)
data_dir = SI portable.json présent → <racine>/data
         SINON → ~/.local/share/thot/data (XDG)
db = data/database/thot-phase-1.db
Rust spawn sidecar avec THOT_DATA_DIR / THOT_DB_PATH / THOT_APP_DATA_DIR
Python database.py : env prioritaire SINON get_data_dir()/database/thot-phase-1.db

# Gestion mémoire (cf. backend/inference/README.md pour la FSM complète)
RÈGLE : 1 modèle en VRAM à la fois, admission 1 run lourd
  orchestrator/générateur : LFM2.5-8B (temp 0.2, n_ctx adaptatif ≥12GB→131072 sinon min(32768, tokens+1024))
  vision générale : LFM2.5-VL-3B + mmproj (prepare 1536px JPEG88, NaFlex tracé)
  OCR : GLM-OCR-f16 + mmproj-Q8_0 (Text/Table/Formula, resolve_ocr_task)
  KV Q8_0, mmap 300ms, _wait_for_vram_release(3s), unload sleep 0.5s + attente 5s
  OOM ("Failed to load model" ou VRAM basse) → n_gpu_layers réduit + n_ctx /2 (32768→16384→8192...)

# Build
dev :
  pip install --break-system-packages -r requirements.txt
  python3 -m backend.main < ligne JSON  # test sidecar sans Tauri
  npm --prefix frontend install && npm run build  # 242 modules ~309kB
  npm run tauri dev  # Vite 1420 + Tauri (drag natif, preview token)
prod :
  pyinstaller --onefile --collect-all backend backend/main.py --distpath src-tauri/binaries
  npm run tauri build  # bundle SANS models/ (.taurignore) — modèles externes
```

## 2bis. Schémas ASCII

```
STOCKAGE (portable vs installé — même code, racine différente)
──────────────────────────────────────────────────────────────
 portable.json présent ? ──OUI──▶ <racine>/data/           (clé USB / dossier app)
         │                        ├─ database/thot-phase-1.db (WAL, 12 tables)
         NON                       ├─ blobs/sha256/ab/cd...bin (durable, hash)
         │                         ├─ previews/ (reconstructible)
         ▼                         ├─ derived/ temp/staged/ (volatil)
 ~/.local/share/thot/data/ (XDG)   ├─ trash/ (7j puis GC)
         │                         └─ models/ (miroir GGUF portable)
         └─ même arborescence ─────▶ (chemins relatifs + sha256 → T-ATT-18 OK)

models/ (7 fichiers, ~9 Go — GGUF ignorés git, README versionné)
 ├─ LLM/LFM2.5-8B-A1B-UD-Q4_K_M.gguf ............ 5.0G (orchestrateur+générateur, temp 0.2)
 ├─ Vision-document/GLM-OCR-f16.gguf ............ 1.7G } OCR 8K
 │   + mmproj-GLM-OCR-Q8_0.gguf ................ 484M }
 └─ Vision-general/LFM2.5-VL-3B-UD-Q4_K_XL.gguf . 1.6G } vision 32K NaFlex
     + mmproj-*-BF16/F16/Q8 .................... 856/854/583M }
```

```
TIMELINE VRAM (séquentiel strict — 1 modèle à la fois)
─────────────────────────────────────────────────────
temps ──────────────────────────────────────────────────▶
 [8B orchestre] → [unload 0.5s+5s] → [VL-3B vision] → [unload] → [8B génère]
       ≈9 Go gardé chaud        │         │                       │
  test manuel SKIP si <6 Go ────┘         │ (en-app OK : 1 sidecar)
  libres (conflit 2 processus,            │  _wait_for_vram_release(3s)
  pas un bug)                             │  KV Q8_0 · mmap 300ms

BUILD (dev vs prod)
 dev  : pip install -r requirements.txt → pytest → python3 -m backend.main (test JSONL)
        → npm --prefix frontend install && build (242 modules ~309kB) → npm run tauri dev (Vite 1420)
 prod : pyinstaller --onefile --collect-all backend → src-tauri/binaries/thot-sidecar-*
        → npm run tauri build (bundle SANS models/ — .taurignore — modèles externes)
```

## 3. Extraits réels

```bash
pip install --break-system-packages -r requirements.txt
python3 -m pytest backend/tests/ -v
echo '{"id":"test","method":"chat.send","params":{"message":"Bonjour","mode":"chat"}}' | python3 -m backend.main
cargo check --manifest-path src-tauri/Cargo.toml
```

```python
# backend/config/defaults.py
MODEL_CONFIGS = {orchestrator: LFM2.5-8B, vision: LFM2.5-VL-3B, ocr: GLM-OCR}
resolve_n_ctx(vram) → 131072 si ≥12GB sinon min(32768, tokens+1024)
```

## 4. Pièges

- `Thot v0.8 (Copie)` = backup, jamais modifié.
- Test VL manuel SKIP si < 6 Go libres (8B ≈ 9 Go gardé chaud dans le même processus applicatif — en-app pas de conflit car un seul sidecar séquentiel).
- T-ATT-18 (déplacement portable) : blobs survivent car chemins relatifs + sha256, jamais absolus en DB cible.
