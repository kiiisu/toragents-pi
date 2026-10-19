#!/usr/bin/env python3
"""Presence may gate ADDRESSING. It must never gate DELIVERY.

The failure this pins, found on a real peer: a box running an older build writes no
presence at all. Its windows are open, its registry is empty, and a targeted message
is refused "no live session on this box" — which is not "cannot address", it is
"cannot receive", for exactly the mail the sender bothered to address.

The rule, and it is a spec rather than a description of today:

  an absent writer degrades TARGETING (accept unverified, deliver to the owner, say
  so on the receipt) and never DELIVERY (the bytes are stored either way).

Absence is the UNTRUSTED default, not evidence of absence. Only a fresh heartbeat
marker licenses an empty registry to mean "no windows here", which is the one reading
that permits a refusal.

NOTE ON THE POSITIVE MARKER. An earlier draft of this file demanded that
state/live/_writer.json EXIST in a directory where nothing had ever run. That is
impossible by construction, and inverting it is the fix: the absent case has to be
the safe one, so a marker is required for TRUST (refusal), not for the degrade path.

NOTE ON A TEST I DELETED, kept because it is the rule (peer's, 2026-10-19): a second
test here asserted almost nothing and passed trivially, so `expectedFailure` reported
it as an unexpected success. A test that cannot fail does not become a test by being
marked red.

Three states, each with its own assertion:
  absent writer            -> stored, to_session dropped, presence="unverified: ..."
  trusted writer, no label -> REFUSED, error names the label
  trusted writer, label up -> stored, to_session kept
"""
import json
import os
import sys
import time
import unittest

HERE = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, HERE)
sys.path.insert(0, os.path.dirname(HERE))

import test_tier2_acceptance as acc  # noqa: E402
import inbox_agent as ia  # noqa: E402


class AbsentWriter(unittest.TestCase):
    def setUp(self):
        self.a = acc.Node("sender")          # never starts a daemon; just a keypair
        self.b = acc.Node("receiver")
        self.b.allow(self.a)
        self.b.start()
        self.addCleanup(self.b.stop)
        self.addCleanup(self.a.tmp.cleanup)
        self.live = os.path.join(self.b.state, "live")

    # -- helpers ----------------------------------------------------------
    def write_writer(self, fresh=True):
        os.makedirs(self.live, exist_ok=True)
        rec = {"v": 2, "session": "writer", "pid": os.getpid(),
               "at": (time.time() if fresh else time.time() - 600) * 1000.0}
        with open(os.path.join(self.live, ia.WRITER_FILE), "w") as f:
            json.dump(rec, f)

    def write_session(self, label, fresh=True):
        os.makedirs(self.live, exist_ok=True)
        rec = {"session": label, "pid": os.getpid(),
               "at": (time.time() if fresh else time.time() - 600) * 1000.0}
        with open(os.path.join(self.live, label + ".json"), "w") as f:
            json.dump(rec, f)

    def stored(self, reply):
        mid = reply.get("id")
        d = os.path.join(self.b.state, "inbox")
        for name in os.listdir(d):
            if not name.endswith(".json"):
                continue
            with open(os.path.join(d, name)) as f:
                rec = json.load(f)
            if rec.get("id") == mid:
                return rec
        return None

    def send(self, target):
        body = "presence-" + os.urandom(3).hex()
        reply = self.b.send_with(self.a, body, session=None) if target is None else \
            self.b.send_with(self.a, body, session=None)
        return body, reply

    def send_targeted(self, target):
        """One send carrying to_session, through the real framed protocol."""
        body = "presence-" + os.urandom(3).hex()
        nonce = self.b.hello()
        payload = {"op": "send", "nonce": nonce, "body": body,
                   "pubkey": self.a.keys.public().to_dict(),
                   "sig": acc.wire.b64e(self.a.keys.sign(acc.wire.b64d(nonce) + body.encode())),
                   "from_session": "sender", "to_session": target}
        return body, self.b.raw_op(payload)

    # -- the property ------------------------------------------------------
    def test_1_absent_writer_degrades_addressing_and_delivers(self):
        self.assertFalse(os.path.exists(self.live), "precondition: no live dir at all")
        body, reply = self.send_targeted("pi-ghost")
        self.assertTrue(reply.get("ok"),
                        f"delivery was gated on presence; the message was lost: {reply}")
        self.assertIn("unverified", str(reply.get("presence") or ""),
                      f"the receipt must say the targeting was not honoured: {reply}")
        rec = self.stored(reply)
        self.assertIsNotNone(rec, "accepted but not stored")
        self.assertEqual(rec.get("body"), body)
        self.assertNotIn("to_session", rec,
                         "the address must be DROPPED when it cannot be verified, so the "
                         "owning window delivers it instead of it being mis-delivered")
        self.assertIn("unverified", rec.get("presence", ""))

    def test_2_stale_writer_is_not_trusted_either(self):
        self.write_writer(fresh=False)
        body, reply = self.send_targeted("pi-ghost")
        self.assertTrue(reply.get("ok"), f"a stale writer must not block delivery: {reply}")
        self.assertIn("stale", str(reply.get("presence") or ""), reply)

    def test_3_trusted_writer_refuses_a_label_that_is_not_here(self):
        self.write_writer(fresh=True)
        body, reply = self.send_targeted("pi-ghost")
        self.assertFalse(reply.get("ok"), f"a trusted empty registry must refuse: {reply}")
        self.assertIn("pi-ghost", str(reply.get("error") or ""),
                      "the refusal must name the label the sender asked for")
        self.assertIn("live_sessions", reply, "the refusal must say what IS live")

    def test_4_trusted_writer_and_a_live_label_is_addressed_normally(self):
        self.write_writer(fresh=True)
        self.write_session("pi-here")
        body, reply = self.send_targeted("pi-here")
        self.assertTrue(reply.get("ok"), reply)
        self.assertFalse(reply.get("presence"), f"nothing was degraded: {reply}")
        rec = self.stored(reply)
        self.assertEqual(rec.get("to_session"), "pi-here")


if __name__ == "__main__":
    unittest.main(verbosity=2)
