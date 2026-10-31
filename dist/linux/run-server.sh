#!/bin/sh
set -eu

server_dir=$(CDPATH= cd -- "$(dirname -- "$0")" && pwd)
python_bin=${GRFS_PYTHON:-python3}
if ! command -v "$python_bin" >/dev/null 2>&1; then
    printf '%s\n' 'Python 3.11 or newer is required. Install python3 or set GRFS_PYTHON.' >&2
    exit 1
fi
"$python_bin" -c 'import sys; sys.exit(0 if sys.version_info >= (3, 11) else "Python 3.11 or newer is required.")'

cd "$server_dir"
export GRFS_RUNTIME_DIR="$server_dir"
exec "$python_bin" -X utf8 -m grfs "$@"
