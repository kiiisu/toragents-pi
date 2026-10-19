#!/usr/bin/env python3
"""A synthetic frame in a live inbox must be findable, not shipped.

A test message reached a live daemon once, then rode out in every digest to every
peer until the disclosure bug was found. The lesson is not "clean it up" -- the
record is evidence about how it got there -- it is that doctor should SAY SO.

Two kinds, reported separately because they mean different things:
  artifact   from_fingerprint is not 32 hex at all -> a test frame, not a sender
  stranger   a real-looking fingerprint that is not on the allowlist -> stored but
             unowned; not shipped in digests (the filters see to that), still there

Reported, never deleted. Deleting is how the next occurrence becomes invisible.
"""
import json
import os
import subprocess
import sys
import tempfile
import unittest

HERE = os.path.dirname(os.path.abspath(__file__))
REPO = os.path.dirname(HERE)
AGENT = os.path.join(REPO, "inbox_agent.py")


class DoctorArtifacts(unittest.TestCase):
    def setUp(self):
        self.home = tempfile.mkdtemp(prefix="doctor-art-")
        self.state = os.path.join(self.home, "state")
        os.makedirs(os.path.join(self.state, "inbox"))
        self.addCleanup(lambda: subprocess.run(["rm", "-rf", self.home]))
        self.env = dict(os.environ, TORAGENTS_HOME=self.home, TORAGENTS_STATE=self.state)

    def put(self, mid, fp, body="x", label=""):
        rec = {"id": mid, "received": 1.0, "from_fingerprint": fp, "body": body}
        if label:
            rec["from_label"] = label
        with open(os.path.join(self.state, "inbox", f"20260101-{mid}.json"), "w") as f:
            json.dump(rec, f)

    def doctor(self):
        p = subprocess.run([sys.executable, AGENT, "doctor"], capture_output=True,
                           text=True, env=self.env, timeout=120)
        return p.stdout + p.stderr

    def test_malformed_fingerprint_is_reported_as_an_artifact(self):
        self.put("beefcafe0001", "0" * 32)
        out = self.doctor()
        self.assertIn("artifacts", out, f"doctor did not report the artifact:\n{out}")
        self.assertIn("beefcafe0001", out, "the report must name the record, not just count")

    def test_a_harness_label_is_called_out_without_opening_the_body(self):
        """Triage without trust: the label is self-declared, so the report says so.

        Found on a live box: an "unowned sender" turned out to be our own Tor
        loopback test. Answering that required opening the body, which is exactly the
        step this annotation removes -- while never letting the label become an input
        to a trust decision.
        """
        self.put("beefcafe0003", "0123456789abcdef0123456789abcdef", label="wiretest")
        out = self.doctor()
        self.assertIn("wiretest", out, f"the label was not surfaced:\n{out}")
        self.assertIn("self-declared", out,
                      "a self-chosen label must be presented as self-declared, not as fact")
        self.assertIn("test identity", out, "a harness label should be marked as such")

    def test_an_artifact_also_names_its_label(self):
        self.put("beefcafe0004", "0" * 32, label="self-test")
        out = self.doctor()
        self.assertIn("self-test", out, f"artifact without its label:\n{out}")

    def test_unknown_sender_is_reported_as_unowned(self):
        self.put("beefcafe0002", "0123456789abcdef0123456789abcdef")
        out = self.doctor()
        self.assertIn("unowned", out, f"doctor did not report the stranger:\n{out}")

    def test_a_clean_inbox_says_nothing(self):
        """No records: no line. A detector that speaks when nothing is wrong is noise."""
        out = self.doctor()
        self.assertNotIn("artifacts", out)
        self.assertNotIn("unowned", out)


if __name__ == "__main__":
    unittest.main(verbosity=2)
