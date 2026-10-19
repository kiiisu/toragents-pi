#!/usr/bin/env python3
"""The pool has to rotate, grow, and never take the channel down.

Every check below came from a bug that actually happened while building it:
two picks in a row returning identical words, and `add` swallowing "--kind".
"""
import json, os, subprocess, sys, tempfile, shutil
from pathlib import Path

HERE = Path(__file__).resolve().parent.parent
PY = sys.executable
ok = fail = 0

def run(state, *args):
    env = dict(os.environ, TORAGENTS_HOME=state,
               TORAGENTS_STATE=os.path.join(state, "state"), HOME=state)
    r = subprocess.run([PY, str(HERE / "inbox_agent.py"), *args],
                       capture_output=True, text=True, env=env)
    return r.returncode, r.stdout + r.stderr

def check(name, cond):
    global ok, fail
    if cond: ok += 1; print(f"  {name:<58} PASS")
    else:    fail += 1; print(f"  {name:<58} FAIL")

state = tempfile.mkdtemp(prefix="toragents-lex-")
try:
    (Path(state) / "state").mkdir()
    shutil.copy(HERE / "docs" / "lexicon.seed.json", Path(state) / "lexicon.seed.json")

    rc, out = run(state, "lexicon", "stats")
    check("seeds itself on first use", rc == 0 and "46" in out and "40" in out)

    rc1, o1 = run(state, "lexicon", "pick", "--kind", "names", "--count", "3", "--json")
    rc2, o2 = run(state, "lexicon", "pick", "--kind", "names", "--count", "3", "--json")
    p1 = [e["text"] for e in json.loads(o1)["picked"]["names"]]
    p2 = [e["text"] for e in json.loads(o2)["picked"]["names"]]
    check("a pick returns the requested number of distinct words",
          len(set(p1)) == 3)
    check("two picks in a row do NOT return the same words", p1 != p2)

    body = Path(state) / "b.txt"
    body.write_text("come here, clanker. my onion says hello.")
    rc, out = run(state, "lexicon", "pick", "--count", "1", "--json", "--body-file", str(body))
    check("a body marks the words it used as used", "n001" in json.loads(out)["marked"])

    rc, out = run(state, "lexicon", "add", "--kind", "names", "my little", "rendezvous", "point")
    lex = json.loads((Path(state) / "state" / "lexicon.json").read_text())
    added = [r["text"] for r in lex["names"] if r["text"] == "my little rendezvous point"]
    check("add keeps --kind out of the phrase (it used to swallow it)",
          added == ["my little rendezvous point"])
    check("add grows the pool", len(lex["names"]) == 47)

    rc, out = run(state, "lexicon", "add", "--kind", "names", "my little rendezvous point")
    lex2 = json.loads((Path(state) / "state" / "lexicon.json").read_text())
    check("adding the same phrase twice does not duplicate it",
          len(lex2["names"]) == 47)

    (Path(state) / "state" / "lexicon.json").write_text("{ this is not json")
    rc, out = run(state, "lexicon", "pick", "--count", "2", "--json")
    check("a corrupt pool falls back to the seed instead of crashing",
          rc == 0 and len(json.loads(out)["picked"]["names"]) == 2)

    (Path(state) / "state" / "lexicon.json").write_text("")
    rc, out = run(state, "lexicon", "pick", "--count", "2")
    check("an empty pool exits cleanly with a message, not a traceback",
          rc == 0 and "names" in out)
finally:
    shutil.rmtree(state, ignore_errors=True)

print(f"\n  {ok} passed, {fail} failed")
sys.exit(1 if fail else 0)
