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
  absent writer                -> cannot answer           exit 1
  stale writer                 -> cannot answer           exit 1

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
            json.dump({"v": 2, "session": "w", "pid": pid, "at": at}, f)

    def doctor(self):
        env = dict(os.environ, TORAGENTS_HOME=self.home, TORAGENTS_STATE=self.state)
        p = subprocess.run([sys.executable, AGENT, "doctor"], capture_output=True,
                           text=True, env=env, timeout=180)
        return p.returncode, p.stdout + p.stderr

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
        self.assertNotEqual(rc, 0, "a detector that cannot answer must not exit 0")

    def test_stale_writer_cannot_answer(self):
        self.writer(fresh=False)
        rc, out = self.doctor()
        line = self.window_line(out)
        self.assertIn("cannot answer", line, line)
        self.assertIn("stale", line, line)
        self.assertNotEqual(rc, 0)


if __name__ == "__main__":
    unittest.main(verbosity=2)
