#!/bin/bash
# Lance l'interface AMIAD sur le cluster : construit l'interface si besoin, puis démarre le back end Python qui la sert.
# Ouvrir ensuite http://localhost:8008 (VS Code redirige le port automatiquement ; sinon onglet « Ports » → 8008).
#   interface/run.sh            démarre (ou redémarre) en arrière-plan
#   interface/run.sh --llm      idem, et lance le modèle de langue local adapté à la machine (deploy/llm_launch.py :
#                               Qwen3-VL-8B, sur GPU si présent, sinon sur CPU ; poids téléchargés de Hugging Face si absents)
#   interface/run.sh stop       arrête (et le modèle de langue lancé par --llm)
set -euo pipefail
ROOT="$(cd "$(dirname "$0")/.." && pwd)"
cd "$ROOT"
PORT="${AMIAD_PORT:-8008}"
HOST="${AMIAD_HOST:-127.0.0.1}"   # AMIAD_HOST=0.0.0.0 : accessible depuis le réseau (http://<adresse du cluster>:PORT)
LOG=outputs/interface_cache/api.log
PID=outputs/interface_cache/api.pid
LLM_PID=outputs/interface_cache/llm.pid
mkdir -p outputs/interface_cache
if [ -f "$PID" ] && kill -0 "$(cat "$PID")" 2>/dev/null; then kill "$(cat "$PID")"; sleep 1; fi
if [ "${1:-}" = stop ]; then
  if [ -f "$LLM_PID" ] && kill -0 "$(cat "$LLM_PID")" 2>/dev/null; then kill "$(cat "$LLM_PID")"; fi
  echo "arrêté"; exit 0
fi
export PATH="$HOME/.local/node/bin:$PATH"  # Node.js: building the interface and the rule search (RAG/search)
if [ ! -d interface/dist ] || [ -n "$(find interface/src interface/index.html -newer interface/dist -print -quit)" ]; then
  echo "construction de l'interface…"
  (cd interface && { [ -d node_modules ] || npm install --no-audit --no-fund; } && npm run build >/dev/null)
fi
CUDA_VISIBLE_DEVICES="" OMP_NUM_THREADS="${OMP_NUM_THREADS:-8}" nohup .venv/bin/uvicorn interface.backend.amiad_api:app --host "$HOST" --port "$PORT" > "$LOG" 2>&1 &
echo $! > "$PID"
if [ "${1:-}" = --llm ] && ! { [ -f "$LLM_PID" ] && kill -0 "$(cat "$LLM_PID")" 2>/dev/null; }; then
  nohup .venv/bin/python deploy/llm_launch.py > outputs/interface_cache/llm-launch.log 2>&1 &
  echo $! > "$LLM_PID"
  echo "modèle de langue : $(.venv/bin/python deploy/llm_launch.py --dry-run) — journal outputs/interface_cache/llm-launch.log"
fi
echo "interface : http://localhost:$PORT  (journal : $LOG ; préparation des données : environ 1 minute au premier lancement)"
