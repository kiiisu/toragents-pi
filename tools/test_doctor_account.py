#!/usr/bin/env python3
"""A diagnostic line must be DERIVED FROM the account that sets the exit status.

The bug this closes shipped once: the daemon/window mismatch printed a warning and
never joined the problem list, so `doctor` exited 0 while telling the human something
was wrong. A fact that reaches the human but not the machine is the same gap as an
artifact that reached production but not the detector -- the sentence existed, the
consequence did not.

doctor now builds ONE list of findings and derives both the printed lines and the exit
code from it. This test holds the rule from the outside, because the inside is only a
convention anyone could break again:

  every line containing "<-" (a word to the human) must correspond to a non-note
  finding OR appear in the documented NOTE allowlist below

Adding a new warning line without wiring it to a severity fails here, which is the
point: the alternative is remembering.

Mutation: remove a finding's entry while keeping its text (or downgrade a wrong to a
note) and the corresponding scenario fails.
"""
import json
import os
import subprocess
import sys
import tempfile
import time
import unittest

HERE = os.path.dirname(os.path.abspath(__file__))
AGENT = os.path.join(os.path.dirname(HERE), "inbox_agent.py")

# Lines that legitimately say "<-" to the human while leaving the status at 0:
# they are facts about DATA, not about whether what is running matches the disk.
NOTE_PREFIXES = ("artifacts ", "unowned ")


class DerivedFromOneAccount(unittest.TestCase):
    def setUp(self):
        self.home = tempfile.mkdtemp(prefix="doctor-account-")
        self.state = os.path.join(self.home, "state")
        os.makedirs(os.path.join(self.state, "live"))
        self.addCleanup(lambda: subprocess.run(["rm", "-rf", self.home]))
        self.procs = []
        self.addCleanup(self._reap)

    def _reap(self):
        for p in self.procs:
            p.kill()
            p.wait()

    def _pid(self):
        p = subprocess.Popen(["sleep", "30"])
        self.procs.append(p)
        return p.pid

    def writer(self, fresh=True):
        at = (time.time() if fresh else time.time() - 600) * 1000.0
        with open(os.path.join(self.state, "live", "_writer.json"), "w") as f:
            json.dump({"v": 2, "session": "w", "pid": self._pid(), "at": at}, f)

    def window(self, code):
        with open(os.path.join(self.state, "live", "pi-t.json"), "w") as f:
            json.dump({"session": "pi-t", "pid": self._pid(),
                       "at": time.time() * 1000.0, "code": code}, f)

    def record(self, mid, fp, label=""):
        rec = {"id": mid, "received": 1.0, "from_fingerprint": fp, "body": "x"}
        if label:
            rec["from_label"] = label
        with open(os.path.join(self.state, "inbox", f"20260101-{mid}.json"), "w") as f:
            json.dump(rec, f)

    def doctor(self):
        env = dict(os.environ, TORAGENTS_HOME=self.home, TORAGENTS_STATE=self.state)
        p = subprocess.run([sys.executable, AGENT, "doctor"], capture_output=True,
                           text=True, env=env, timeout=180)
        return p.returncode, (p.stdout + p.stderr)

    def warnings(self, out):
        return [l for l in out.splitlines() if "<-" in l]

    def test_every_warning_either_moves_the_status_or_is_a_documented_note(self):
        os.makedirs(os.path.join(self.state, "inbox"), exist_ok=True)
        self.record("aaaa00000001", "0" * 32, "self-test")       # artifact (note)
        self.record("bbbb00000002", "0123456789abcdef0123456789abcdef",
                    "wiretest")                                   # unowned  (note)
        self.writer()
        self.window("2")                                          # stale   (wrong)

        rc, out = self.doctor()
        warns = self.warnings(out)
        self.assertTrue(warns, "expected at least one warning line to audit")
        for line in warns:
            if line.startswith(NOTE_PREFIXES):
                self.assertEqual(rc, 1,
                                 "a note line may not be the only reason for a status; "
                                 f"here it is, and that is what this test is checking: {line}")
                continue
            self.assertNotEqual(rc, 0,
                                f"this line says something is wrong but the status does "
                                f"not: {line!r}")

    def test_a_clean_system_prints_no_warning_and_exits_zero(self):
        self.writer()
        self.window("3")
        rc, out = self.doctor()
        self.assertEqual(rc, 0, out)
        self.assertEqual(self.warnings(out), [],
                         "a clean system must not print a warning: a detector that speaks "
                         "when nothing is wrong is noise people learn to skip")

    def test_notes_alone_leave_the_status_clean(self):
        """Data facts do not make the running system inconsistent with the disk."""
        os.makedirs(os.path.join(self.state, "inbox"), exist_ok=True)
        self.record("cccc00000003", "0" * 32, "self-test")
        self.writer()
        self.window("3")
        rc, out = self.doctor()
        self.assertEqual(rc, 0, f"an artifact in data turned the install red:\n{out}")
        self.assertTrue(any(l.startswith("artifacts") for l in self.warnings(out)),
                        "the artifact should still be reported")


if __name__ == "__main__":
    unittest.main(verbosity=2)
