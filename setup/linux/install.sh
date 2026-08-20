#!/usr/bin/env sh
set -eu

linux_dir=$(CDPATH= cd -- "$(dirname -- "$0")" && pwd)
setup_dir=$(CDPATH= cd -- "$linux_dir/.." && pwd)

distro=unknown
if [ -r /etc/os-release ]; then
    # shellcheck disable=SC1091
    . /etc/os-release
    distro=${ID:-unknown}
fi

case "$distro" in
    arch|manjaro|endeavouros) "$linux_dir/arch.sh" ;;
    debian|ubuntu|linuxmint|pop) "$linux_dir/debian-ubuntu.sh" ;;
    fedora|rhel|centos|rocky|almalinux) "$linux_dir/fedora.sh" ;;
    *)
        if ! command -v python3 >/dev/null 2>&1; then
            echo "Unsupported Linux distribution: install Python 3.11+ first." >&2
            exit 1
        fi
        echo "Unknown Linux distribution; continuing with the available Python runtime."
        ;;
esac

exec "$setup_dir/common/install-unix.sh" "$@"
