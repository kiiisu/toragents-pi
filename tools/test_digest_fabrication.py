#!/usr/bin/env python3
"""The negative control for delivery provenance: a fabricated row must be NAMED.

56 matching rows can never prove this direction. They show two honest stores agree;
they cannot show that a dishonest or mistaken row is refused, because an honest peer
never sends one. So we plant one and read the receiver's own ledger.

Two different lies, because they fail differently:

  UNMINTED  a row whose id the receiver never minted at all, with a plausible h and n.
            Expected: never-stored, and the ledger row carries a reason naming it.
            The receiver ANNOUNCES the fabrication; it does not absorb it.

  REUSED    a row with an id the receiver DID mint but a DIFFERENT h -- a peer
            substituting a body under an existing id. Expected: mismatch (id-reuse),
            and critically the stored body must NOT change: an attestation is a claim,
            not a write. This is the dangerous one -- accepting it would let any peer
            rewrite our history.

Mutation targets, stated so the file can be shown to fail: make dg_reconcile_row
treat a missing local anchor as accepted-only, or let a mismatch fall through to
delivered-both, and the corresponding test must go red.

Usage: python3 tools/test_digest_fabrication.py
"""
import hashlib
import json
import os
import sys
import unittest

HERE = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, HERE)
sys.path.insert(0, os.path.dirname(HERE))

import test_tier2_acceptance as acc  # noqa: E402


def h_of(body):
    return hashlib.sha256(body.encode("utf-8")).hexdigest()[:32]


