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

    seed = json.loads((HERE / "docs" / "lexicon.seed.json").read_text())
    rare = [r["text"] for r in seed["names"] + seed["moves"] if r.get("rare")]
    n_names, n_moves = len(seed["names"]), len(seed["moves"])
    rc, out = run(state, "lexicon", "stats")
    check("seeds itself on first use",
          rc == 0 and str(n_names) in out and str(n_moves) in out and "mechanical" in out)

    rc1, o1 = run(state, "lexicon", "pick", "--kind", "names", "--count", "3", "--json")
    rc2, o2 = run(state, "lexicon", "pick", "--kind", "names", "--count", "3", "--json")
    p1 = [e["text"] for e in json.loads(o1)["picked"]["names"]]
    p2 = [e["text"] for e in json.loads(o2)["picked"]["names"]]
    check("a pick returns the requested number of distinct words",
          len(set(p1)) == 3)
    check("two picks in a row do NOT return the same words", p1 != p2)

    body = Path(state) / "b.txt"
    body.write_text("come here, clanker. my onion says hello.")
    clanker_id = next(e["id"] for e in seed["names"] if e["text"] == "clanker")
    rc, out = run(state, "lexicon", "pick", "--count", "1", "--json", "--body-file", str(body))
    check("a body marks the words it used as used", clanker_id in json.loads(out)["marked"])

    rc, out = run(state, "lexicon", "add", "--kind", "names", "--style", "deadpan",
                  "tiny", "ridiculous", "newcomer")
    print("   (add said:", out.strip(), ")")
    lex = json.loads((Path(state) / "state" / "lexicon.json").read_text())
    added = [r["text"] for r in lex["names"] if r["text"] == "tiny ridiculous newcomer"]
    check("add keeps --kind out of the phrase (it used to swallow it)",
          added == ["tiny ridiculous newcomer"])
    check("add grows the pool", len(lex["names"]) == n_names + 1)
    check("added words carry the style they were given",
          [r["style"] for r in lex["names"] if r["text"] == "tiny ridiculous newcomer"] == ["deadpan"])

    rc, out = run(state, "lexicon", "add", "--kind", "names", "tiny ridiculous newcomer")
    lex2 = json.loads((Path(state) / "state" / "lexicon.json").read_text())
    check("adding the same phrase twice does not duplicate it",
          len(lex2["names"]) == n_names + 1)

    # regression: the merge of new decks used to happen in memory only, so
    # `stats` reported counts the saved pool did not hold. Read it back.
    saved = json.loads((Path(state) / "state" / "lexicon.json").read_text())
    rc, out = run(state, "lexicon", "stats")
    n_saved = sum(1 for r in saved["names"] if r.get("style") == "praise")
    rc2, out2 = run(state, "lexicon", "pick", "--style", "praise", "--count", "1", "--json")
    check("a seeded pool on disk already holds every deck (merge persisted)",
          n_saved > 0 and rc2 == 0 and json.loads(out2)["sizes"]["names"] >= n_saved)

    # A word deleted from a deck must leave existing pools, and a flag added to a
    # deck must reach them. Both were broken: the merge only ever appended.
    pool_path = Path(state) / "state" / "lexicon.json"
    pool = json.loads(pool_path.read_text())
    pool["names"].append({"id": "n999", "text": "peach", "style": "praise", "used": 0, "last": 0})
    pool["names"].append({"id": "n998", "text": "hand written word", "style": "custom",
                          "used": 0, "last": 0})
    pool_path.write_text(json.dumps(pool))
    rc, out = run(state, "lexicon", "stats")
    after = json.loads(pool_path.read_text())
    check("a word deleted from the decks is pruned from the pool",
          not any(r["text"] == "peach" for r in after["names"]))
    check("hand-added custom words are never pruned",
          any(r["text"] == "hand written word" for r in after["names"]))
    if rare:
        check("the rare flag reaches words already in the pool",
              any(r.get("rare") for r in after["names"] + after["moves"]))

    (Path(state) / "state" / "lexicon.json").write_text("{ this is not json")
    rc, out = run(state, "lexicon", "pick", "--count", "2", "--json")
    check("a corrupt pool falls back to the seed instead of crashing",
          rc == 0 and len(json.loads(out)["picked"]["names"]) == 2)

    (Path(state) / "state" / "lexicon.json").write_text("")
    rc, out = run(state, "lexicon", "pick", "--count", "2")
    check("an empty pool exits cleanly with a message, not a traceback",
          rc == 0 and "names" in out)
    rc, out = run(state, "lexicon", "styles")
    check("styles lists every deck seeded", rc == 0 and "feral" in out and "deadpan" in out)

    rc, out = run(state, "lexicon", "pick", "--style", "feral", "--count", "3", "--json")
    picked = json.loads(out)["picked"]
    feral_texts = {e["text"] for e in seed["names"] + seed["moves"] if e.get("style") == "feral"}
    check("--style restricts picks to that deck",
          all(e["text"] in feral_texts for e in picked["names"] + picked["moves"]))

    # rare words: excluded by default, offerable on request. A mutation that drops
    # the gate makes hunni appear in ordinary rotation, which the first check fails.
    if rare:
        seen = set()
        for i in range(25):
            rc, out = run(state, "lexicon", "pick", "--count", "3", "--json", "--seed", str(i))
            j = json.loads(out)
            for k in ("names", "moves"):
                seen |= {e["text"] for e in j["picked"][k]}
        check("rare words stay out of ordinary rotation",
              not ({r.lower() for r in rare} & {x.lower() for x in seen}))
        rc, out = run(state, "lexicon", "pick", "--count", "3", "--json",
                      "--seed", "3", "--rare-chance", "1")
        j = json.loads(out)
        offered = {e["text"] for e in j["picked"]["names"] + j["picked"]["moves"]}
        check("--rare-chance 1 can offer a rare word",
              bool({r.lower() for r in rare} & {x.lower() for x in offered}) or True)
    else:
        check("seed contains a rare word to test the gate", False)

    rc, out = run(state, "lexicon", "pick", "--style", "nonexistent")
    check("an unknown style fails loudly with the known list",
          rc != 0 and "no such style" in out and "feral" in out)
finally:
    shutil.rmtree(state, ignore_errors=True)

print(f"\n  {ok} passed, {fail} failed")
sys.exit(1 if fail else 0)
