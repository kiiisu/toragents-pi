#!/usr/bin/env python3
"""A fixture must not model a state the system cannot produce.

The lesson came from a fix that required editing several fixtures at once: they all
built a presence marker that counted as TRUSTED while carrying no build identifier, and
they were green because the claim "trusted" was never checked against reality. One edit
is a fix; a sweep is a correction of the model -- and the sweep is the early signal.

So this is the mechanical half: any test that writes a writer marker must give it a
`code`, because a marker without one is now, by definition, "unversioned" and no
running writer can produce a trusted one. A fixture that omits it is not testing the
trusted path, whatever its assertions say.

Deliberately crude: it looks for the marker filename and requires the word `code`
nearby in the same file. A precise parser would be another thing that can be wrong, and
its failure mode is a false pass rather than a false failure. The cost of being wrong
here is one confused reader, not a shipped bug.
"""
import os
import re
import sys

HERE = os.path.dirname(os.path.abspath(__file__))
SKIP = {os.path.basename(__file__)}

# The fixture may name the file literally or through the module constant it lives
# under; both are writes of the same marker, and missing the constant would let the
# exact fixture that started this lesson pass.
MARKERS = ("_writer.json", "WRITER_FILE")
MARKER = MARKERS[0]
WINDOW_MARKER = re.compile(r"live.*?\.json")     # window records also carry `code`


def fixture_files():
    for name in sorted(os.listdir(HERE)):
        if not (name.endswith(".py") or name.endswith(".ts")):
            continue
        if name in SKIP or not name.startswith("test_"):
            continue
        yield name, os.path.join(HERE, name)


def main():
    findings = []
    scanned = 0
    for name, path in fixture_files():
        with open(path) as f:
            text = f.read()
        if not any(m in text for m in MARKERS):
            continue
        scanned += 1
        # Every line that mentions the marker is part of a write of it; the surrounding
        # statement must carry a `code`. Look at the marker line plus the next few, and
        # the previous line, which is where a dict literal's opening brace sits.
        lines = text.splitlines()
        in_doc = False
        for i, line in enumerate(lines):
            # Skip prose: the marker is discussed in docstrings, and a lint that fires
            # on a COMMENT ABOUT the marker would teach people to stop writing comments.
            if line.count('"""') % 2 == 1:
                in_doc = not in_doc
                continue
            if in_doc or line.lstrip().startswith(("#", "*", "//")):
                continue
            if not any(m in line for m in MARKERS):
                continue
            if not any(tok in line for tok in ("join(", "open(", "writeFileSync", "Path(", "write_text")):
                continue
            window = "\n".join(lines[max(0, i - 3):i + 4])
            if "code" not in window:
                findings.append(
                    f"{name}:{i + 1}: builds a writer marker with no `code` -- that is "
                    f"an UNVERSIONED marker, not a trusted one, so this fixture is "
                    f"modelling a state no current writer can produce")
    for f in findings:
        print(f"FAIL {f}")
    if findings:
        return 1
    print(f"fixtures modelling the current writer: {scanned} file(s) checked, 0 findings")
    return 0


if __name__ == "__main__":
    sys.exit(main())
