#!/usr/bin/env bash
set -euo pipefail

SCRIPT_DIR="$(cd -- "$(dirname -- "$0")" && pwd)"
VENV="$SCRIPT_DIR/.venv"
SCRIPT="$SCRIPT_DIR/chatgpt_agent.py"

if [ ! -f "$SCRIPT" ]; then
    echo "Error: $SCRIPT not found" >&2
    exit 1
fi

if [ ! -x "$VENV/bin/python" ]; then
    echo "Error: $VENV/bin/python not found; create the local virtual environment first" >&2
    exit 1
fi

exec "$VENV/bin/python" "$SCRIPT" "$@"
