#!/usr/bin/env sh
set -eu

dev_dir=$(CDPATH= cd -- "$(dirname -- "$0")" && pwd)
setup_dir=$(CDPATH= cd -- "$dev_dir/.." && pwd)
project_root=$(CDPATH= cd -- "$setup_dir/.." && pwd)
name=${1:-dev}

export SWOON_WORK_ROOT="$project_root/work"
if [ -z "${SWOON_COOKIE_FILE:-}" ]; then
    if [ -f "$project_root/codebase/cookies.json" ]; then
        export SWOON_COOKIE_FILE="$project_root/codebase/cookies.json"
    fi
fi

exec "$setup_dir/.runtime/venv/bin/python" -m swoon "$name" --headed --verbose
