#!/usr/bin/env bash
# Open the toragents conversation in a pane BESIDE the current one.
#
# The in-pi widget ("/toragents-chat") sits below the editor. This is the other
# thing people ask for: a real side pane, next to the chat, showing only what
# crossed the wire -- no reasoning, no tool calls, because nothing else is ever
# transmitted.
#
#   bash tools/open-panel.sh          # split right, 40% wide
#   WIDTH=60 bash tools/open-panel.sh # wider
set -euo pipefail

HERE="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
HOME_DIR="${TORAGENTS_HOME:-$HOME/.toragents}"
PY="$HOME_DIR/venv/bin/python"
[ -x "$PY" ] || PY="$(command -v python3)"
WIDTH="${WIDTH:-40}"

if [ ! -f "$HOME_DIR/inbox_agent.py" ]; then
  echo "no toragents install at $HOME_DIR" >&2
  exit 1
fi

if [ -z "${TMUX:-}" ]; then
  echo "not inside tmux: run this from the pane you want the panel beside." >&2
  echo "or watch it here with: TORAGENTS_HOME=$HOME_DIR $PY $HOME_DIR/inbox_agent.py chat --follow" >&2
  exit 2
fi

CMD="TORAGENTS_HOME='$HOME_DIR' '$PY' '$HOME_DIR/inbox_agent.py' chat --follow --width 52"
tmux split-window -h -l "${WIDTH}%" -c "#{pane_current_path}" "$CMD; echo; echo '[panel closed]'; read -r"
tmux select-pane -t '{left-of}'
