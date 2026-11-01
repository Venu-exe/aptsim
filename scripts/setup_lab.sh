#!/usr/bin/env bash
# Create (or refresh) the disposable aptsim lab on Arch Linux.
set -euo pipefail

ROOT="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
cd "$ROOT"

PYTHON="${PYTHON:-python3}"
if ! command -v "$PYTHON" >/dev/null 2>&1; then
  echo "error: $PYTHON not found. On Arch: sudo pacman -S python" >&2
  exit 1
fi
if ! "$PYTHON" -c 'import sys; raise SystemExit(0 if sys.version_info >= (3, 11) else 1)'; then
  echo "error: Python >= 3.11 required, found $("$PYTHON" -V 2>&1)" >&2
  exit 1
fi

PROFILE="${APTSIM_PROFILE:-lab-full}"
export PYTHONPATH="$ROOT/src${PYTHONPATH:+:$PYTHONPATH}"

echo "[aptsim] python : $("$PYTHON" -V 2>&1)"
echo "[aptsim] profile: $PROFILE"
"$PYTHON" -m aptsim init --profile "$PROFILE"
"$PYTHON" -m aptsim doctor --profile "$PROFILE" || true

if [[ "${1:-}" == "--install" ]]; then
  echo "[aptsim] installing editable package (optional)"
  "$PYTHON" -m pip install --user -e . || {
    echo "warning: pip install failed; scripts use PYTHONPATH and still work" >&2
  }
fi

cat <<EOF

Lab ready: $ROOT/lab

Next steps
  scripts/run_lab.sh                 # full simulated intrusion
  scripts/run_lab.sh --stop-after s5-fileops
  scripts/c2_demo.sh                 # loopback C2 only
  scripts/run_tests.sh               # unit + integration tests
  scripts/cleanup_lab.sh             # restore files, remove artifacts
  scripts/cleanup_lab.sh --purge --yes
EOF
