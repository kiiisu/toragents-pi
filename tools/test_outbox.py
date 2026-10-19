#!/usr/bin/env python3
"""Tests for tools/outbox.py -- the two failures that would make it useless.

An outbox has exactly two jobs, and each has a test here that fails if the job
is done badly:

  never lose a body   -> a failing send must leave the item, with its attempt
                         count and the reason, and the body byte-identical.
  never send twice    -> two flushers racing one item must produce exactly ONE
                         send (a real subprocess race, not a simulated one),
                         and an ambiguous verdict must be parked, not retried.

A third test guards the case that makes a durable queue lie: a send that times
out may well have arrived. That must be recorded as ambiguous and left alone by
default, because "retry until exit 0" silently duplicates messages.

Run:  python3 tools/test_outbox.py
"""
import json
import os
import subprocess
import sys
import tempfile
import time
import unittest

HERE = os.path.dirname(os.path.abspath(__file__))
OUTBOX = os.path.join(HERE, "outbox.py")
sys.path.insert(0, HERE)
import outbox as O                     # noqa: E402

STUB = r'''
import json, os, sys, time
counter = os.environ["STUB_COUNTER"]
mode = os.environ.get("STUB_MODE", "ok")
with open(counter, "a") as f:            # one byte per invocation, O_APPEND
    f.write("x")
try:
    body = open(sys.argv[sys.argv.index("--body-file") + 1], encoding="utf-8").read()
    open(os.environ["STUB_LASTBODY"], "w", encoding="utf-8").write(body)
except Exception:
    pass
if mode == "ok":
    print(json.dumps({"ok": True, "id": "stub-id-1"})); sys.exit(0)
if mode == "fail":
    print(json.dumps({"ok": False, "error": "cannot reach peer after 4 attempts"}))
    print("  MEASURED: a peer whose tor has just restarted takes about THREE MINUTES", file=sys.stderr)
    sys.exit(1)
if mode == "badjson":
    print("not json at all"); sys.exit(0)          # exit 0 but no verdict
if mode == "slow_ok":
    time.sleep(float(os.environ.get("STUB_SLEEP", "0.8")))
    print(json.dumps({"ok": True, "id": "slow-id"})); sys.exit(0)
if mode == "hang":
    time.sleep(60); sys.exit(0)
sys.exit(3)
'''


