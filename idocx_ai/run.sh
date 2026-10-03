#!/bin/bash
# iDocx AI backend + test page  ->  http://<this machine's IP>:8100
# Needs the model server on :8080 (idp-endpoint/start_model.sh).
# Open to the LAN with no login: test with made-up documents only.
# HOST=127.0.0.1 bash run.sh   to keep it to this machine.
cd "$(dirname "$0")"
[ -x .venv/bin/python ] || { echo "no .venv -- see requirements.txt"; exit 1; }
export LLAMA_ENDPOINT="${LLAMA_ENDPOINT:-http://localhost:8080}"
exec .venv/bin/python -m uvicorn app:app --host "${HOST:-0.0.0.0}" --port "${PORT:-8100}"