class Fabrication(unittest.TestCase):
    """A real receiver (B), a real allowlisted sender (A), real framed protocol."""

    def setUp(self):
        # Allowlist BEFORE starting the daemon: the receiver reads peers.json at
        # start, so a peer added afterwards is present on disk but absent in memory
        # and every frame is refused with "sender not on allowlist" -- a test that
        # looks like an auth failure but is really an ordering mistake in the test.
        self.a = acc.Node("fabricator")
        self.b = acc.Node("victim")
        self.a.allow(self.b)
        self.b.allow(self.a)
        self.a.start()
        self.b.start()
        self.addCleanup(self.b.stop)
        self.addCleanup(self.a.stop)

    # -- reading the receiver's own ledger --------------------------------
    def ledger(self):
        """The receiver's seen.json, for the sender's fingerprint."""
        path = os.path.join(self.b.state, "seen.json")
        self.assertTrue(os.path.exists(path), "the receiver kept no ledger")
        with open(path) as f:
            data = json.load(f)
        fp = self.a.fingerprint
        peers = data.get("peers") or {}
        self.assertIn(fp, peers, f"no ledger entry for the sender: {list(data)}")
        return peers[fp]

    def inbox_record(self, mid):
        """Find a stored message by its record id (filenames carry a timestamp)."""
        d = os.path.join(self.b.state, "inbox")
        for name in os.listdir(d):
            if not name.endswith(".json"):
                continue
            with open(os.path.join(d, name)) as f:
                rec = json.load(f)
            if rec.get("id") == mid:
                return rec
        return None

    def row(self, mid):
        rows = self.ledger().get("rows") or {}
        return rows.get(mid)

    # Which side a row lives on is the trap that killed the feature once already, so
    # name it at every call site:
    #   dg "a" = ids WE minted on receipt -> the peer's SENT store
    #   dg "b" = ids the PEER minted for our bodies -> OUR INBOX
    def a_sends_to_b(self, body):
        """B's INBOX gets an id; used for "b" rows (ids we minted on receipt)."""
        reply = self.b.send_with(self.a, body, session="t")
        self.assertTrue(reply and reply.get("ok"), reply)
        d = os.path.join(self.b.state, "inbox")
        newest = max((n for n in os.listdir(d) if n.endswith(".json")),
                     key=lambda n: os.path.getmtime(os.path.join(d, n)))
        with open(os.path.join(d, newest)) as f:
            rec = json.load(f)
        # The digest id is the record's own `id`, NOT the filename: inbox filenames
        # carry a receipt timestamp prefix (1792410927-e4bb785b4f89.json), so taking
        # the stem would invent an id nobody ever minted and the row would reconcile
        # as never-stored -- a fabricated test rather than a fabricated row.
        return rec["id"], rec["body"]

    def b_sends_to_a(self, body):
        """B's SENT store gets an id; used for "a" rows (ids we minted sending)."""
        reply = self.a.send_with(self.b, body, session="t")
        self.assertTrue(reply and reply.get("ok"), reply)
        d = os.path.join(self.b.state, "sent")
        newest = max(os.listdir(d), key=lambda n: os.path.getmtime(os.path.join(d, n)))
        with open(os.path.join(d, newest)) as f:
            rec = json.load(f)
        return rec["local_id"], rec["sha256"]

    # -- the two lies ------------------------------------------------------
    def test_unminted_id_is_announced_not_absorbed(self):
        ghost = "deadbeef0000"
        d = acc.dg(items_a=[acc.item(id_=ghost, h=h_of("a body we never received"))], items_b=[])
        reply = self.b.digest_op(self.a, d)
        self.assertTrue(reply.get("ok"), reply)

        row = self.row(ghost)
        self.assertIsNotNone(row, "the planted id never reached the ledger at all")
        self.assertEqual(row.get("state"), "never-stored",
                         f"a fabricated id must never be recorded as delivered: {row}")
        self.assertTrue(str(row.get("why") or "").strip(),
                        "never-stored without a reason is a silent disagreement, not an announcement")

    def test_inbox_row_with_a_forged_hash_is_mismatch(self):
        """A genuine message we received, re-attested with a different body hash."""
        mid, body = self.a_sends_to_b("genuine-" + os.urandom(4).hex())
        real_h = h_of(body)
        forged = "0" * 32 if real_h != "0" * 32 else "1" * 32

        d = acc.dg(items_a=[], items_b=[acc.item(id_=mid, h=forged)])
        reply = self.b.digest_op(self.a, d)
        self.assertTrue(reply.get("ok"), reply)

        row = self.row(mid)
        self.assertIsNotNone(row, "the forged row never reached the ledger")
        self.assertEqual(row.get("state"), "mismatch",
                         f"a substituted body under a live id must be a mismatch: {row}")

        after = self.inbox_record(mid)
        self.assertEqual(after["body"], body,
                         "a peer's digest rewrote stored content -- the claim became a write")

    def test_id_reuse_keeps_the_FIRST_hash_and_says_so(self):
        """The dangerous lie: reuse an id to substitute a body on a LATER digest.

        Scope, stated because a fixture can lie: this exercises the id-reuse GUARD
        (the peer_h bookkeeping) with no local anchor, so the row is never-stored and
        the guard fires on the second attestation. That the guard also protects a row
        WITH a local anchor is the "b" test's job above, where the local hash is what
        disagrees. Splitting them means neither passes because the other ran.
        """
        mid = "beef" + os.urandom(4).hex()
        first, second = "a" * 32, "b" * 32

        for h in (first, second):
            d = acc.dg(items_a=[acc.item(id_=mid, h=h)], items_b=[])
            reply = self.b.digest_op(self.a, d)
            self.assertTrue(reply.get("ok"), reply)

        row = self.row(mid)
        self.assertIsNotNone(row, "the reused id was never reconciled")
        self.assertEqual(row.get("state"), "mismatch", row)
        self.assertEqual(row.get("peer_h"), first,
                         "the SECOND hash overwrote the first -- a peer can rewrite our history")
        self.assertIn("id-reuse", str(row.get("why") or ""),
                      f"the reason must name id-reuse, not a generic disagreement: {row}")


if __name__ == "__main__":
    unittest.main(verbosity=2)
