# Lancer Thot Phase 1 — Guide de test (PyInstaller, sans venv)

> **But :** vérifier que le scaffold `Thot-phase-1` fonctionne, du test le plus rapide au lancement complet.

---

## 0. Pré-requis

Vérifie les versions et que les modèles sont bien déplacés (pas de mmproj, pas de Qwen) :

```bash
python3 --version  # ≥3.11 et <3.13 (wheels GPU dispo 3.11/3.12)
pip --version
rustc --version
npm --version

# Modèles Phase 1 (déjà déplacés depuis Thot v0.8)
ls -lh models/LLM/LFM2.5-8B-A1B-UD-Q4_K_M.gguf           # 5.0G
ls -lh models/Vision-document/GLM-OCR-f16.gguf          # 1.7G
ls -lh models/Vision-general/LFM2.5-VL-3B-UD-Q4_K_XL.gguf # 1.6G
```

---

## 1. Tests unitaires (30s, sans GPU, sans modèle)

Le plus rapide pour vérifier la cascade GPU et le chargement séquentiel :

```bash
# Tu es déjà dans Thot-phase-1/ (pas besoin de cd)
pwd  # doit afficher .../Thot-phase-1

# Installation dépendances (Ubuntu 24.04 = PEP 668, pip système bloqué)
pip install --break-system-packages -r requirements.txt
# si tu préfères ne pas forcer système :
# pipx install pytest && pipx inject pytest pymupdf sqlalchemy pillow
# ou minimal :
# pip install --break-system-packages pytest pymupdf sqlalchemy pillow

# Lancer les tests (utilise python3, pas python)
python3 -m pytest backend/tests/ -v
```

**Attendu :**
- `test_cuda_detection` — mock nvidia-smi → `backend='cuda'`
- `test_cpu_fallback` — aucun GPU → `backend='cpu', n_gpu_layers=0`
- `test_estimate_layers` — formule 0 ≤ layers ≤ 24
- `test_supports_vision_guard` — 8B=False, VL-3B=True

Si `supports_gpu_offload=False` alors que tu as un GPU → wheel CPU seul (cache pollué) :
```bash
pip install llama-cpp-python --extra-index-url https://abetlen.github.io/llama-cpp-python/whl/cu121 --force-reinstall --no-cache-dir
```

---

## 2. Détection hardware (vérifie la cascade par vendor)

```bash
python3 -c "from backend.inference.hardware import HardwareDetector; d=HardwareDetector(); print(d.detect()); print('GPU offload:', d.supports_gpu_offload())"
# Note : `python` seul n'existe pas sur Ubuntu, utilise toujours `python3`
```

**Attendu :**
```
BackendConfig(backend='cuda', gpu_name='NVIDIA GeForce RTX ...', total_vram=..., free_vram=..., n_gpu_layers=-1)
GPU offload: True
```
- `cuda` → NVIDIA, `rocm` → AMD, `metal` → Apple, `vulkan` → fallback, `cpu` → aucun GPU
- `n_gpu_layers=-1` = tout sur GPU si assez de VRAM, sinon partiel calculé par `estimate_layers()`

---

## 3. IPC Python seul (sans Tauri, sans frontend)

Teste le sidecar en JSON Lines sur stdin/stdout (le cœur de l'archi) :

```bash
# Test IPC (2 façons, les deux marchent maintenant)
echo '{"id":"test-1","method":"chat.send","params":{"message":"Bonjour","mode":"chat"}}' | python3 -m backend.main
# ou : echo '...' | PYTHONPATH=. python3 backend/main.py

# Si GPU offload=False (wheel CPU seul) alors que tu as une 4070 SUPER :
pip install --break-system-packages llama-cpp-python --extra-index-url https://abetlen.github.io/llama-cpp-python/whl/cu121 --force-reinstall --no-cache-dir
python3 -c "from llama_cpp import llama_supports_gpu_offload; print(llama_supports_gpu_offload())"  # doit afficher True
```

**Attendu :**
- Sur `stdout` (une ligne = un JSON) :
  ```json
  {"event":"backend_ready","payload":{"backend":"cuda","vram":...,"n_gpu_layers":-1}}
  {"event":"message_started","payload":{"request_id":"test-1","run_id":"test-1","model":"LFM2.5-8B"}}
  {"event":"thinking","payload":{"request_id":"test-1","text":"Planification...","iteration":0}}
  {"event":"answer","payload":{"request_id":"test-1","text":"..."}}
  {"event":"message_completed","payload":{"request_id":"test-1","run_id":"test-1","tokens_in":...}}
  ```
- Sur `stderr` (logs, jamais stdout) :
  ```
  [thot-phase-1] backend_ready backend=cuda vram=11500MB
  ```

**Si blocage ou OOM :**
- Vérifie `resolve_n_ctx` adaptatif : 128K natif si ≥12GB, sinon 32K (évite 11.5GB sur 8GB)
- Logs détaillés : `echo ... | python backend/main.py 2> /tmp/thot.log && cat /tmp/thot.log`

---

## 4. Build PyInstaller + Tauri (complet)

### 4a. Packager le sidecar Python en binaire autonome

```bash
# Depuis Thot-phase-1/
pyinstaller --onedir --name thot-sidecar backend/main.py --distpath src-tauri/binaries/ --clean --noconfirm

# Vérifie
ls -lh src-tauri/binaries/thot-sidecar/
# doit contenir : thot-sidecar (binaire) + _internal/ (libs)
```

**Note :** `--onedir` obligatoire (pas `--onefile`, bug bootloader double PID). Le binaire doit être nommé `thot-sidecar-<triple>` si tu build pour plusieurs cibles.

### 4b. Lancer l'app en dev

```bash
# Depuis Thot-phase-1/ (racine, pas frontend/)
npm install                    # installe @tauri-apps/cli au root
npm --prefix frontend install  # installe React/Vite du frontend

# Lance Tauri (le beforeDevCommand va démarrer Vite sur port 1420)
npm run tauri dev
# Si erreur "tauri: command not found" → npx tauri dev
```

**Attendu :**
- Fenêtre "Thot Phase 1" s'ouvre
- Console Rust affiche `[python] backend_ready ...`
- Dans l'app : envoie "Bonjour" en mode `chat` → tu vois `thinking` → `answer` en streaming
- En mode `agent` avec image : `tool_call vision` → `tool_result` → `answer`

### 4c. Build prod

```bash
pyinstaller --onedir --name thot-sidecar backend/main.py --distpath src-tauri/binaries/ --clean --noconfirm
npm run tauri build
# Bundle sans models/ (via .taurignore) — models restent externes
```

---

## Dépannage rapide

| Symptôme | Check |
|----------|-------|
| `supports_gpu_offload=False` | `pip install --force-reinstall --no-cache-dir` avec le bon `whl/cu121` |
| `CUDA out of memory` | Vérifie `free_vram` et `resolve_n_ctx` (doit passer en 32K si 8GB) |
| `thot-sidecar introuvable` | `ls src-tauri/binaries/` doit contenir le dossier `thot-sidecar/` après pyinstaller |
| `JSON parse error` | Vérifie `PYTHONUNBUFFERED=1` et `flush()` dans `backend/inference/ipc.py` (déjà fait) |
| Events non reçus frontend | Vérifie `window.emit` côté Rust et `listen` côté TS (`frontend/src/lib/tauri.ts`) |

---

## Spec complète

Toute la spec est dans `SPEC.md` (642 lignes, copie de `Futur/Notes.md`) — notes en français dans tous les codes.
