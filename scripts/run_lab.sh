#!/usr/bin/env bash
# Launch the full simulation pipeline against the disposable lab.
set -euo pipefail

ROOT="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
cd "$ROOT"
export PYTHONPATH="$ROOT/src${PYTHONPATH:+:$PYTHONPATH}"
PYTHON="${PYTHON:-python3}"

PROFILE="${APTSIM_PROFILE:-lab-full}"
LAB_ARGS=()
if [[ -n "${APTSIM_LAB:-}" ]]; then
  LAB_ARGS=(--lab "$APTSIM_LAB")
fi

if [[ ! -d "${APTSIM_LAB:-$ROOT/lab}/victim" ]]; then
  echo "[aptsim] lab not found, running setup first"
  "$PYTHON" -m aptsim init --profile "$PROFILE" "${LAB_ARGS[@]}"
fi

echo "[aptsim] running profile: $PROFILE"
set +e
"$PYTHON" -m aptsim run --profile "$PROFILE" "${LAB_ARGS[@]}" "$@"
STATUS=$?
set -e

case "$STATUS" in
  0) echo "[aptsim] run completed" ;;
  3) echo "[aptsim] run intentionally interrupted; resume with: python3 -m aptsim resume" ;;
  *) echo "[aptsim] run failed with status $STATUS" >&2 ;;
esac

echo "[aptsim] latest timeline:"
find "${APTSIM_LAB:-$ROOT/lab}/sessions" -name timeline.txt -printf '%T@ %p\n' 2>/dev/null \
  | sort -n | tail -1 | cut -d' ' -f2- || true
exit "$STATUS"
