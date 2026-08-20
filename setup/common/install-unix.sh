#!/usr/bin/env sh
set -eu

setup_dir=$(CDPATH= cd -- "$(dirname -- "$0")/.." && pwd)
project_root=$(CDPATH= cd -- "$setup_dir/.." && pwd)
codebase_dir="$project_root/codebase"
runtime_dir="$setup_dir/.runtime"
venv_dir="$runtime_dir/venv"
work_dir="$project_root/work"
launcher_dir="${HOME}/.local/bin"
launcher="$launcher_dir/swoon"

if ! command -v python3 >/dev/null 2>&1; then
    echo "Python 3.11 or newer is required and was not found." >&2
    exit 1
fi

python_version=$(python3 -c 'import sys; print(f"{sys.version_info.major}.{sys.version_info.minor}")')
python3 -c 'import sys; raise SystemExit(sys.version_info < (3, 11))' || {
    echo "Python 3.11 or newer is required; found $python_version." >&2
    exit 1
}

mkdir -p "$runtime_dir" "$work_dir/input" "$work_dir/output" "$launcher_dir"
python3 -m venv "$venv_dir"
"$venv_dir/bin/python" -m pip install "$codebase_dir"
"$venv_dir/bin/python" -m playwright install chromium

case "$(uname -s)" in
    Darwin) config_dir="${HOME}/Library/Application Support/Swoon Code" ;;
    *) config_dir="${XDG_CONFIG_HOME:-${HOME}/.config}/swoon-code" ;;
esac
mkdir -p "$config_dir"
chmod 700 "$config_dir"

cookie_source=${1:-}
if [ -z "$cookie_source" ] && [ -f "$codebase_dir/cookies.json" ]; then
    cookie_source="$codebase_dir/cookies.json"
fi
if [ -z "$cookie_source" ]; then
    printf 'Path to your exported chatgpt.com cookies.json: '
    IFS= read -r cookie_source
fi
if [ ! -f "$cookie_source" ]; then
    echo "Cookie file not found: $cookie_source" >&2
    exit 1
fi
cookie_target="$config_dir/cookies.json"
cp "$cookie_source" "$cookie_target"
chmod 600 "$cookie_target"

{
    printf '%s\n' '#!/usr/bin/env sh' 'set -eu'
    printf "export SWOON_WORK_ROOT='%s'\n" "$work_dir"
    printf "export SWOON_COOKIE_FILE='%s'\n" "$cookie_target"
    printf "exec '%s' -m swoon \"\$@\"\n" "$venv_dir/bin/python"
} > "$launcher"
chmod 700 "$launcher"

case ":${PATH}:" in
    *":${launcher_dir}:"*) ;;
    *)
        profile="${HOME}/.profile"
        path_line='export PATH="$HOME/.local/bin:$PATH"'
        if [ ! -f "$profile" ] || ! grep -F "$path_line" "$profile" >/dev/null 2>&1; then
            printf '\n%s\n' "$path_line" >> "$profile"
        fi
        ;;
esac

echo "Swoon Code is installed."
echo "Open a new terminal and run: swoon"
echo "For a named workspace run: swoon my-project"
