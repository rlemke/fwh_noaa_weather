#!/usr/bin/env bash
# Generate climate reports for every top-level Geofabrik continent +
# every sub-region beneath each one. Covers essentially the whole
# index in ~8 command invocations.
#
# Same effect as `climate-report.sh --all` but one continent per call
# so progress is broken up + failures don't cascade across continents.

set -euo pipefail

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"

# Quote the flag bundle — otherwise bash splits on whitespace and tries
# to run `export` on each token as a KEY=VALUE assignment.
PARAM=(
    --start-year 1950
    --end-year 2025
    --i-know-this-is-huge
    --max-stations 10
    --jobs 4
    --include-parents
)

# Geofabrik's top-level region slugs (verified against the cached
# index-v1.json). Russia is indexed at the top level — not under
# Europe or Asia. Antarctica exists but has almost no GHCN stations.
CONTINENTS=(
    north-america
    central-america
    south-america
    europe
    asia
    africa
    australia-oceania
    russia
    antarctica
)

for continent in "${CONTINENTS[@]}"; do
    echo
    echo "=== $continent ==="
    "${SCRIPT_DIR}/climate-report.sh" --all-under "$continent" "${PARAM[@]}" || {
        echo "[continue] $continent exited non-zero — moving on"
    }
done
