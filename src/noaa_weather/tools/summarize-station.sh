#!/usr/bin/env bash
# Thin shell wrapper. See `summarize-station.sh --help` and the module docstring for details.
set -euo pipefail

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
REPO_ROOT="$(cd "${SCRIPT_DIR}/../../.." && pwd)"

if [ -f "${REPO_ROOT}/scripts/_env.sh" ]; then
    # shellcheck disable=SC1091
    source "${REPO_ROOT}/scripts/_env.sh"
fi

# Call the venv's python3 directly — sourcing .venv/bin/activate can silently
# fail if VIRTUAL_ENV hardcoded in the activate script no longer matches the
# repo's on-disk path (e.g. after a repo rename). Calling the interpreter
# directly gets the venv's site-packages regardless of PATH state.
PY="${REPO_ROOT}/.venv/bin/python3"
if [ ! -x "$PY" ]; then
    PY="python3"
fi

exec "$PY" "${SCRIPT_DIR}/summarize_station.py" "$@"
