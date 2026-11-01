#!/usr/bin/env bash
# Restore reversible file operations and remove simulated persistence.
set -euo pipefail

ROOT="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
cd "$ROOT"
export PYTHONPATH="$ROOT/src${PYTHONPATH:+:$PYTHONPATH}"
PYTHON="${PYTHON:-python3}"
LAB="${APTSIM_LAB:-$ROOT/lab}"

PURGE=0
YES=0
for arg in "$@"; do
  case "$arg" in
    --purge) PURGE=1 ;;
    --yes|-y) YES=1 ;;
    *) echo "unknown option: $arg" >&2; exit 2 ;;
  esac
done

if [[ ! -d "$LAB" ]]; then
  echo "[aptsim] lab not present: $LAB"
  exit 0
fi

"$PYTHON" -m aptsim cleanup --lab "$LAB"
"$PYTHON" -m aptsim verify --lab "$LAB" --lenient

if [[ "$PURGE" == "1" ]]; then
  case "$LAB" in
    "$ROOT"/lab|/tmp/opencode/*|/tmp/aptsim-*) ;;
    *) echo "refusing to purge unexpected path: $LAB" >&2; exit 2 ;;
  esac
  if [[ "$YES" != "1" ]]; then
    read -r -p "Delete $LAB entirely? [y/N] " reply
    [[ "$reply" =~ ^[Yy]$ ]] || { echo "aborted"; exit 0; }
  fi
  rm -rf -- "$LAB"
  echo "[aptsim] purged $LAB"
fi
