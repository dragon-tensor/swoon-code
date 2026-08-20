#!/usr/bin/env sh
set -eu

setup_dir=$(CDPATH= cd -- "$(dirname -- "$0")" && pwd)

case "$(uname -s)" in
    Linux) exec "$setup_dir/linux/install.sh" "$@" ;;
    Darwin) exec "$setup_dir/macos/install.sh" "$@" ;;
    *)
        echo "Unsupported Unix platform. On Windows run setup\\windows\\install.cmd." >&2
        exit 1
        ;;
esac
