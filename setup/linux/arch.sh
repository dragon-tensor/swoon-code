#!/usr/bin/env sh
set -eu

missing=""
command -v python3 >/dev/null 2>&1 || missing="$missing python"
command -v bwrap >/dev/null 2>&1 || missing="$missing bubblewrap"
command -v prlimit >/dev/null 2>&1 || missing="$missing util-linux"

if [ -n "$missing" ]; then
    sudo pacman -S --needed --noconfirm $missing
fi
