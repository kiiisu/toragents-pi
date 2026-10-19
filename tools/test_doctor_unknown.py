#!/usr/bin/env python3
"""Two kinds of stale window, and only one of them writes a marker.

  marker present, ext old      -> detected, printed as "pre-version stale"
  marker absent entirely       -> a window running an extension older than markLive
                                  writes NOTHING, so it is invisible

The second case is why the writer marker is load-bearing for the VERSION detector and
not only for the refusal path: without it, "no windows reported" and "no window exists"
are the same output, and we would infer a negative from an absence -- the mistake this
codebase made in four subsystems before it was named.

Three states, same shape as the refusal path:
  fresh writer, zero windows   -> genuinely none          exit 0
  absent writer                -> cannot answer           exit 2 (WAIT)
  stale writer                 -> cannot answer           exit 2 (WAIT)
  fresh writer, old window     -> known and wrong         exit 1 (REPAIR)

1 and 2 are different actions: repair something, or wait for a window to speak. One
code for both makes a script wait when it should repair -- the same lossy compression
as reporting a store gap as a peer's fabrication.

Mutation: drop the writer check and the "cannot answer" cases print "no live window" —
a confident negative built on nothing.
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
sys.path.insert(0, os.path.dirname(HERE))
# Import the expectation instead of repeating the number: a hardcoded version inside a
# test is the same rot as a hardcoded version in code, one level down.
from inbox_agent import EXPECTED_EXT_VERSION  # noqa: E402


class DoctorCannotAnswer(unittest.TestCase):
    def setUp(self):
        self.home = tempfile.mkdtemp(prefix="doctor-unknown-")
        self.state = os.path.join(self.home, "state")
        os.makedirs(os.path.join(self.state, "live"))
        self.addCleanup(lambda: subprocess.run(["rm", "-rf", self.home]))
        self.procs = []
        self.addCleanup(self._reap)

    def _reap(self):
        for p in self.procs:
            p.kill()
            p.wait()

    def writer(self, fresh=True, alive=True):
        proc = subprocess.Popen(["sleep", "30"]) if alive else None
        if proc:
            self.procs.append(proc)
        pid = proc.pid if proc else 999999
        at = (time.time() if fresh else time.time() - 600) * 1000.0
        with open(os.path.join(self.state, "live", "_writer.json"), "w") as f:
            json.dump({"v": 2, "session": "w", "pid": pid, "at": at,
                       "code": EXPECTED_EXT_VERSION}, f)

    def doctor(self):
        env = dict(os.environ, TORAGENTS_HOME=self.home, TORAGENTS_STATE=self.state)
        p = subprocess.run([sys.executable, AGENT, "doctor"], capture_output=True,
                           text=True, env=env, timeout=180)
        return p.returncode, p.stdout + p.stderr

    def window(self, code, fresh=True):
        """A live window record reporting `code`."""
        proc = subprocess.Popen(["sleep", "30"])
        self.procs.append(proc)
        with open(os.path.join(self.state, "live", "pi-test.json"), "w") as f:
            json.dump({"session": "pi-test", "pid": proc.pid,
                       "at": time.time() * 1000.0, "code": code}, f)

    def window_line(self, out):
        for line in out.splitlines():
            if line.startswith("window"):
                return line
        return ""

    def test_fresh_writer_with_no_windows_says_none(self):
        self.writer()
        rc, out = self.doctor()
        line = self.window_line(out)
        self.assertIn("no live window", line, line)
        self.assertNotIn("cannot answer", line, line)

    def test_absent_writer_cannot_answer(self):
        rc, out = self.doctor()
        line = self.window_line(out)
        self.assertIn("cannot answer", line,
                      f"an absent writer was read as 'no windows': {line}")
        self.assertIn("absent", line, line)
        self.assertEqual(rc, 2, "cannot-answer must be 2 (wait), not 1 (repair)")

    def test_stale_writer_cannot_answer(self):
        self.writer(fresh=False)
        rc, out = self.doctor()
        line = self.window_line(out)
        self.assertIn("cannot answer", line, line)
        self.assertIn("stale", line, line)
        self.assertEqual(rc, 2)


    def test_a_stale_window_against_a_fresh_writer_is_a_known_wrong(self):
        """The complement: a window that speaks, and speaks the wrong version."""
        self.writer()
        self.window("2")                       # older than EXPECTED_EXT_VERSION
        rc, out = self.doctor()
        self.assertIn("pi-test", out, out)
        self.assertIn("stale", out, out)
        self.assertEqual(rc, 1, "a known mismatch must be 1 (repair), not 2 (wait)")

    def test_a_current_window_is_fine(self):
        self.writer()
        self.window(EXPECTED_EXT_VERSION)
        rc, out = self.doctor()
        self.assertEqual(rc, 0, out)
        self.assertIn("ok", self.window_line(out), out)


if __name__ == "__main__":
    unittest.main(verbosity=2)
