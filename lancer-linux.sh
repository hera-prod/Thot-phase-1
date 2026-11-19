#!/usr/bin/env bash
# Lanceur Linux — Thot Phase 1 (code source).
# Installe les dépendances au premier lancement, puis démarre Electron.
set -euo pipefail
unset PYTHONPATH PYTHONHOME

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
cd "$SCRIPT_DIR"

echo "[Thot] Dossier : $SCRIPT_DIR"

if ! command -v python3 >/dev/null 2>&1; then
  echo "[Thot] Python 3 est requis (>= 3.11)." >&2
  exit 1
fi
if ! command -v node >/dev/null 2>&1; then
  echo "[Thot] Node.js est requis (>= 22.12.0)." >&2
  exit 1
fi
NODE_RAW="$(node --version)"
NODE_VERSION="${NODE_RAW#v}"
if [ -z "$NODE_VERSION" ]; then
  echo "[Thot] Impossible de lire la version de Node.js." >&2
  exit 1
fi
NODE_MAJOR="${NODE_VERSION%%.*}"
NODE_MINOR="${NODE_VERSION#*.}"
NODE_MINOR="${NODE_MINOR%%.*}"
if [ "$NODE_MAJOR" -lt 22 ] || { [ "$NODE_MAJOR" -eq 22 ] && [ "$NODE_MINOR" -lt 12 ]; }; then
  echo "[Thot] Node.js >= 22.12.0 est requis (version détectée : $NODE_RAW). Mettez Node.js à jour." >&2
  exit 1
fi
if ! command -v npm >/dev/null 2>&1; then
  echo "[Thot] npm est requis." >&2
  exit 1
fi

if [ -z "${DISPLAY:-}" ] && [ -z "${WAYLAND_DISPLAY:-}" ]; then
  echo "[Thot] Aucun affichage détecté (DISPLAY/WAYLAND_DISPLAY). Lancez depuis un bureau graphique." >&2
  exit 1
fi

if [ ! -x ".venv/bin/python" ]; then
  echo "[Thot] Création de l'environnement Python..."
  python3 -m venv .venv
fi

PYTHON="$SCRIPT_DIR/.venv/bin/python"
if [ ! -f ".venv/.thot-deps-installed" ]; then
  echo "[Thot] Installation des dépendances Python (peut prendre du temps)..."
  "$PYTHON" -m pip install --upgrade pip
  "$PYTHON" -m pip install -r requirements.txt
  touch ".venv/.thot-deps-installed"
fi

if [ ! -d "node_modules" ]; then
  echo "[Thot] Installation des dépendances Node (racine)..."
  npm install
fi
if [ ! -d "frontend/node_modules" ]; then
  echo "[Thot] Installation des dépendances Node (frontend)..."
  npm --prefix frontend install
fi

if [ ! -d "models" ] || [ -z "$(find models -mindepth 1 -maxdepth 1 -type d -print -quit 2>/dev/null)" ]; then
  echo "[Thot] Avertissement : aucun modèle dans models/. L'interface démarrera, mais l'inférence sera indisponible."
fi

export THOT_DEV_PYTHON="$PYTHON"
export THOT_PORTABLE=1
echo "[Thot] Lancement..."
npm start
