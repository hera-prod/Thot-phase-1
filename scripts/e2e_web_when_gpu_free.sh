#!/usr/bin/env bash
# Lance l'E2E orchestrateur web_research DÈS que la VRAM le permet.
# Le job vidéo du propriétaire (ou ComfyUI) peut tenir le GPU au moment où on
# écrit ce script : on attend, on ne tue rien, et on note l'heure de départ
# pour que la mesure soit datée.
set -u
cd "$(dirname "$0")/.."

BESOIN_MIB=6500            # LFM2.5-8B-A1B-UD-Q4_K_M fait 5,0 Go + marge
ATTENTE_S=60
MAX_ATTENTES=240           # ~4 h

for i in $(seq 1 "$MAX_ATTENTES"); do
  libre=$(nvidia-smi --query-gpu=memory.free --format=csv,noheader,nounits 2>/dev/null | head -1)
  if [ -z "$libre" ]; then
    echo "[watch] nvidia-smi muet, nouvel essai dans ${ATTENTE_S}s"
  elif [ "$libre" -ge "$BESOIN_MIB" ]; then
    echo "[watch] ${libre} Mio libres >= ${BESOIN_MIB} — lancement $(date -Is)"
    rm -rf /tmp/thot_web_orch && mkdir -p /tmp/thot_web_orch/data/database
    THOT_E2E_DIR=/tmp/thot_web_orch/data \
    THOT_DATA_DIR=/tmp/thot_web_orch/data \
    THOT_DB_PATH=/tmp/thot_web_orch/data/database/thot.db \
    THOT_WEB_MODE=web_allowed \
    python3 tests/backend/manual/e2e_web_orchestrator.py \
      definition scientifique source-uri propre actu
    echo "[watch] terminé $(date -Is)"
    exit 0
  else
    echo "[watch] ${libre} Mio libres < ${BESOIN_MIB} — occupation tierce, essai ${i}/${MAX_ATTENTES}"
  fi
  sleep "$ATTENTE_S"
done
echo "[watch] abandon : la VRAM n'a jamais été libre assez longtemps"
exit 1
