#!/usr/bin/env bash
# Run the reader test.
#
# Why a wrapper: toragents-panel.ts imports @earendil-works/pi-tui at runtime
# (truncateToWidth, wrapTextWithAnsi, Key, matchesKey), and that package only
# resolves from pi's own node_modules. Nothing is vendored and nothing is added to
# this repo's dependencies — the wrapper points NODE_PATH at the copy that is
# already installed here, which is the same one pi will load the extension with.
set -euo pipefail
ROOT="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"

# Where a global npm install of pi puts its dependencies. Ordered: an explicit
# NODE_PATH first (a pinning CI can pass one), then npm's own answer, then the
# paths pi lands in on this box's platforms.
candidates=()
[ -n "${NODE_PATH:-}" ] && candidates+=("$NODE_PATH")
if command -v npm >/dev/null 2>&1; then
	candidates+=("$(npm root -g 2>/dev/null || true)")
fi
candidates+=(
	"/usr/lib/node_modules"
	"/usr/local/lib/node_modules"
	"$HOME/.pi/agent/npm/node_modules"
	"$HOME/.npm-global/lib/node_modules"
	"/opt/homebrew/lib/node_modules"
)
if command -v pi >/dev/null 2>&1; then
	candidates+=("$(dirname "$(readlink -f "$(command -v pi)")")/../lib/node_modules")
fi

NM=""
for root in "${candidates[@]}"; do
	[ -n "$root" ] || continue
	for sub in "@earendil-works/pi-coding-agent/node_modules" "node_modules/@earendil-works/pi-coding-agent/node_modules"; do
		if [ -d "$root/$sub/@earendil-works/pi-tui" ]; then
			NM="$root/$sub"
			break 2
		fi
	done
done

if [ -z "$NM" ]; then
	echo "SKIP test_panel: @earendil-works/pi-tui not found — install pi, or pass NODE_PATH" >&2
	exit 0
fi
if ! command -v bun >/dev/null 2>&1; then
	echo "SKIP test_panel: bun not installed (the rest of this repo's ts tests need it too)" >&2
	exit 0
fi

cd "$ROOT"
NODE_PATH="$NM" exec bun tools/test_panel.ts
