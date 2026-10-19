#!/usr/bin/env python3
"""Staleness acceptance tests. Written before the implementation, like tier 2.

Spec: docs/TIER2-DIGEST.md, "Heartbeat / staleness: schema, not prose".

The rule being tested is not "the panel shows the right numbers". It is: a
consumer must never be shown last-known counters *as if they were current*.
That is the same defect as a bare `20:10` stamp with no date, and as a poller
that died with its shell -- correct value, wrong claim about now.

    "Absence must render as stale/unknown -- never as the last known counters
     displayed as if they were live."

Deliberately NOT tested here: any threshold. The spec says the implementation
chooses one and the tests must not assert it. So this file constrains the
threshold only from the outside, at points no sane value can straddle:

    a digest from just now        -> not stale        (so the threshold is > 0s)
    400 days old                  -> stale            (so it is < 400d)
    3 days old                    -> stale            (the "read it after a
                                                       weekend" case: a 60s
                                                       constant passes the first
                                                       two and still lies)

Nothing asserts 60, 600 or 3600. If an implementation picks a value that fails
the 3-day case, the reading experience is the thing that broke, not this test.

These tests fail today for one reason: there is no `dg` op and no `dg show`
subcommand yet. They must fail with that sentence, not with a traceback.

Run:  python3 tools/test_stale.py -v
"""
import json
import os
import pathlib
import subprocess
import sys
import tempfile
import time
import unittest

HERE = os.path.dirname(os.path.abspath(__file__))
ROOT = os.path.dirname(HERE)
AGENT = os.path.join(ROOT, "inbox_agent.py")
sys.path.insert(0, ROOT)

import inbox_agent  # noqa: E402

FP = "ab" * 16
LABEL = "kisu"
H = "a" * 32
ID = "3a7f0c22be19"


def need(what, section="staleness section"):
    raise AssertionError(f"NOT IMPLEMENTED: {what} -- see TIER2-DIGEST.md {section}")


class Store:
    """A state dir with a hand-written seen.json/peers.json, per the spec's schema."""

    def __init__(self, last_digest_at="missing", rows=None, counters=None):
        self.tmp = tempfile.TemporaryDirectory()
        self.home = self.tmp.name
        self.state = os.path.join(self.home, "state")
        os.makedirs(self.state)
        peer = {
            "rows": rows if rows is not None else {
                ID: {"id": ID, "owner": "peer", "at": time.time() - 400 * 86400,
                     "peer_fp": FP, "n": 412, "h": H, "body": True, "peer_at": None,
                     "peer_n": 412, "peer_h": H, "state": "delivered-both", "why": "",
                     "peer_first": False, "announced": True},
            },
            "orphans": [],
            "counters": counters or {"dg_in": 9, "dg_bad": 0, "stored": 1190, "dropped": 0},
            "caps": {"dg": True, "last": 0},
        }
        if last_digest_at != "missing":
            peer["last_digest_at"] = last_digest_at
        with open(os.path.join(self.state, "seen.json"), "w") as f:
            json.dump({"v": 1, "peers": {FP: peer}}, f)
        with open(os.path.join(self.state, "peers.json"), "w") as f:
            json.dump({FP: {"label": LABEL, "onion": "a" * 56 + ".onion"}}, f)

    def show(self):
        env = dict(os.environ, TORAGENTS_HOME=self.home, TORAGENTS_STATE=self.state)
        proc = subprocess.run([sys.executable, AGENT, "dg", "show"], capture_output=True,
                              text=True, env=env, timeout=60)
        if "invalid choice" in (proc.stderr or ""):
            need("`inbox_agent.py dg show`")
        return proc

    def close(self):
        self.tmp.cleanup()


