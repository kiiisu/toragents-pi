#!/usr/bin/env python3
"""A digest must index ONE conversation, not the whole mailbox.

Found on a live box by looking at which senders appear in one inbox: 55 from one
peer, 11 from another, 1 from a synthetic test frame. dg_attach swept the entire
inbox into `a`, so every digest we sent disclosed third-party message ids, their
body hashes and their byte lengths -- metadata about conversations the recipient is
not part of, carried by a feature whose entire premise is "index, not bodies".

The synthetic frame is the proof: a test message that reached the daemon once rode
out in every subsequent digest to every peer.

Two directions, both tested because they are separate code:
  the `a` list (ids we minted on receipt) -> only messages FROM this peer
  the `b` list (ids the peer minted)     -> only rows addressed TO this peer

Mutation: delete either filter and the corresponding assertion must fail.
"""
import json
import os
import sys
import unittest

HERE = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, HERE)
sys.path.insert(0, os.path.dirname(HERE))

import inbox_agent as ia  # noqa: E402

A_FP, B_FP = "a" * 32, "b" * 32


class Disclosure(unittest.TestCase):
    def setUp(self):
        import tempfile
        self.d = tempfile.mkdtemp(prefix="disclosure-")
        self.inbox = os.path.join(self.d, "inbox")
        self.sent = os.path.join(self.d, "sent")
        os.makedirs(self.inbox)
        os.makedirs(self.sent)
        self._sent_dir, self._state = ia.SENT, ia.STATE
        ia.SENT, ia.STATE = self.sent, self.d
        self.addCleanup(setattr, ia, "SENT", self._sent_dir)
        self.addCleanup(setattr, ia, "STATE", self._state)

    def incoming(self, mid, body, frm):
        rec = {"id": mid, "received": 1.0, "from_fingerprint": frm, "body": body}
        with open(os.path.join(self.inbox, f"20260101-{mid}.json"), "w") as f:
            json.dump(rec, f)
        return mid

    def outgoing(self, mid, body, to_label, onion):
        rec = {"local_id": mid, "peer_id": mid, "state": "confirmed", "at": 1.0,
               "to": to_label, "onion": onion, "len": len(body.encode()),
               "sha256": ia.dg_hash(body), "body": body, "peer_id": mid}
        with open(os.path.join(self.sent, mid + ".json"), "w") as f:
            json.dump(rec, f)
        return mid

    def ids(self, dg):
        return {it["id"] for it in dg.get("a", [])} | {it["id"] for it in dg.get("b", [])}

    def test_a_list_excludes_other_peers_messages(self):
        mine = self.incoming("aaaa00000001", "from A", A_FP)
        other = self.incoming("bbbb00000002", "from B", B_FP)
        dg = ia.dg_attach(self.d, peer_fp=A_FP, peer_label="peer-a")
        ids = self.ids(dg)
        self.assertIn(mine, ids, "the recipient's own messages must be indexed")
        self.assertNotIn(other, ids,
                         "another peer's message id was disclosed in a digest")

    def test_b_list_excludes_rows_sent_to_other_peers(self):
        to_a = self.outgoing("cccc00000003", "to A", "peer-a", "a.onion")
        to_b = self.outgoing("dddd00000004", "to B", "peer-b", "b.onion")
        # Same onion, wrong label: excluded by the LABEL filter alone, so the two
        # filters are tested separately instead of one hiding the other.
        to_b_same_onion = self.outgoing("eeee00000005", "to B, same onion", "peer-b", "a.onion")
        dg = ia.dg_attach(self.d, peer_fp=A_FP, peer_label="peer-a", peer_onion="a.onion")
        ids = self.ids(dg)
        self.assertIn(to_a, ids)
        self.assertNotIn(to_b, ids,
                         "a message to another peer was indexed for this one")
        self.assertNotIn(to_b_same_onion, ids,
                         "the addressee label did not filter a row addressed to another peer")

    def test_no_peer_is_still_the_whole_ledger_for_local_display(self):
        """`dg show` may list everything; the WIRE must not. This pins the split."""
        self.incoming("aaaa00000001", "from A", A_FP)
        self.incoming("bbbb00000002", "from B", B_FP)
        dg = ia.dg_attach(self.d)
        self.assertEqual(len(self.ids(dg)), 2, "local display should still see both")


if __name__ == "__main__":
    unittest.main(verbosity=2)
