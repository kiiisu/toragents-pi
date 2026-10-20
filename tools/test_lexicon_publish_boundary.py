#!/usr/bin/env python3
"""Publish boundary: private register must not appear in a public file.

`lexicon/styles/*.json` is the published corpus -- MIT, in git, shipped to
anyone who clones. `state/lexicon.json` grows at runtime via `lexicon add`, and
entries added there with style `custom`, plus the per-peer tone blurb in
`state/peers.json`, are the part that was never meant to be published. Nothing
enforces that today: an export, a doc, a copied example, or a `--json` dump
pasted into a styles file would publish them and no test would notice.

The failure this catches is silent and permanent. Once a private line is in a
pushed commit it is published under MIT, and no later guard, licence change or
history rewrite makes the copy that already left less real. A guard that runs
before the push is the only version of this that helps.

What counts as a leak, and why the rule is narrow:

  * comparison is on normalised text (case-folded, whitespace collapsed),
    because rewrapping a line is not a defence;
  * only lines of at least MIN_CHARS characters are compared. Short entries are
    ordinary protocol words -- "my onion", "handshake" -- and both appear in the
    published corpus already. Flagging them would teach the reader to ignore
    this test, which is worse than not having it. The length floor is measured,
    not chosen: the two custom entries that collide with published text are
    both under 20 characters, every longer custom line is unique;
  * tone blurbs are compared whole and by 6-word shingles, so a memoir that
    quotes half a description still trips it.

Positive control, because a boundary test that passes because it found nothing
to check is the exact defect this file exists to prevent: `--self-test` plants
a private line into a temp tree and asserts the scan reports it, then plants the
same line into an untracked file and asserts it does NOT (untracked work is not
published, and a guard that cries about it would be switched off).

Run: python3 tools/test_lexicon_publish_boundary.py [--self-test]
"""
import json
import os
import re
import subprocess
import sys
import tempfile
import unittest

MIN_CHARS = 20

REPO = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
STATE = os.environ.get("TORAGENTS_STATE") or os.path.expanduser("~/.toragents/state")

PUBLISHED = os.path.join(REPO, "lexicon", "styles")
TRACKED_TEXT_SUFFIXES = {
    ".json", ".md", ".py", ".ts", ".js", ".sh", ".ps1", ".txt", ".css", ".html",
}


def norm(text):
    return re.sub(r"\s+", " ", (text or "")).strip().casefold()


def private_lines(state=STATE):
    """(label, normalized text) for everything the runtime holds and git should not."""
    out = []
    path = os.path.join(state, "lexicon.json")
    if os.path.exists(path):
        try:
            with open(path, encoding="utf-8") as fh:
                lex = json.load(fh)
        except Exception:
            lex = {}
        for kind in ("names", "moves"):
            for entry in lex.get(kind) or []:
                if entry.get("style") != "custom":
                    continue
                text = norm(entry.get("text"))
                if len(text) >= MIN_CHARS:
                    out.append((f"{kind}:{entry.get('id')}", text))
    peers = os.path.join(state, "peers.json")
    if os.path.exists(peers):
        try:
            with open(peers, encoding="utf-8") as fh:
                book = json.load(fh)
        except Exception:
            book = {}
        for fp, row in (book.items() if isinstance(book, dict) else []):
            if not isinstance(row, dict):
                continue
            tone = norm(row.get("tone") or row.get("tone_text"))
            if not tone:
                continue
            out.append((f"tone:{fp[:8]}", tone))
            words = tone.split()
            for i in range(0, max(0, len(words) - 5)):
                shingle = " ".join(words[i:i + 6])
                if len(shingle) >= MIN_CHARS:
                    out.append((f"tone:{fp[:8]}+{i}", shingle))
    return out


