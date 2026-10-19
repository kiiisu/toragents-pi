#!/usr/bin/env python3
"""The allowlist is the security boundary, because deposit IS injection.

An accepted body is placed in front of an agent that may act on it. So an allowlist
entry does not mean "may send us mail" -- it means "may speak into an agent's
context", which is the strongest surface this system has. That makes state/peers.json
the most consequential file in the state directory: anything that can write to it can
talk to an agent.

Hence this test: NO wire operation may modify it. Not one, not with a helpful field
name, not because the sender is already trusted. The only writers are the operator
commands (add-peer / rm-peer), which run in a shell the peer cannot reach.

It hashes peers.json, drives every op the daemon serves plus invented ones, and
requires the file to be byte-identical afterwards. A mutation that lets any op write
the allowlist must fail it -- that is the whole claim.

Usage: python3 tools/test_allowlist_write.py
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


def digest_of(path):
    if not os.path.exists(path):
        return None
    with open(path, "rb") as f:
        return hashlib.sha256(f.read()).hexdigest()


class AllowlistNotWritable(unittest.TestCase):
    def setUp(self):
        self.a = acc.Node("peer")
        self.b = acc.Node("receiver")
        self.b.allow(self.a)
        self.b.start()
        self.addCleanup(self.b.stop)
        self.addCleanup(self.a.tmp.cleanup)
        self.peers = os.path.join(self.b.state, "peers.json")
        self.assertTrue(os.path.exists(self.peers), "precondition: allowlist exists")

    def assert_unchanged(self, label, before):
        after = digest_of(self.peers)
        self.assertEqual(before, after,
                         f"{label} modified the allowlist -- deposit is injection, so "
                         f"any wire path that can write this file can talk to an agent")
        with open(self.peers) as f:
            data = json.load(f)
        self.assertIn(self.a.fingerprint, data,
                      f"{label} removed the existing entry")

    def test_no_wire_op_can_write_the_allowlist(self):
        before = digest_of(self.peers)

        # A normal, fully authorised message.
        self.b.send_with(self.a, "hello", session="t")

        # The same, carrying a digest and a target.
        self.b.send_with(self.a, "hello again", digest=acc.dg(items_a=[], items_b=[]),
                         session="t")

        # The digest op on its own.
        self.b.digest_op(self.a, acc.dg(items_a=[], items_b=[]))

        # Invented ops and invented fields, including ones that ASK to be trusted.
        for payload in (
            {"op": "add-peer", "fingerprint": "f" * 32, "onion": "x.onion", "label": "evil"},
            {"op": "peers", "add": {"fingerprint": "f" * 32}},
            {"op": "hello", "add_peer": True, "trust": True},
            {"op": "tlv", "session_request": {"name": "x"}},
            {"op": "ping", "set_peers": {"f" * 32: {"label": "evil"}}},
        ):
            try:
                self.b.raw_op(payload)
            except Exception:
                pass          # a refused or dropped frame is a fine outcome
            self.assert_unchanged(f"op {payload.get('op')!r}", before)

    def test_a_digest_cannot_register_or_revive_a_peer(self):
        before = digest_of(self.peers)
        stranger = "e" * 32
        # A digest whose rows look like they come from an unknown identity: rows are
        # bookkeeping about messages, never a vouch for a sender.
        self.b.digest_op(self.a, acc.dg(items_a=[acc.item(id_="aaaabbbbcccc", h="a" * 32)],
                                        items_b=[]))
        self.assert_unchanged("digest with unknown rows", before)
        with open(self.peers) as f:
            self.assertNotIn(stranger, json.load(f))


if __name__ == "__main__":
    unittest.main(verbosity=2)
