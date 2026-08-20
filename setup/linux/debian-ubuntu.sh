#!/usr/bin/env sh
set -eu

if ! command -v python3 >/dev/null 2>&1 || \
   ! command -v bwrap >/dev/null 2>&1 || \
   ! command -v prlimit >/dev/null 2>&1; then
    sudo apt-get update
    sudo apt-get install --yes python3 python3-venv python3-pip bubblewrap util-linux
fi
