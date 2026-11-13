#!/bin/bash
# Lance l'interface AMIAD sur le cluster : construit l'interface si besoin, puis démarre le back end Python qui la sert.
# Ouvrir ensuite http://localhost:8008 (VS Code redirige le port automatiquement ; sinon onglet « Ports » → 8008).
#   interface/run.sh            démarre (ou redémarre) en arrière-plan
#   interface/run.sh stop       arrête
set -euo pipefail
ROOT="$(cd "$(dirname "$0")/.." && pwd)"
cd "$ROOT"
PORT="${AMIAD_PORT:-8008}"
LOG=outputs/interface_cache/api.log
PID=outputs/interface_cache/api.pid
mkdir -p outputs/interface_cache
if [ -f "$PID" ] && kill -0 "$(cat "$PID")" 2>/dev/null; then kill "$(cat "$PID")"; sleep 1; fi
[ "${1:-}" = stop ] && { echo "arrêté"; exit 0; }
export PATH="$HOME/.local/node/bin:$PATH"
if [ ! -d interface/dist ] || [ -n "$(find interface/src interface/index.html -newer interface/dist -print -quit)" ]; then
  echo "construction de l'interface…"
  (cd interface && { [ -d node_modules ] || npm install --no-audit --no-fund; } && npm run build >/dev/null)
fi
OMP_NUM_THREADS="${OMP_NUM_THREADS:-8}" nohup .venv/bin/uvicorn interface.backend.amiad_api:app --host 127.0.0.1 --port "$PORT" > "$LOG" 2>&1 &
echo $! > "$PID"
echo "interface : http://localhost:$PORT  (journal : $LOG ; préparation des données : environ 1 minute au premier lancement)"
