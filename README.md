# Thot Phase 1 — Chat + Agent loop (clean)

Produit standalone, inspiré de Thot v0.8 mais **sans toucher à v0.8**. **Pas de .venv, pas de uv — tout en PyInstaller.**

## Stack
- **Frontend** : React 19 + Vite + Tauri 2 (IPC `invoke`/`listen`, JSON Lines)
- **Backend Rust** : Tauri sidecar management (stdin/stdout, `window.emit`)
- **Backend Python** : `llama-cpp-python` (LFM2.5-8B-A1B 128K texte pur + GLM-OCR f16 1.7G + LFM2.5-VL-3B Q4_K_XL 1.6G), agent loop 2 couches séquentiel
- **DB** : `data/thot-phase-1.db` (4 tables fresh start, CASCADE + INDEX)
- **Mémoire** : séquentiel strict 1 modèle à la fois, borne 7.5 GB (mmap + `resolve_n_ctx` adaptatif 32K/128K)
- **Modèles présents** : `models/LLM/LFM2.5-8B-A1B-UD-Q4_K_M.gguf` (5.0G), `models/Vision-document/GLM-OCR-f16.gguf` (1.7G), `models/Vision-general/LFM2.5-VL-3B-UD-Q4_K_XL.gguf` (1.6G) — **pas de mmproj, pas de Qwen**
- **Build** : **PyInstaller --onefile --collect-all backend** → `src-tauri/binaries/thot-sidecar-x86_64-unknown-linux-gnu` (7.8M, pas de venv, CUDA 0.3.34 cu121 fixé)
- **Frontend** : `frontend/index.html` + `src/App.tsx` (4 pages Chat/Agent) + `src/main.tsx` — écran blanc corrigé (Vite 1420)

## Arborescence
Voir `SPEC.md` (copie propre de `Futur/Notes.md` § Arborescence validée) — scaffold complet avec notes FR.

## Lancer en dev (sans venv)
```bash
# 1. Modèles — déjà déplacés depuis Thot v0.8
ls -lh models/LLM/ models/Vision-document/ models/Vision-general/  # 5.0G + 1.7G + 1.6G

# 2. Dépendances (Ubuntu 24.04 = PEP 668)
pip install --break-system-packages -r requirements.txt
# Fix CUDA 4070 SUPER (wheel 0.3.35 SIGILL → rester en 0.3.34 cu121) :
# pip install --break-system-packages llama-cpp-python==0.3.34 --extra-index-url https://abetlen.github.io/llama-cpp-python/whl/cu121 --force-reinstall --no-cache-dir
# Vérif : python3 -c "from llama_cpp import llama_supports_gpu_offload; print(llama_supports_gpu_offload())"  # True

# 3. Test Python sidecar (JSON Lines) — utilise python3 -m
echo '{"id":"test","method":"chat.send","params":{"message":"Bonjour","mode":"chat"}}' | python3 -m backend.main
# Attendu : backend_ready (cuda) → thinking → answer streaming → message_completed

# 4. Packager le sidecar (PyInstaller — collecte backend pour éviter ModuleNotFoundError)
pyinstaller --onefile --name thot-sidecar-x86_64-unknown-linux-gnu --collect-all backend backend/main.py --distpath src-tauri/binaries --clean --noconfirm
# Vérif : ls -lh src-tauri/binaries/thot-sidecar-x86_64-unknown-linux-gnu  # 7.8M

# 5. Tauri dev
npm install                    # root (tauri cli)
npm --prefix frontend install  # frontend (React/Vite)
npm run tauri dev              # depuis Thot-phase-1/ (pas frontend/) — Vite sur 1420, Tauri devUrl 1420
```

## Build prod
```bash
pyinstaller --onefile --name thot-sidecar-x86_64-unknown-linux-gnu --collect-all backend backend/main.py --distpath src-tauri/binaries --clean --noconfirm
npm run tauri build  # bundle sans models/ (via .taurignore) — models restent externes
```

## Spec
Toute la spec est dans `SPEC.md` (copie de `Futur/Notes.md`, 642 lignes, 100% scaffoldable) — **Notes en français dans tous les codes**.

## Vérif
```bash
pip install --break-system-packages pytest && python3 -m pytest backend/tests/  # 5 passed (cuda, cpu fallback, estimate_layers)
python3 -m backend.main  # doit émettre {"event":"backend_ready",...} sur stdout (pas python backend/main.py)
```

## Changements 30/08 (écran blanc + sidecar)
- **Écran blanc** : `frontend/index.html` + `src/App.tsx` (nav Chat/Agent) + `vite.config.ts` port 1420 (était 5173) + `src/main.tsx` — corrigé
- **Sidecar** : `ModuleNotFoundError: No module named 'backend'` → `--collect-all backend` + `sys.path` fix dans `backend/main.py`
- **Tauri** : `tauri.conf.json` et `capabilities/default.json` nettoyés des `//` (JSON invalide) + `Cargo.toml`/`build.rs`/`lib.rs` scaffoldés + `package.json` racine pour `npm run tauri dev`
- **CUDA** : figé `llama-cpp-python==0.3.34 cu121` (0.3.35 SIGILL sur H370 AVX2)