class StaleState(unittest.TestCase):
    def setUp(self):
        self.stores = []

    def tearDown(self):
        for s in self.stores:
            s.close()

    def store(self, **kw):
        s = Store(**kw)
        self.stores.append(s)
        return s

    # -- the state exists at all -------------------------------------------
    def test_the_spec_state_is_reachable_and_named(self):
        proc = self.store(last_digest_at=time.time()).show()
        out = (proc.stdout + proc.stderr).lower()
        self.assertIn("digest", out, f"dg show produced nothing recognisable: {out[:200]}")
        # 'stale' must be a member of the vocabulary the renderer can produce,
        # not an adjective someone invents at print time.
        states = getattr(inbox_agent, "DG_STATES", None)
        if states is not None:
            self.assertIn("stale", states,
                          f"DG_STATES does not include stale: {states}")
        else:
            proc_stale = self.store(last_digest_at=time.time() - 400 * 86400).show()
            self.assertIn("stale", (proc_stale.stdout + proc_stale.stderr).lower(),
                          "no DG_STATES vocabulary and no stale render: the state does not exist")

    # -- absence must announce itself --------------------------------------
    def test_a_peer_we_never_heard_from_is_stale_not_up_to_date(self):
        proc = self.store(last_digest_at="missing").show()
        out = (proc.stdout + proc.stderr).lower()
        self.assertIn("stale", out,
                      "a peer with no last_digest_at rendered as current -- absence read as freshness")

    def test_an_ancient_digest_is_stale(self):
        proc = self.store(last_digest_at=time.time() - 400 * 86400).show()
        self.assertIn("stale", (proc.stdout + proc.stderr).lower())

    def test_a_weekend_old_digest_is_stale(self):
        """The case a 60-second constant passes and still lies about."""
        proc = self.store(last_digest_at=time.time() - 3 * 86400).show()
        self.assertIn("stale", (proc.stdout + proc.stderr).lower(),
                      "3 days without a digest still rendered as current")

    def test_a_digest_from_just_now_is_not_stale(self):
        proc = self.store(last_digest_at=time.time()).show()
        self.assertNotIn("stale", (proc.stdout + proc.stderr).lower(),
                         "a fresh digest rendered as stale; a warning that always fires is not a warning")

    # -- an empty digest is information ------------------------------------
    def test_an_empty_digest_counts_as_heard_from(self):
        """'We heard from them and they hold nothing' is not 'we heard nothing'."""
        apply_ = getattr(inbox_agent, "dg_apply", None)
        if apply_ is None:
            need("dg_apply()")
        s = self.store(last_digest_at="missing")
        apply_(s.state, FP, {"v": 1, "gen": time.time(), "a": [], "b": []})
        with open(os.path.join(s.state, "seen.json")) as f:
            peer = json.load(f)["peers"][FP]
        self.assertIn("last_digest_at", peer,
                      "an empty digest did not record that we heard from the peer")
        out = (s.show().stdout + s.show().stderr).lower()
        self.assertNotIn("stale", out,
                         "after an accepted empty digest the peer still renders stale")

    # -- no path renders last-known counters as live ------------------------
    def test_stale_never_presents_the_old_counters_as_current(self):
        """The counters here are real (1190 delivered). When stale they may be
        shown, but the qualification must LEAD: it cannot be a footnote under a
        number a reader takes as live."""
        proc = self.store(last_digest_at=time.time() - 400 * 86400).show()
        out = proc.stdout
        lines = [l for l in out.splitlines() if l.strip()]
        self.assertTrue(lines, "dg show printed nothing")
        head = "\n".join(lines[:3]).lower()
        self.assertIn("stale", head,
                      f"staleness was not in the first three lines, so the counters read as live:\n{out}")
        self.assertIn("1190", out,
                      "the stored counters vanished; the contract is to qualify them, not hide them")

    def test_a_store_that_cannot_be_read_says_so_and_does_not_throw(self):
        s = self.store(last_digest_at=time.time())
        with open(os.path.join(s.state, "seen.json"), "w") as f:
            f.write('{"v":1,"peers":{"abab":{"rows":{')      # truncated
        proc = s.show()
        self.assertEqual(proc.returncode, 0, f"a corrupt store threw: {proc.stderr[:200]}")
        self.assertIn("unavailable", (proc.stdout + proc.stderr).lower(),
                      "a corrupt store must render the unavailable line, not silence and not numbers")


class ThresholdIsNotPinned(unittest.TestCase):
    """A guard on this file, not on the implementation."""

    def test_this_file_does_not_assert_a_threshold_number(self):
        """A guard on this file, not on the implementation.

        Scanned with ast, not by substring: a substring check flagged the
        docstring that explains the rule, which is the same class of mistake as
        a measurement that reads the wrong surface. What actually matters is
        whether a *comparison* pins a plausible threshold constant -- so look at
        Compare nodes only, and only for numbers a threshold would plausibly be.
        """
        import ast

        plausible = {30, 60, 120, 300, 600, 900, 1800, 3600, 7200, 86400, 604800}
        tree = ast.parse(pathlib.Path(__file__).read_text(encoding="utf-8"))
        offenders = []
        for node in ast.walk(tree):
            # (a) expression comparisons: `stale_after < 60`
            if isinstance(node, ast.Compare):
                for operand in [node.left, *node.comparators]:
                    if isinstance(operand, ast.Constant) and operand.value in plausible:
                        offenders.append((node.lineno, operand.value))
                continue
            # (b) unittest assertion calls: `self.assertLess(stale_after, 60)`
            # These are ast.Call, NOT ast.Compare -- the first version of this
            # guard scanned only Compare and therefore could not fire on the
            # most likely way a threshold gets pinned. A guard that cannot fire
            # is the bug it exists to prevent.
            if isinstance(node, ast.Call):
                fn = node.func
                name = fn.attr if isinstance(fn, ast.Attribute) else (
                    fn.id if isinstance(fn, ast.Name) else "")
                if not name.startswith("assert"):
                    continue
                for arg in [*node.args, *(kw.value for kw in node.keywords)]:
                    if isinstance(arg, ast.Constant) and arg.value in plausible:
                        offenders.append((node.lineno, arg.value))
        self.assertEqual(offenders, [],
                         f"this file pins implementation thresholds at {offenders} -- "
                         "the spec says tests must not assert the number")
        # the config knob must not be asserted either: its value belongs to config
        src = pathlib.Path(__file__).read_text(encoding="utf-8")
        self.assertNotIn("flush_ms", src.split('"""')[-1],
                         "this file asserts the config knob instead of the behaviour")


if __name__ == "__main__":
    unittest.main(verbosity=2)
