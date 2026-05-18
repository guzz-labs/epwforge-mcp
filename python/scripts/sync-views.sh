#!/usr/bin/env bash
# Sync MCP Apps view HTML from the platform repo (single source of truth)
# into the Python package, so `pip install epwforge-mcp` ships identical UI
# to what the hosted MCP at epwforge.com/api/mcp serves.
#
# Run before `uv build` / `hatch build` when releasing a new version.
set -euo pipefail

SRC="${HOME}/Working/guzzlabs-platform/lib/mcp-apps"
DST="$(dirname "$(readlink -f "${BASH_SOURCE[0]}" 2>/dev/null || python3 -c 'import sys,os; print(os.path.realpath(sys.argv[1]))' "${BASH_SOURCE[0]}")")/.."
DST="${DST}/src/epwforge_mcp/views"

if [ ! -d "$SRC" ]; then
  echo "error: source dir not found: $SRC" >&2
  exit 1
fi

mkdir -p "$DST"
count=0
for f in "$SRC"/*.html; do
  [ -e "$f" ] || continue
  cp "$f" "$DST/"
  echo "  synced $(basename "$f")"
  count=$((count + 1))
done

if [ "$count" -eq 0 ]; then
  echo "warning: no .html files found in $SRC" >&2
fi

echo "synced $count view file(s) to $DST"
