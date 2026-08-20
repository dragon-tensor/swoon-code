#!/usr/bin/env sh
set -eu

macos_dir=$(CDPATH= cd -- "$(dirname -- "$0")" && pwd)
setup_dir=$(CDPATH= cd -- "$macos_dir/.." && pwd)

if ! command -v python3 >/dev/null 2>&1; then
    if command -v brew >/dev/null 2>&1; then
        brew install python
    else
        echo "Python 3.11+ is required. Install Python from python.org or Homebrew, then rerun." >&2
        exit 1
    fi
fi

exec "$setup_dir/common/install-unix.sh" "$@"
