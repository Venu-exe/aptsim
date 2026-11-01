#!/usr/bin/env bash
# In-process loopback C2 demo (real TCP sockets, inert task handlers).
set -euo pipefail

ROOT="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
cd "$ROOT"
export PYTHONPATH="$ROOT/src${PYTHONPATH:+:$PYTHONPATH}"
PYTHON="${PYTHON:-python3}"

SCENARIO="${1:-$ROOT/config/scenarios/c2-demo.json}"
LAB_ARGS=()
if [[ -n "${APTSIM_LAB:-}" ]]; then
  LAB_ARGS=(--lab "$APTSIM_LAB")
fi

"$PYTHON" -m aptsim c2-demo --profile lab-full --scenario "$SCENARIO" "${LAB_ARGS[@]}"
