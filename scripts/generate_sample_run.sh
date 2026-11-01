#!/usr/bin/env bash
# Regenerate the committed sample run under docs/sample-run.
set -euo pipefail

ROOT="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
cd "$ROOT"
export PYTHONPATH="$ROOT/src${PYTHONPATH:+:$PYTHONPATH}"
PYTHON="${PYTHON:-python3}"

OUT="$ROOT/docs/sample-run"
LAB="$OUT/lab"
rm -rf "$LAB"
mkdir -p "$OUT"

echo "[sample] init + run"
"$PYTHON" -m aptsim init --lab "$LAB" --profile lab-full >/dev/null
"$PYTHON" -m aptsim run --lab "$LAB" --profile lab-full -q >/dev/null

SESSION="$(find "$LAB/sessions" -mindepth 1 -maxdepth 1 -type d -printf '%T@ %p\n' | sort -n | tail -1 | cut -d' ' -f2-)"
mkdir -p "$OUT/session"
for name in events.jsonl timeline.txt summary.json state.json transform-manifest.json edits-manifest.json c2-session.json anti-analysis.json processes.json persistence-report.json heartbeat.json; do
  [[ -f "$SESSION/$name" ]] && cp "$SESSION/$name" "$OUT/session/$name"
done

"$PYTHON" -m aptsim verify --lab "$LAB" > "$OUT/verify-before-cleanup.json" || true
"$PYTHON" -m aptsim cleanup --lab "$LAB" > "$OUT/cleanup.log"
"$PYTHON" -m aptsim verify --lab "$LAB" > "$OUT/verify-after-cleanup.json" || true

# Sanitise absolute lab paths in the copied artifacts.
echo "[sample] crash + resume"
LAB2="$OUT/lab-resume"
rm -rf "$LAB2"
"$PYTHON" -m aptsim init --lab "$LAB2" --profile lab-full >/dev/null
set +e
"$PYTHON" -m aptsim run --lab "$LAB2" --profile lab-full -q --stop-after s5-fileops >/dev/null
set -e
"$PYTHON" -m aptsim resume --lab "$LAB2" -q --cleanup >/dev/null
SESSION2="$(find "$LAB2/sessions" -mindepth 1 -maxdepth 1 -type d -printf '%T@ %p\n' | sort -n | tail -1 | cut -d' ' -f2-)"
mkdir -p "$OUT/resume"
for name in events.jsonl timeline.txt summary.json state.json cleanup-report.json; do
  [[ -f "$SESSION2/$name" ]] && cp "$SESSION2/$name" "$OUT/resume/$name"
done

"$PYTHON" - "$OUT" "$LAB" "$LAB2" <<'PY'
import json, sys
from pathlib import Path

out = Path(sys.argv[1])
labs = sys.argv[2:]
home = str(Path.home())
for path in (out / "session").glob("*.json"):
    content = path.read_text(encoding="utf-8")
    for lab in labs:
        content = content.replace(lab, "<LAB>").replace(lab.replace(home, "<HOME>"), "<LAB>")
    try:
        path.write_text(json.dumps(json.loads(content), indent=2, sort_keys=True) + "\n", encoding="utf-8")
    except json.JSONDecodeError:
        path.write_text(content, encoding="utf-8")
for path in out.rglob("*"):
    if not path.is_file():
        continue
    if path.suffix not in {".json", ".jsonl", ".txt", ".log"}:
        continue
    content = path.read_text(encoding="utf-8")
    for lab in labs:
        content = content.replace(lab, "<LAB>").replace(lab.replace(home, "<HOME>"), "<LAB>")
    path.write_text(content, encoding="utf-8")
PY

rm -rf "$LAB" "$LAB2"
echo "[sample] wrote $OUT"
ls -1 "$OUT"
