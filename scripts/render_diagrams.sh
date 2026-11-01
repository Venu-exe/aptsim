#!/usr/bin/env bash
# Render Mermaid diagrams to SVG when mermaid-cli is available.
set -euo pipefail

ROOT="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
DIAGRAMS="$ROOT/docs/diagrams"

if ! command -v mmdc >/dev/null 2>&1; then
  cat <<'EOF'
mmdc (mermaid-cli) is not installed.
The .mmd sources are valid Mermaid and render on GitHub or in the Mermaid Live Editor.
Install with: npm install -g @mermaid-js/mermaid-cli   (or: yay -S mermaid-cli)
EOF
  exit 0
fi

for source in "$DIAGRAMS"/*.mmd; do
  target="${source%.mmd}.svg"
  echo "[aptsim] rendering $(basename "$source") -> $(basename "$target")"
  mmdc -i "$source" -o "$target" --backgroundColor transparent
done
