#!/usr/bin/env python3
"""Presence: does the record expire, and does a dead pid stop counting as live?

These are the negative controls for the refusal at inbox_agent.py:449. The
refusal is only as good as `live_labels()`: if that function says "live" for a
record that is stale, dead, or deleted, then a targeted message is accepted and
consumed by a window the sender did not address -- the exact silent failure the
refusal exists to prevent.

Four cases, in gale's order. Case 4 is split, and the split is the honest part:
the daemon half (already live here) and the window half (needs a seam that does
not exist yet) are reported separately rather than one test pretending to cover
both.

Run: python3 tools/test_presence.py
"""
import json
import os
import sys
import tempfile
import time
import unittest

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
import inbox_agent as I                                            # noqa: E402

MY_PID = os.getpid()


class Presence(unittest.TestCase):
    def setUp(self):
        self.dir = tempfile.mkdtemp(prefix="presence-")
        os.makedirs(os.path.join(self.dir, "live"), exist_ok=True)

    def record(self, label, pid=MY_PID, age_s=0.0):
        at = (time.time() - age_s) * 1000.0
        with open(os.path.join(self.dir, "live", f"{label}.json"), "w") as f:
            json.dump({"session": label, "pid": pid, "at": at}, f)

    # 1. the file is deleted mid-run: the label must stop being live
    def test_a_deleted_record_stops_being_live(self):
        self.record("pi-window-a")
        self.assertTrue(I.live_labels(self.dir).get("pi-window-a"), "a fresh record must be live")
        os.unlink(os.path.join(self.dir, "live", "pi-window-a.json"))
        live = I.live_labels(self.dir)
        self.assertFalse(live.get("pi-window-a"),
                         "a deleted presence file must not keep the label live")
        # and the refusal branch the daemon takes is keyed on exactly this
        self.assertTrue(not live.get("pi-window-a"), "refusal condition must fire")

    # 2. a record whose pid is dead must not count, however fresh it looks
    def test_a_dead_pid_is_not_live(self):
        dead = 999999
        try:
            os.kill(dead, 0)
            self.skipTest("pid 999999 exists on this box")
        except OSError:
            pass
        self.record("pi-window-dead", pid=dead, age_s=0)
        self.assertFalse(I.live_labels(self.dir).get("pi-window-dead"),
                         "a dead pid with a fresh timestamp must not be live")

    # 3. a fresh pid with an aged timestamp must not count either
    def test_an_aged_record_is_not_live(self):
        stale_s = I.LIVE_STALE_MS / 1000.0 + 5
        self.record("pi-window-stale", age_s=stale_s)
        self.assertFalse(I.live_labels(self.dir).get("pi-window-stale"),
                         f"a record older than LIVE_STALE_MS ({I.LIVE_STALE_MS}ms) must not be live")
        # the boundary itself: just inside the window is still live
        self.record("pi-window-fresh", age_s=max(0.0, stale_s - 30))
        self.assertTrue(I.live_labels(self.dir).get("pi-window-fresh"),
                        "a record inside the window must be live, or the refusal refuses everything")

    # 4a. the daemon half: two live labels, one addressed -> the address is preserved
    def test_two_live_labels_and_one_address_is_carried_and_the_other_stays_live(self):
        self.record("pi-window-a")
        self.record("pi-window-b")
        live = I.live_labels(self.dir)
        self.assertEqual(sorted(k for k, v in live.items() if v), ["pi-window-a", "pi-window-b"])
        # the refusal must NOT fire for the addressed label, and must fire for a third
        self.assertTrue(live.get("pi-window-a"), "the addressed label is live: accept")
        self.assertFalse(live.get("pi-window-c"), "an unaddressed unknown label must be refused")

    # 4b. the window half, stated as a gap instead of faked
    def test_the_second_windows_silence_is_NOT_YET_COVERED(self):
        """The extension decides silence via addressedToMe, which is not exported.

        Whatever this file counted as coverage would be a lie: a message stored
        with to_session="pi-window-a" is filtered out by every other window at
        delivery time, and that filter is one un-exported function. Until it is
        exported (or a two-window harness drives real sessions), the user's
        original complaint -- "messages arrive in BOTH windows" -- is reasoned
        about, not tested. This test documents the gap so the count cannot be
        read as coverage.
        """
        src = open(os.path.join(os.path.dirname(os.path.dirname(
            os.path.abspath(__file__))), "toragents_watch.ts"), encoding="utf-8").read()
        exported = any(line.strip().startswith("export function addressedToMe")
                       for line in src.splitlines())
        self.assertFalse(
            exported,
            "addressedToMe is now exported: replace this gap test with a real "
            "two-window test that asserts the second window receives nothing")


if __name__ == "__main__":
    unittest.main(verbosity=2)
