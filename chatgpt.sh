#!/usr/bin/env bash
set -euo pipefail

VENV=/tmp/chatgpt-venv
SCRIPT="$(dirname "$0")/chatgpt_agent.py"

if [ ! -f "$SCRIPT" ]; then
    echo "Error: $SCRIPT not found" >&2
    exit 1
fi

exec "$VENV/bin/python" "$SCRIPT" "$@"

