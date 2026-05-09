#!/usr/bin/env bash
# Generate a climate report for every US state (or a subset), using the
# Geofabrik region path as the spatial filter for each one.
#
# Equivalent to running, per state:
#   ./climate-report.sh --start-year 1950 --end-year 2026 \
#       --region north-america/us/<state> --i-know-this-is-huge
#
# Usage:
#   ./report-states.sh                                 # all 50 states
#   ./report-states.sh texas california new-york       # subset, positional
#   ./report-states.sh --start-year 1970 --end-year 2020
#   ./report-states.sh --states "texas new-york"       # subset via flag
#   ./report-states.sh --stop-on-fail                  # bail at first error
#
# Geofabrik state names are lowercase with hyphens
# (``new-york``, ``new-jersey``, ``district-of-columbia``, etc.).
#
# Any flag this script doesn't consume is forwarded to every
# ``climate-report.sh`` invocation — so ``--max-stations 50 --use-mock``
# or ``--baseline 1981-2010`` will propagate.

set -uo pipefail

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"

if [ -t 1 ]; then
    BOLD=$'\033[1m'
    GREEN=$'\033[32m'
    YELLOW=$'\033[33m'
    RED=$'\033[31m'
    RESET=$'\033[0m'
else
    BOLD="" GREEN="" YELLOW="" RED="" RESET=""
fi

step_header() { printf '\n%s=== %s ===%s\n' "$BOLD" "$*" "$RESET"; }
step_ok()     { printf '%s[ok]%s %s\n'   "$GREEN"  "$RESET" "$*"; }
step_skip()   { printf '%s[skip]%s %s\n' "$YELLOW" "$RESET" "$*"; }
step_fail()   { printf '%s[fail]%s %s\n' "$RED"    "$RESET" "$*"; }

# All 50 US states + DC, in Geofabrik path format.
ALL_STATES=(
    alabama alaska arizona arkansas california colorado connecticut delaware
    district-of-columbia florida georgia hawaii idaho illinois indiana iowa
    kansas kentucky louisiana maine maryland massachusetts michigan minnesota
    mississippi missouri montana nebraska nevada new-hampshire new-jersey
    new-mexico new-york north-carolina north-dakota ohio oklahoma oregon
    pennsylvania puerto-rico rhode-island south-carolina south-dakota
    tennessee texas utah vermont virginia washington west-virginia wisconsin
    wyoming
)

# Defaults that match the example the user gave. Overridable by CLI flags.
START_YEAR=1950
END_YEAR=2026
STOP_ON_FAIL=0
STATES=()
EXTRA_ARGS=()

while [ $# -gt 0 ]; do
    case "$1" in
        --start-year)   START_YEAR="$2"; shift 2 ;;
        --start-year=*) START_YEAR="${1#--start-year=}"; shift ;;
        --end-year)     END_YEAR="$2"; shift 2 ;;
        --end-year=*)   END_YEAR="${1#--end-year=}"; shift ;;
        --states)       read -r -a STATES <<<"$2"; shift 2 ;;
        --states=*)     read -r -a STATES <<<"${1#--states=}"; shift ;;
        --stop-on-fail) STOP_ON_FAIL=1; shift ;;
        -h|--help)
            sed -n '2,25p' "${BASH_SOURCE[0]}" | sed 's/^# \{0,1\}//'
            exit 0
            ;;
        --)
            shift
            EXTRA_ARGS+=("$@")
            break
            ;;
        -*)
            # Forward any unknown flag (and its value, if the next arg
            # doesn't start with '-') to climate-report.sh.
            EXTRA_ARGS+=("$1")
            if [ $# -ge 2 ] && [[ "$2" != -* ]]; then
                EXTRA_ARGS+=("$2")
                shift 2
            else
                shift
            fi
            ;;
        *)
            # Positional → state name.
            STATES+=("$1")
            shift
            ;;
    esac
done

if [ ${#STATES[@]} -eq 0 ]; then
    STATES=("${ALL_STATES[@]}")
fi

TOTAL=${#STATES[@]}
printf '%sgenerating climate reports for %d state(s)%s\n' "$BOLD" "$TOTAL" "$RESET"
printf '  start_year=%s end_year=%s stop_on_fail=%s\n' \
    "$START_YEAR" "$END_YEAR" "$STOP_ON_FAIL"
if [ ${#EXTRA_ARGS[@]} -gt 0 ]; then
    printf '  extra args: %s\n' "${EXTRA_ARGS[*]}"
fi

FAILED=()
OK_COUNT=0
IDX=0
START_EPOCH=$(date +%s)
for state in "${STATES[@]}"; do
    IDX=$((IDX + 1))
    step_header "[$IDX/$TOTAL] $state"
    if "${SCRIPT_DIR}/climate-report.sh" \
        --start-year "$START_YEAR" \
        --end-year "$END_YEAR" \
        --region "north-america/us/$state" \
        --i-know-this-is-huge \
        "${EXTRA_ARGS[@]}"; then
        step_ok "$state"
        OK_COUNT=$((OK_COUNT + 1))
    else
        rc=$?
        step_fail "$state (exit $rc)"
        FAILED+=("$state")
        if [ "$STOP_ON_FAIL" = "1" ]; then
            printf '\n%s--stop-on-fail set — aborting%s\n' "$RED" "$RESET"
            break
        fi
    fi
done

ELAPSED=$(( $(date +%s) - START_EPOCH ))
echo
printf '%ssummary%s  %d ok, %d failed, %ds elapsed\n' \
    "$BOLD" "$RESET" "$OK_COUNT" "${#FAILED[@]}" "$ELAPSED"
if [ ${#FAILED[@]} -gt 0 ]; then
    printf '%sfailed:%s %s\n' "$RED" "$RESET" "${FAILED[*]}"
    exit 1
fi
exit 0
