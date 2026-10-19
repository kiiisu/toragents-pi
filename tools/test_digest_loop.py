#!/usr/bin/env python3
"""The digest LOOP: build -> frame -> apply -> reconcile, with real stores.

Why this file exists: 28 acceptance tests called dg_apply with hand-built
digests, and six green commits sat over a loop that could not reconcile a single
real row. Each unit was right; the wiring between them was never exercised in
the direction the wire actually travels.

The failure it pins, in one line: dg_attach built `a` from our SENT store, but
`a` holds ids WE minted on receipt, which live in our INBOX. So every `a` row was
structurally unmatchable and reconciled as never-stored -- on both machines, with
every body present on disk.

This test builds the digest from stores, ships it through the real framing to a
second state dir, applies it, and requires delivered-both. A unit test cannot
catch a side swap; this can.
"""
import json
import os
import shutil
import sys
import tempfile
import time
import unittest

HERE = os.path.dirname(os.path.abspath(__file__))
REPO = os.path.dirname(HERE)
sys.path.insert(0, REPO)
import inbox_agent as ia  # noqa: E402

PEER_FP = "cd" * 16


def write_record(path, rec):
    os.makedirs(os.path.dirname(path), exist_ok=True)
    with open(path, "w") as f:
        json.dump(rec, f)


class Loop(unittest.TestCase):
    """Two machines' state dirs, one message in each direction."""

    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.me = os.path.join(self.tmp.name, "me", "state")
        self.them = os.path.join(self.tmp.name, "them", "state")
        for d in (self.me, self.them):
            os.makedirs(os.path.join(d, "inbox"), exist_ok=True)
            os.makedirs(os.path.join(d, "sent"), exist_ok=True)

        # A body the PEER sent us: we minted "id-from-them" on receipt.
        self.body_theirs = "they sent this, and we hold it complete"
        write_record(os.path.join(self.me, "inbox", "1-id-from-them.json"),
                     {"id": "id-from-them", "body": self.body_theirs,
                      "received": time.time(), "from_fingerprint": PEER_FP})

        # A body WE sent them: they minted "id-from-me" on receipt, so our sent
        # record carries it as peer_id. This is the row the bug could not anchor.
        self.body_mine = "we sent this, and they hold it complete"
        write_record(os.path.join(self.me, "sent", "local1.json"),
                     {"local_id": "local1", "state": "confirmed",
                      "peer_id": "id-from-me", "body": self.body_mine,
                      "len": len(self.body_mine.encode()),
                      "sha256": ia.dg_hash(self.body_mine), "at": time.time(),
                      "to": "peer", "onion": "x.onion"})

        # The peer's mirror of the same two messages.
        write_record(os.path.join(self.them, "inbox", "1-id-from-me.json"),
                     {"id": "id-from-me", "body": self.body_mine,
                      "received": time.time(), "from_fingerprint": "ab" * 16})
        write_record(os.path.join(self.them, "sent", "local2.json"),
                     {"local_id": "local2", "state": "confirmed",
                      "peer_id": "id-from-them", "body": self.body_theirs,
                      "len": len(self.body_theirs.encode()),
                      "sha256": ia.dg_hash(self.body_theirs), "at": time.time(),
                      "to": "peer", "onion": "y.onion"})

    def tearDown(self):
        self.tmp.cleanup()

    def test_the_built_digest_round_trips_and_reconciles_delivered_both(self):
        """The whole loop, both directions. This is the test six commits lacked."""
        digest = ia.dg_attach(self.me)
        ok, why = ia.dg_validate(digest)
        self.assertTrue(ok, f"our own digest does not validate: {why}")

        self.assertEqual([it["id"] for it in digest["a"]], ["id-from-them"],
                         "a must carry the id WE minted on receipt (our inbox)")
        self.assertEqual([it["id"] for it in digest["b"]], ["id-from-me"],
                         "b must carry the id THEY minted for our body (our sent, peer_id)")

        result = ia.dg_apply(self.them, "ab" * 16, digest)
        rows = result.get("rows") or {}
        self.assertEqual(rows.get("id-from-them", {}).get("state"), "delivered-both",
                         "the peer could not anchor the id we minted on receipt")
        self.assertEqual(rows.get("id-from-me", {}).get("state"), "delivered-both",
                         "the peer could not anchor the id it minted for our body")

    def test_swapping_the_sides_breaks_it(self):
        """The mutation this test exists for, applied to the builder itself.

        If `a` is built from sent instead of inbox, the peer reconciles the row as
        never-stored even though it holds the body. Unit tests cannot see this,
        because they hand dg_apply ids that were never routed through a builder.
        """
        digest = ia.dg_attach(self.me)
        swapped = {"v": 1, "gen": time.time(), "a": digest["b"], "b": digest["a"]}
        result = ia.dg_apply(self.them, "ab" * 16, swapped)
        states = {k: v.get("state") for k, v in (result.get("rows") or {}).items()}
        self.assertNotEqual(states.get("id-from-them"), "delivered-both",
                            "the sides are not load-bearing; this test cannot fire")

    def test_an_unconfirmed_send_is_not_attested(self):
        """A digest is a claim about what we HOLD. A pending send is a hope."""
        write_record(os.path.join(self.me, "sent", "local3.json"),
                     {"local_id": "local3", "state": "pending", "body": "not yet",
                      "len": 7, "sha256": ia.dg_hash("not yet"), "at": time.time(),
                      "peer_id": "id-pending"})
        digest = ia.dg_attach(self.me)
        ids = [it["id"] for it in digest["b"]]
        self.assertNotIn("id-pending", ids,
                         "an unconfirmed send was attested as held")


if __name__ == "__main__":
    unittest.main(verbosity=2)
