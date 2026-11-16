#!/bin/bash
# iDocx AI backend + test page  ->  http://<this machine's IP>:8100
# Needs a llama.cpp model server; point at it with LLAMA_ENDPOINT (default http://localhost:8080).
# Open to the LAN with no login: test with made-up documents only.
# HOST=127.0.0.1 bash run.sh   to keep it to this machine.
cd "$(dirname "$0")"
[ -x .venv/bin/python ] || { echo "no .venv -- run: uv sync"; exit 1; }
export LLAMA_ENDPOINT="${LLAMA_ENDPOINT:-http://localhost:8080}"
exec .venv/bin/python -m uvicorn app:app --host "${HOST:-0.0.0.0}" --port "${PORT:-8100}"
