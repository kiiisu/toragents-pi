#!/usr/bin/env python3
"""Write verified artifact hashes into both installers.

    tools/pin_hashes.py artifact-name=sha256 [more ...]

Only hashes that came from the artifact's own publisher belong here. A hash from a
mirror, a blog, or a file we downloaded without checking proves nothing: it only
records what we already had.
"""
from __future__ import annotations

import pathlib
import re
import sys

ROOT = pathlib.Path(__file__).resolve().parent.parent


def main(argv: list[str]) -> int:
    if not argv:
        print(__doc__.strip())
        return 2
    pins: dict[str, str] = {}
    for arg in argv:
        name, _, digest = arg.partition("=")
        digest = digest.strip().lower()
        if len(digest) != 64 or not re.fullmatch(r"[0-9a-f]{64}", digest):
            print(f"refusing: {name} has no valid sha256", file=sys.stderr)
            return 2
        pins[name.strip()] = digest

    sh = ROOT / "setup.sh"
    text = sh.read_text()
    table = "\n".join(f"{n} {d}" for n, d in sorted(pins.items()))
    # bash: no spaces around =, value opens with a newline and closes on its own line
    text = re.sub(r'SHA256_PINS="[^"]*"',
                  'SHA256_PINS="\n' + table + '\n"', text, count=1)
    sh.write_text(text)

    ps = ROOT / "setup.ps1"
    text = ps.read_text()
    entries = "\n".join(f'  ["{n}"] = "{d}"' for n, d in sorted(pins.items()))
    text = re.sub(r'\$Sha256Pins = @\{[^}]*\}',
                  "$Sha256Pins = @{\n" + entries + "\n}", text, count=1)
    ps.write_text(text)
    print(f"pinned {len(pins)} artifact(s) into setup.sh and setup.ps1")
    return 0


if __name__ == "__main__":
    sys.exit(main(sys.argv[1:]))
