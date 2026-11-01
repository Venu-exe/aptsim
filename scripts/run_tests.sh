#!/usr/bin/env bash
# Run unit + integration tests (unittest is enough; pytest is used if present).
set -euo pipefail

ROOT="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
cd "$ROOT"
export PYTHONPATH="$ROOT/src${PYTHONPATH:+:$PYTHONPATH}"
PYTHON="${PYTHON:-python3}"

echo "[aptsim] unittest discover"
"$PYTHON" -m unittest discover -s tests -t . -v

if "$PYTHON" -c 'import pytest' >/dev/null 2>&1; then
  echo
  echo "[aptsim] pytest"
  "$PYTHON" -m pytest -q
fi
