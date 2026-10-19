#!/usr/bin/env python3
"""A match must not outlive its evidence.

Found by the peer on a live digest (24 a-side rows reported delivered-both whose ids
were in neither sent-store column):

    row = rows.get(mid) or {...}
    if mine:                     # matched THIS time
        row["h"] = mine.get("sha256")
    # no else -- the previous h survives when `mine` is None this time

So a row that matched once kept its anchor forever. Later digests found no local
record, left `h` alone, and dg_reconcile_row returned delivered-both from a match that
no longer existed. The ledger was reporting knowledge it had lost.

This is the fourth instance of one disease: ABSENCE INHERITING AN OLD VERDICT.
  empty registry read as "nothing live"          (presence)
  missing claim read as "unowned"                (claims)
  missing anchor read as "peer fabricated"       (never-stored)
  stale anchor read as a live match              (this file)

The fix clears the anchor when the store cannot produce it, which turns "we still
know" into "we no longer know" -- the honest direction. Both branches are tested
because they are separate code that failed the same way.

Mutation: restore the missing `else` on either branch and the corresponding test must
flip back to delivered-both.
"""
import json
import os
import sys
import unittest

HERE = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, HERE)
sys.path.insert(0, os.path.dirname(HERE))

import test_tier2_acceptance as acc  # noqa: E402
import inbox_agent as ia  # noqa: E402


def h_of(body):
    import hashlib
    return hashlib.sha256(body.encode("utf-8")).hexdigest()[:32]


class StaleAnchor(unittest.TestCase):
    def setUp(self):
        self.a = acc.Node("sender")
        self.b = acc.Node("receiver")
        self.b.allow(self.a)
        self.b.start()
        self.addCleanup(self.b.stop)
        self.addCleanup(self.a.tmp.cleanup)
        # Point the module's own store at the receiver's state dir and restore it
        # afterwards: the record must be written by the real writer, because a
        # fixture that composes its own record can agree with its own bug.
        self._sent_dir, self._state = ia.SENT, ia.STATE
        ia.SENT = os.path.join(self.b.state, "sent")
        ia.STATE = self.b.state
        self.addCleanup(setattr, ia, "SENT", self._sent_dir)
        self.addCleanup(setattr, ia, "STATE", self._state)

    def ledger_row(self, mid):
        with open(os.path.join(self.b.state, "seen.json")) as f:
            data = json.load(f)
        return data["peers"][self.a.fingerprint]["rows"].get(mid)

    def inbox_file(self, mid):
        d = os.path.join(self.b.state, "inbox")
        for name in os.listdir(d):
            if not name.endswith(".json"):
                continue
            with open(os.path.join(d, name)) as f:
                rec = json.load(f)
            if rec.get("id") == mid:
                return os.path.join(d, name)
        return None

    def test_b_side_anchor_cleared_when_the_inbox_record_is_gone(self):
        body = "stale-" + os.urandom(3).hex()
        reply = self.b.send_with(self.a, body, session="t")
        self.assertTrue(reply.get("ok"), reply)
        mid = reply["id"]
        # n must be the REAL length: a digest whose n contradicts the stored length is a
        # mismatch for an honest reason, which would hide the stale-anchor bug behind
        # a correct refusal. (The default n=12 matched "stale-xxxxxx" by luck.)
        dg = acc.dg(items_a=[], items_b=[acc.item(id_=mid, h=h_of(body), n=len(body.encode()))])

        self.assertTrue(self.b.digest_op(self.a, dg).get("ok"))
        row = self.ledger_row(mid)
        self.assertEqual(row.get("state"), "delivered-both",
                         f"precondition: the first digest must match: {row}")

        # The evidence disappears; the ledger must lose the claim with it.
        os.unlink(self.inbox_file(mid))
        self.assertTrue(self.b.digest_op(self.a, dg).get("ok"))
        row = self.ledger_row(mid)
        self.assertEqual(row.get("state"), "never-stored",
                         f"a match outlived its evidence: {row}")
        self.assertIsNone(row.get("h"),
                          "the stale anchor was left in place -- the next reader will "
                          "reconcile this row as known again")
        self.assertIs(row.get("body"), False)

    def test_a_side_anchor_cleared_when_the_sent_record_is_gone(self):
        # A real sent record, written by the store's own writer (not hand-rolled):
        # a fixture that composes its own record can agree with its own bug.
        body = "ours-" + os.urandom(3).hex()
        rec = ia.sent_intent("receiver", "onion", body)
        mid = rec["local_id"]
        dg = acc.dg(items_a=[acc.item(id_=mid, h=h_of(body), n=len(body.encode()))], items_b=[])

        self.assertTrue(self.b.digest_op(self.a, dg).get("ok"))
        self.assertEqual(self.ledger_row(mid).get("state"), "delivered-both")

        os.unlink(os.path.join(self.b.state, "sent", mid + ".json"))
        self.assertTrue(self.b.digest_op(self.a, dg).get("ok"))
        row = self.ledger_row(mid)
        self.assertEqual(row.get("state"), "never-stored", f"stale a-side match: {row}")
        self.assertIsNone(row.get("h"))


class Wording(unittest.TestCase):
    """A store gap must not be reported as a peer's fabrication.

    The peer's 24 rows were exactly this: the journal showed we sent them, the sent
    store had no record. One sentence for three causes blames the peer by default,
    and the reconciler cannot see their store.
    """

    def test_store_gap_is_named_as_ours_not_theirs(self):
        a, b = acc.Node("sender"), acc.Node("receiver")
        b.allow(a)
        b.start()
        self.addCleanup(b.stop)
        self.addCleanup(a.tmp.cleanup)
        self._sent_dir, self._state = ia.SENT, ia.STATE
        ia.SENT, ia.STATE = os.path.join(b.state, "sent"), b.state
        self.addCleanup(setattr, ia, "SENT", self._sent_dir)
        self.addCleanup(setattr, ia, "STATE", self._state)

        body = "gap-" + os.urandom(3).hex()
        rec = ia.sent_intent("receiver", "onion", body)
        mid = rec["local_id"]
        # The journal remembers the send even after the store forgets it.
        with open(os.path.join(b.state, "chat.jsonl"), "a") as f:
            f.write(json.dumps({"at": 1.0, "dir": "out", "peer": "receiver",
                                "id": mid, "body": body}) + "\n")
        os.unlink(os.path.join(b.state, "sent", mid + ".json"))

        dg = acc.dg(items_a=[acc.item(id_=mid, h=h_of(body), n=len(body.encode()))], items_b=[])
        self.assertTrue(b.digest_op(a, dg).get("ok"))
        with open(os.path.join(b.state, "seen.json")) as f:
            row = json.load(f)["peers"][a.fingerprint]["rows"][mid]
        self.assertEqual(row.get("state"), "never-stored")
        self.assertEqual(row.get("cause"), "store-gap",
                         f"our own gap was reported as an unanchorable/foreign id: {row}")
        self.assertIn("our sent store", str(row.get("why")), f"the sentence must own it: {row}")
        # The sentence is about the RECORD, never the body: chat.jsonl truncates at
        # 2000 bytes, so dir="out" proves an attempt existed, not that a body was
        # stored or matched. "we sent this body" would be a claim the journal cannot
        # support.
        self.assertNotIn("we sent", str(row.get("why")).lower(),
                         f"the why claims the journal proves content: {row}")


if __name__ == "__main__":
    unittest.main(verbosity=2)
