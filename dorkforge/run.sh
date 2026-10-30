#!/usr/bin/env bash
# Start the DorkForge lead agent.
#   ./run.sh          -> live mode (scope gate decides what may be harvested)
#   DF_DEMO=1 ./run.sh -> offline demo corpus
set -euo pipefail
cd "$(dirname "$0")/.."

if [ ! -d .venv ]; then
  python3 -m venv .venv
fi
. .venv/bin/activate
pip install -q -r dorkforge/requirements.txt

HOST="${DF_HOST:-0.0.0.0}"
PORT="${DF_PORT:-8000}"

exec python3 -m uvicorn dorkforge.api:app --host "$HOST" --port "$PORT" "$@"
