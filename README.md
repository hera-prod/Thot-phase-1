# Thot

Assistant conversationnel **local** (< 8 Go de VRAM), multiplateforme, construit par incréments en cinq phases.

- **Motif agentique** : un modèle (LFM 2.5 8B), deux rôles — orchestrateur (planification + outils) et générateur (rédaction).
- **Contrainte matérielle** : un seul modèle résident en VRAM à la fois (swap séquentiel).
- **Stack** : Tauri 2 + React/Vite, sidecar Python `llama-cpp-python` (GGUF), SQLite/SQLAlchemy, IPC JSON Lines (stdin/stdout).

## Documentation

Toute la spécification, les annexes (références scientifiques, guides Python) et le pseudo-code annoté se trouvent dans le dossier [`documentation/`](documentation/README.md).

- [Index central](documentation/README.md)
- [Sommaire des annexes](documentation/annexes/README.md)
- [Sommaire du pseudo-code](documentation/pseudocode/README.md)

## Structure du dépôt

```
backend/        # sidecar Python (phase 1) — llama-cpp-python, agent loop, outils, SQLite
frontend/       # React + Vite + Tauri 2 — pages, composants
src-tauri/      # coquille Rust (sidecar manager)
documentation/  # spec des 5 phases + annexes + pseudo-code
```

## Lancer en développement

```bash
# 1. Dépendances (Ubuntu 24.04 — PEP 668)
pip install --break-system-packages -r requirements.txt

# 2. Modèles (GGUF) attendus dans models/LLM, models/Vision-general, models/Vision-document

# 3. Test du sidecar (JSON Lines)
echo '{"id":"test","method":"chat.send","params":{"message":"Bonjour","mode":"chat"}}' | python3 -m backend.main

# 4. Frontend + Tauri
npm install
npm --prefix frontend install
npm run tauri dev
```

## Build

```bash
pyinstaller --onefile --name thot-sidecar-x86_64-unknown-linux-gnu --collect-all backend backend/main.py --distpath src-tauri/binaries --clean --noconfirm
npm run tauri build
```

> Voir [`documentation/README.md`](documentation/README.md) pour la vision produit complète et le découpage en phases.