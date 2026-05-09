#!/usr/bin/env bash
# Install Python dependencies required by the noaa-weather tool set.
#
# Currently:
#   requests     — HTTP client for NOAA S3 + Nominatim + Geofabrik
#                  (already in the main dev extras; we double-check here so
#                  a stripped-down venv works out of the box)
#   matplotlib   — SVG chart rendering for climate-report
#                  (climograph, warming stripes, heatmap, anomaly bars)
#
# Installs into ${REPO_ROOT}/.venv via ``python -m pip``. Idempotent —
# re-running only installs what's missing. Uses ``python -m pip``
# instead of the ``.venv/bin/pip`` wrapper because the latter hardcodes
# the interpreter path in its shebang and breaks if the repo is renamed
# (see the .sh wrappers for the same reason).

set -euo pipefail

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
REPO_ROOT="$(cd "${SCRIPT_DIR}/../../.." && pwd)"
PY="${REPO_ROOT}/.venv/bin/python3"

if [ -t 1 ]; then
    BOLD=$'\033[1m'
    GREEN=$'\033[32m'
    YELLOW=$'\033[33m'
    RED=$'\033[31m'
    RESET=$'\033[0m'
else
    BOLD="" GREEN="" YELLOW="" RED="" RESET=""
fi

log()  { printf '%s==>%s %s\n' "$BOLD" "$RESET" "$*"; }
ok()   { printf '%s[ok]%s %s\n' "$GREEN" "$RESET" "$*"; }
warn() { printf '%s[warn]%s %s\n' "$YELLOW" "$RESET" "$*"; }
fail() { printf '%s[fail]%s %s\n' "$RED" "$RESET" "$*" >&2; exit 1; }

if [ ! -x "$PY" ]; then
    fail ".venv not found at ${PY}. Create it first: python3.13 -m venv ${REPO_ROOT}/.venv"
fi

# Required Python packages + the import-name probe we use to check presence.
REQUIREMENTS=(
    "requests:requests"
    "matplotlib:matplotlib"
)

MISSING=()
for spec in "${REQUIREMENTS[@]}"; do
    pkg="${spec%%:*}"
    mod="${spec##*:}"
    if "$PY" -c "import ${mod}" 2>/dev/null; then
        ok "${pkg} already installed"
    else
        MISSING+=("$pkg")
    fi
done

if [ ${#MISSING[@]} -gt 0 ]; then
    log "installing: ${MISSING[*]}"
    "$PY" -m pip install --disable-pip-version-check "${MISSING[@]}"
    for spec in "${REQUIREMENTS[@]}"; do
        pkg="${spec%%:*}"
        mod="${spec##*:}"
        if "$PY" -c "import ${mod}" 2>/dev/null; then
            ok "${pkg} installed"
        else
            fail "${pkg} still not importable after install — check the pip output"
        fi
    done
fi

log "noaa-weather tool dependencies ready"
