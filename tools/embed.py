#!/usr/bin/env python3
"""Regenerate the embedded payloads in setup.sh and setup.ps1.

`curl | bash` and `irm | iex` have no script directory, and fetching the
companions from a ref was fragile (a force-moved tag served stale bytes from the
CDN, so a pinned one-liner ran a mismatched installer). Embedding makes each
installer self-contained: one URL, one file, no version skew.

Run after editing inbox_agent.py, wire.py, toragents_watch.ts,
            toragents_hook.py or tools/install_hooks.py:
    python3 tools/embed.py
"""
import base64
import pathlib
import re
import sys

SOURCES = {
    "PAYLOAD_INBOX": "inbox_agent.py",
    "PAYLOAD_WIRE": "wire.py",
    "PAYLOAD_WATCH": "toragents_watch.ts",
    "PAYLOAD_LEXICON": "docs/lexicon.seed.json",
}

VARS = ("PAYLOAD_INBOX", "PAYLOAD_WIRE", "PAYLOAD_WATCH", "PAYLOAD_LEXICON")


def target(installer, var):
    """(installer, var, regex, replacement-prefix).

    sh assignment:  PAYLOAD_INBOX='...'
    ps1 assignment: $PAYLOAD_INBOX='...'   (tolerant of a stray leading backslash)
    """
    if installer.endswith(".ps1"):
        return (installer, var, r"^\\?\$" + var + r"='[^']*'$", "$")
    return (installer, var, "^" + var + r"='[^']*'$", "")


TARGETS = [target(f, v) for f in ("setup.sh", "setup.ps1") for v in VARS]

HERE = pathlib.Path(__file__).resolve().parent.parent
fail = False

for target, var, pattern, prefix in TARGETS:
    path = HERE / target
    if not path.exists():
        print(f"skip {target} (absent)")
        continue
    src = (HERE / SOURCES[var]).read_bytes()
    b64 = base64.b64encode(src).decode()
    text = path.read_text()
    replacement = prefix + var + f"='{b64}'"
    new, n = re.subn(pattern, lambda m: replacement, text, flags=re.MULTILINE)
    if n != 1:
        print(f"ERROR {target}: expected 1 {var} line, found {n}")
        fail = True
        continue
    path.write_text(new)
    print(f"{target:10} {var:14} {SOURCES[var]:22} {len(src):6}B -> {len(b64)} b64")

sys.exit(1 if fail else 0)