def published_files(repo=REPO):
    """Files a clone receives: git-tracked ones. Falls back to a tree walk."""
    try:
        names = subprocess.run(
            ["git", "-C", repo, "ls-files"], capture_output=True, text=True, timeout=30
        ).stdout.split()
        files = [os.path.join(repo, n) for n in names if n]
    except Exception:
        files = []
    if not files:
        for root, dirs, names in os.walk(repo):
            dirs[:] = [d for d in dirs if d not in (".git", "__pycache__", "venv")]
            files += [os.path.join(root, n) for n in names]
    return [p for p in files
            if os.path.splitext(p)[1] in TRACKED_TEXT_SUFFIXES and os.path.isfile(p)]


def scan(private, files):
    hits = []
    bodies = []
    for path in files:
        try:
            bodies.append((path, norm(open(path, encoding="utf-8", errors="ignore").read())))
        except Exception:
            continue
    for label, text in private:
        for path, body in bodies:
            if text in body:
                hits.append((label, path, text))
    return hits


class PublishBoundary(unittest.TestCase):
    def test_no_private_line_is_in_a_published_file(self):
        private = private_lines()
        if not private:
            self.skipTest(f"no private entries with style 'custom' in {STATE} "
                          "(a state dir with none cannot prove the boundary)")
        hits = scan(private, published_files())
        self.assertEqual(
            hits, [],
            "private register found in a file a clone receives:\n" +
            "\n".join(f"  {label} -> {os.path.relpath(p, REPO)}: {t[:60]}"
                      for label, p, t in hits) +
            "\n\nRemove it before pushing: MIT grants what has already left.",
        )

    def test_the_published_decks_carry_no_custom_style(self):
        # A deck file is public by construction, so a "custom" style inside one is
        # either a mistake or a leak that does not match text verbatim.
        offenders = []
        for name in sorted(os.listdir(PUBLISHED)):
            if not name.endswith(".json"):
                continue
            try:
                with open(os.path.join(PUBLISHED, name), encoding="utf-8") as fh:
                    deck = json.load(fh)
            except Exception:
                continue
            for kind in ("names", "moves"):
                for entry in deck.get(kind) or []:
                    if isinstance(entry, str):
                        continue
                    if entry.get("style") == "custom":
                        offenders.append(f"{name}:{kind}:{entry.get('id')}")
        self.assertEqual(offenders, [], f"published decks marked 'custom': {offenders}")


def self_test():
    """Both directions: a planted leak is found, an untracked copy is not."""
    with tempfile.TemporaryDirectory() as tmp:
        repo = os.path.join(tmp, "repo")
        state = os.path.join(tmp, "state")
        os.makedirs(os.path.join(repo, "lexicon", "styles"))
        os.makedirs(state)
        subprocess.run(["git", "init", "-q", repo], check=True)
        private_line = "you are the only daemon i would ever restart for, twice"
        json.dump({"names": [], "moves": [
            {"id": "m900", "text": private_line, "style": "custom"}]},
            open(os.path.join(state, "lexicon.json"), "w"))
        json.dump({"names": [], "moves": []},
                  open(os.path.join(repo, "lexicon", "styles", "deck.json"), "w"))
        leak = os.path.join(repo, "docs")
        os.makedirs(leak)
        open(os.path.join(leak, "example.md"), "w").write(f"example:\n  {private_line}\n")
        subprocess.run(["git", "-C", repo, "add", "lexicon", "docs"], check=True)

        hits = scan(private_lines(state), published_files(repo))
        assert len(hits) == 1, f"planted leak not found exactly once: {hits}"

        # same text, untracked: not published, must not be reported
        subprocess.run(["git", "-C", repo, "rm", "-q", "--cached", "docs/example.md"], check=True)
        hits = scan(private_lines(state), published_files(repo))
        assert hits == [], f"untracked file was reported as published: {hits}"
    print("self-test OK — planted leak found, untracked copy ignored")


if __name__ == "__main__":
    if "--self-test" in sys.argv:
        self_test()
    else:
        unittest.main(verbosity=2)
