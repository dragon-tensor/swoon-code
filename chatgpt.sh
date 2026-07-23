#!/usr/bin/env bash
set -euo pipefail

VENV=/tmp/chatgpt-venv
BROWSERS=/tmp/chatgpt-browsers
SCRIPT="$(dirname "$0")/chatgpt_headless.py"

if [ ! -f "$SCRIPT" ]; then
    echo "Error: $SCRIPT not found" >&2
    exit 1
fi

export PLAYWRIGHT_BROWSERS_PATH="$BROWSERS"
exec "$VENV/bin/python" "$SCRIPT" "$@"