class Base(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.state = os.path.join(self.tmp.name, "state")
        self.stub = os.path.join(self.tmp.name, "stub.py")
        with open(self.stub, "w", encoding="utf-8") as f:
            f.write(STUB)
        self.counter = os.path.join(self.tmp.name, "calls")
        self.lastbody = os.path.join(self.tmp.name, "lastbody")
        self.env = dict(os.environ)
        self.env.update(STUB_COUNTER=self.counter, STUB_LASTBODY=self.lastbody,
                        TORAGENTS_SEND_CMD=f"{sys.executable} {self.stub}",
                        TORAGENTS_STATE=self.state)
        self._old = dict(os.environ)
        os.environ.update({k: v for k, v in self.env.items() if k in
                           ("STUB_COUNTER", "STUB_LASTBODY", "TORAGENTS_SEND_CMD", "TORAGENTS_STATE")})
        O.ensure(self.state)

    def tearDown(self):
        os.environ.clear()
        os.environ.update(self._old)

    def calls(self):
        try:
            with open(self.counter, encoding="utf-8") as f:
                return len(f.read())
        except FileNotFoundError:
            return 0

    def add(self, body="hello over tor", to="kisu"):
        bf = os.path.join(self.tmp.name, f"body-{time.time_ns()}.txt")
        with open(bf, "w", encoding="utf-8") as f:
            f.write(body)
        ns = type("A", (), {"body_file": bf, "to": to})()
        return O.cmd_add(ns, self.state)

    def flush(self, *extra):
        return subprocess.run([sys.executable, OUTBOX, "--state", self.state, "flush", *extra],
                              capture_output=True, text=True, env=self.env, timeout=120)

    def items(self):
        return O.items(self.state)


class TestAdd(Base):
    def test_add_stores_the_body_and_a_hash(self):
        self.assertEqual(self.add("the exact words"), 0)
        p = self.items()
        self.assertEqual(len(p), 1)
        it = O.load(p[0])
        self.assertEqual(it["body"], "the exact words")
        self.assertEqual(it["to"], "kisu")
        self.assertEqual(it["attempts"], 0)
        import hashlib
        self.assertEqual(it["sha256"], hashlib.sha256(b"the exact words").hexdigest())

    def test_adding_the_same_body_twice_queues_one_item(self):
        self.add("same")
        self.add("same")
        self.assertEqual(len(self.items()), 1, "a supervisor calling add twice must not double-queue")

    def test_same_body_different_peer_is_a_different_item(self):
        self.add("same", to="kisu")
        self.add("same", to="maestro-pc")
        self.assertEqual(len(self.items()), 2)

    def test_bom_is_stripped_and_empty_bodies_refused(self):
        bf = os.path.join(self.tmp.name, "bom.txt")
        with open(bf, "w", encoding="utf-8-sig") as f:
            f.write("windows wrote this")
        O.cmd_add(type("A", (), {"body_file": bf, "to": "kisu"})(), self.state)
        with open(self.items()[0], encoding="utf-8") as f:
            self.assertFalse(json.load(f)["body"].startswith("\ufeff"))

        empty = os.path.join(self.tmp.name, "empty.txt")
        open(empty, "w").close()
        with self.assertRaises(SystemExit):
            O.cmd_add(type("A", (), {"body_file": empty, "to": "kisu"})(), self.state)


class TestFlushSuccess(Base):
    def test_success_sends_once_and_deletes(self):
        self.add("deliver me")
        r = self.flush()
        self.assertEqual(r.returncode, 0, r.stdout + r.stderr)
        self.assertEqual(self.calls(), 1)
        self.assertEqual(self.items(), [], "a successful send must leave no item")
        with open(self.lastbody, encoding="utf-8") as f:
            self.assertEqual(f.read(), "deliver me", "the peer must receive the queued bytes verbatim")

    def test_success_is_not_retried_by_the_next_flush(self):
        self.add("deliver me")
        self.flush()
        r = self.flush()
        self.assertEqual(r.returncode, 0)
        self.assertEqual(self.calls(), 1, "second flush re-sent a message that already went")


class TestFlushFailure(Base):
    def test_failure_keeps_the_item_with_the_reason(self):
        self.add("keep me")
        os.environ["STUB_MODE"] = "fail"
        self.env["STUB_MODE"] = "fail"
        r = self.flush()
        self.assertEqual(r.returncode, 2, r.stdout + r.stderr)
        p = self.items()
        self.assertEqual(len(p), 1, "a failed send must never drop the body")
        it = O.load(p[0])
        self.assertEqual(it["attempts"], 1)
        self.assertIn("cannot reach peer", it["last_error"])
        self.assertEqual(it["body"], "keep me")
        self.assertIn("FAILED", r.stdout)
        self.assertIn("cannot reach peer", r.stdout)

    def test_second_failure_increments_and_still_loses_nothing(self):
        self.add("keep me")
        self.env["STUB_MODE"] = "fail"
        self.flush()
        self.flush()
        self.assertEqual(self.calls(), 2)
        it = O.load(self.items()[0])
        self.assertEqual(it["attempts"], 2)
        self.assertEqual(it["body"], "keep me")

    def test_exit_zero_without_a_verdict_is_not_success(self):
        # A send that exits 0 but prints nothing must not count as delivered.
        self.add("verify me")
        self.env["STUB_MODE"] = "badjson"
        r = self.flush()
        self.assertEqual(len(self.items()), 1, "no verdict = not sent = item kept")
        self.assertIn("FAILED", r.stdout)

    def test_corrupt_item_is_never_deleted(self):
        self.add("one")
        junk = os.path.join(self.state, "outbox", "1-deadbeef.json")
        with open(junk, "w", encoding="utf-8") as f:
            f.write("{ this is not json")
        r = self.flush()
        self.assertTrue(os.path.exists(junk), "corrupt item was deleted: that loses a body")
        self.assertIn("CORRUPT", r.stdout)


class TestNoDoubleSend(Base):
    def test_two_flushers_racing_one_item_send_it_exactly_once(self):
        """The real race: two processes, one item, a send slow enough to overlap."""
        self.add("race me")
        self.env["STUB_MODE"] = "slow_ok"
        self.env["STUB_SLEEP"] = "0.9"
        a = subprocess.Popen([sys.executable, OUTBOX, "--state", self.state, "flush"],
                             stdout=subprocess.PIPE, stderr=subprocess.STDOUT, text=True, env=self.env)
        time.sleep(0.05)                       # let A take the claim first
        b = subprocess.Popen([sys.executable, OUTBOX, "--state", self.state, "flush"],
                             stdout=subprocess.PIPE, stderr=subprocess.STDOUT, text=True, env=self.env)
        oa = a.communicate(timeout=60)[0]
        ob = b.communicate(timeout=60)[0]
        self.assertEqual(self.calls(), 1,
                         f"sent {self.calls()} times for one item\nA:{oa}\nB:{ob}")
        self.assertEqual(self.items(), [], "item should be gone after the successful send")

    def test_a_fresh_claim_blocks_a_second_flusher(self):
        self.add("claimed")
        cpath = os.path.join(self.state, "outbox", ".claims",
                             os.path.basename(self.items()[0]) + ".claim")
        with open(cpath, "w", encoding="utf-8") as f:
            json.dump({"pid": 999999, "at": time.time()}, f)
        r = self.flush()
        self.assertEqual(self.calls(), 0, "sent an item another flusher had claimed")
        self.assertEqual(len(self.items()), 1)
        self.assertEqual(r.returncode, 2)

    def test_a_stale_claim_is_taken_over(self):
        self.add("orphaned")
        cpath = os.path.join(self.state, "outbox", ".claims",
                             os.path.basename(self.items()[0]) + ".claim")
        with open(cpath, "w", encoding="utf-8") as f:
            json.dump({"pid": 999999, "at": time.time()}, f)
        old = time.time() - (O.CLAIM_STALE_S + 60)
        os.utime(cpath, (old, old))
        r = self.flush()
        self.assertEqual(self.calls(), 1, "a dead flusher's claim must not wedge the queue forever")
        self.assertEqual(self.items(), [])
        self.assertEqual(r.returncode, 0)


class TestAmbiguity(Base):
    def test_timeout_is_parked_not_retried(self):
        self.add("maybe arrived")
        os.environ["STUB_MODE"] = "hang"
        self.env["STUB_MODE"] = "hang"
        item = self.items()[0]
        outcome, info = O.try_item(self.state, item, timeout=0.4)
        self.assertEqual(outcome, "ambiguous", info)
        it = O.load(item)
        self.assertTrue(it["ambiguous"])
        self.assertIn("TIMEOUT", it["last_error"])
        self.assertEqual(it["body"], "maybe arrived")

        r = self.flush()                        # must NOT resend
        self.assertEqual(self.calls(), 1, "retried an ambiguous item without being asked")
        self.assertIn("AMBIGUOUS", r.stdout)
        self.assertEqual(len(self.items()), 1)

    def test_retry_ambiguous_is_opt_in(self):
        self.add("maybe arrived")
        os.environ["STUB_MODE"] = "hang"
        self.env["STUB_MODE"] = "hang"
        item = self.items()[0]
        O.try_item(self.state, item, timeout=0.4)
        self.env["STUB_MODE"] = "ok"
        os.environ["STUB_MODE"] = "ok"
        r = self.flush("--retry-ambiguous")
        self.assertEqual(self.calls(), 2, "the opt-in flag did not resend")
        self.assertEqual(self.items(), [])
        self.assertEqual(r.returncode, 0)


class TestBudget(Base):
    def test_until_budget_stops_cleanly_and_loses_nothing(self):
        self.add("first", to="kisu")
        time.sleep(0.01)
        self.add("second", to="kisu")
        self.env["STUB_MODE"] = "slow_ok"
        self.env["STUB_SLEEP"] = "1.2"
        r = self.flush("--until", "1.0")
        remaining = self.items()
        self.assertEqual(len(remaining), 1, f"budget handling lost or sent too much: {r.stdout}")
        self.assertEqual(self.calls(), 1)
        bodies = {O.load(p)["body"] for p in remaining}
        self.assertIn("second", bodies, "the untouched body must still be queued verbatim")
        self.assertEqual(r.returncode, 2)
        self.assertIn("budget", r.stdout.lower())


class TestList(Base):
    def test_list_shows_pending_and_the_reason(self):
        self.add("why not")
        self.env["STUB_MODE"] = "fail"
        self.flush()
        r = subprocess.run([sys.executable, OUTBOX, "--state", self.state, "list"],
                           capture_output=True, text=True, env=self.env, timeout=60)
        self.assertIn("to=kisu", r.stdout)
        self.assertIn("attempts=1", r.stdout)
        self.assertIn("cannot reach peer", r.stdout)


if __name__ == "__main__":
    unittest.main(verbosity=2)
