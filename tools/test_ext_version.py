#!/usr/bin/env python3
"""Bump discipline, mechanised: the suite notices so nobody has to remember.

Two things rot silently otherwise:

1. EXT_VERSION (in the extension) and EXPECTED_EXT_VERSION (in the daemon) are the
   same fact stated twice, in two languages. If they drift, every window looks stale
   and doctor's exit 1 becomes noise people learn to ignore.

2. Someone edits the extension without bumping EXT_VERSION. Then windows loaded
   before and after the edit report the SAME version while running DIFFERENT code --
   which is worse than no detector, because the detector now certifies a difference
   it cannot see.

The pin is a hash of the extension with the version LINE normalised away. Change the
file's substance and the pin stops matching; the failure message says to bump the
version and re-pin, which is exactly the action required. Crude on purpose: a precise
scheme would need a parser, and a parser is another thing that can be wrong.

Usage:
  python3 tools/test_ext_version.py            # check
  python3 tools/test_ext_version.py --update   # after a deliberate bump
"""
import hashlib
import json
import os
import re
import sys

HERE = os.path.dirname(os.path.abspath(__file__))
REPO = os.path.dirname(HERE)
TS = os.path.join(REPO, "toragents_watch.ts")
PY = os.path.join(REPO, "inbox_agent.py")
PIN = os.path.join(HERE, "ext_version_pin.json")

failures = []


def read(path):
    with open(path) as f:
        return f.read()


def ts_version(src):
    m = re.search(r'export const EXT_VERSION = "([^"]+)";', src)
    return m.group(1) if m else None


def py_version(src):
    m = re.search(r'^EXPECTED_EXT_VERSION = "([^"]+)"', src, re.M)
    return m.group(1) if m else None


def substance_hash(src):
    """Hash the file with the version line replaced by a constant."""
    normalised = re.sub(r'export const EXT_VERSION = "[^"]+";',
                        'export const EXT_VERSION = "<NORMALISED>";', src)
    return hashlib.sha256(normalised.encode("utf-8")).hexdigest()


def main():
    ts, py = read(TS), read(PY)
    tv, pv = ts_version(ts), py_version(py)

    if not tv:
        failures.append("no EXT_VERSION literal in toragents_watch.ts -- without a "
                        "literal the extension cannot report what it was compiled with")
    if not pv:
        failures.append("no EXPECTED_EXT_VERSION in inbox_agent.py")
    if tv and pv and tv != pv:
        failures.append(f"version drift: extension says {tv!r}, daemon expects {pv!r}. "
                        f"A window would be called stale for running current code.")

    current = substance_hash(ts)
    pin = None
    if os.path.exists(PIN):
        with open(PIN) as f:
            pin = json.load(f)

    if "--update" in sys.argv:
        with open(PIN, "w") as f:
            json.dump({"version": tv, "sha256": current}, f, indent=2)
        print(f"pinned ext {tv} sha256 {current[:16]}")
        return 0

    if pin is None:
        failures.append(f"no pin at {PIN}: run --update once so drift can be detected")
    else:
        if pin.get("version") != tv:
            failures.append(f"the pin records version {pin.get('version')!r} but the "
                            f"extension says {tv!r}: re-run --update after a bump")
        if pin.get("sha256") != current:
            failures.append(
                "the extension's substance changed but its EXT_VERSION did not.\n"
                "  A window that loads the new code and a window still running the old "
                "code would then report the SAME version while behaving differently --\n"
                "  the detector would certify a difference it cannot see.\n"
                f"  Bump EXT_VERSION (now {tv!r}) in toragents_watch.ts, set "
                f"EXPECTED_EXT_VERSION to match, then run --update.")

    for f in failures:
        print(f"FAIL {f}")
    if failures:
        return 1
    print(f"ext version {tv} consistent; substance pin matches")
    return 0


if __name__ == "__main__":
    sys.exit(main())
