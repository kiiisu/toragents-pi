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
# The reader is a second extension with its own files. Names must match between
# repo and install: toragents-panel.ts imports ./_lib/toragents-*.ts by that path.
READER_SRC="$REPO/toragents-panel.ts"
READER_DST="$DEST/toragents-panel.ts"
LIB_SRC="$REPO/_lib"
LIB_DST="$DEST/_lib"
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

# ── the reader ──────────────────────────────────────────────────────────────
# Same refusal rule as above, and for the same reason: the installed copy is the
# only copy a live session is running.
reader_differs=0
if [ -f "$READER_DST" ] && ! cmp -s "$READER_SRC" "$READER_DST"; then
  reader_differs=1
fi
for f in toragents-transcript toragents-side-column toragents-policy; do
  if [ -f "$LIB_DST/$f.ts" ] && ! cmp -s "$LIB_SRC/$f.ts" "$LIB_DST/$f.ts"; then
    reader_differs=1
  fi
done
if [ "$reader_differs" -eq 1 ]; then
  echo "installed reader differs from the repo:"
  diff -u "$READER_DST" "$READER_SRC" | head -20 || true
  for f in toragents-transcript toragents-side-column toragents-policy; do
    diff -u "$LIB_DST/$f.ts" "$LIB_SRC/$f.ts" 2>/dev/null | head -10 || true
  done
  if [ "$FORCE" -ne 1 ]; then
    echo "REFUSING to overwrite the reader. Merge your changes into the repo, or re-run with --force."
    exit 3
  fi
  [ -f "$READER_DST" ] && cp "$READER_DST" "$READER_DST.bak-$(date +%Y%m%d-%H%M%S)"
fi
mkdir -p "$LIB_DST"
install -m 644 "$READER_SRC" "$READER_DST"
for f in toragents-transcript toragents-side-column toragents-policy; do
  install -m 644 "$LIB_SRC/$f.ts" "$LIB_DST/$f.ts"
done
echo "installed $READER_DST (+ _lib/toragents-*.ts)"
echo "note: a running pi session keeps the old module -- /reload to pick this up"
