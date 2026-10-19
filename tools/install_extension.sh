#!/usr/bin/env bash
# Copy the extension into the pi extensions dir WITHOUT clobbering local work.
#
# Written after doing exactly that: a repo->install copy at 13:39 silently
# overwrote ~570 lines another live session had added to the installed file.
# A copy is a merge decision only when you have looked; otherwise it is a delete
# with extra steps.
set -euo pipefail
REPO="$(cd "$(dirname "$0")/.." && pwd)"
DEST="${PI_EXT_DIR:-$HOME/.pi/agent/extensions}"
SRC="$REPO/toragents_watch.ts"
DST="$DEST/toragents-inbox.ts"
FORCE=0
[ "${1:-}" = "--force" ] && FORCE=1

mkdir -p "$DEST"
if [ -f "$DST" ] && ! cmp -s "$SRC" "$DST"; then
  echo "installed file differs from the repo:"
  diff -u "$DST" "$SRC" | head -40 || true
  echo "---"
  echo "diffstat: $(diff "$DST" "$SRC" | grep -cE '^[<>]') changed lines"
  if [ "$FORCE" -ne 1 ]; then
    echo "REFUSING to overwrite. If those local lines are yours to keep, merge them into the repo first;"
    echo "if the installed file is stale, re-run with --force."
    exit 3
  fi
  cp "$DST" "$DST.bak-$(date +%Y%m%d-%H%M%S)"
fi
install -m 644 "$SRC" "$DST"
echo "installed $DST"
echo "note: a running pi session keeps the old module -- /reload to pick this up"
