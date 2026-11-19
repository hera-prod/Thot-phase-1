#!/usr/bin/env bash
# ingestion/setup_worker_env.sh — Recrée le venv du worker PaddleOCR-VL (brouillon).
# POURQUOI UN VENV DÉDIÉ : le modèle exige transformers==4.56.* (ni le 5.x système,
# ni un 4.x trop vieux). --system-site-packages réutilise torch/PIL système (pas de
# re-téléchargement CUDA). Idempotent : relançable sans risque après reboot (/tmp).
set -euo pipefail
VENV="${1:-${TMPDIR:-/tmp}/thot-diag/paddleocr-env}"
python3 -m venv --system-site-packages "$VENV"
"$VENV/bin/pip" install -q "transformers==4.56.0"
"$VENV/bin/python" -c "import transformers; assert transformers.__version__ == '4.56.0', transformers.__version__; print('worker env ok : transformers 4.56.0')"
