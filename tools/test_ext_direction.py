#!/usr/bin/env python3
"""The mismatch verdict is one bit; the sentence is what a human acts on.

Reported live: doctor printed "running a module from before ext 3" for windows running
ext 5 against a reader that expected 3. Every word of that is false in the direction
that matters -- those windows were AFTER the reader, and /reload could not have fixed
anything. The verdict was right; the remedy named the half that was already correct.

The direction that gets it wrong is the COMMON one, not the exotic one: we edit the repo
and reload windows long before anyone restarts a daemon that has been up for hours. So
a message that assumes "window is behind" guarantees the error persists for exactly the
case that produces it most.
"""
import json
import os
import subprocess
import sys
import tempfile
import time
import unittest

HERE = os.path.dirname(os.path.abspath(__file__))
ROOT = os.path.dirname(HERE)
AGENT = os.path.join(ROOT, "inbox_agent.py")
sys.path.insert(0, ROOT)
from inbox_agent import _mismatch_sentence  # noqa: E402


class MismatchSentence(unittest.TestCase):
    def test_window_behind_names_reload(self):
        why = _mismatch_sentence("3", "5")
        self.assertIn("/reload this window", why)
        self.assertNotIn("READER is behind", why)

    def test_reader_behind_names_the_reader_and_never_reload(self):
        why = _mismatch_sentence("5", "3")
        self.assertIn("READER is behind", why)
        self.assertIn("/reload cannot", why.replace("reloading the window cannot", "/reload cannot")
                      if "reloading the window cannot" in why else why)
        # The defect verbatim: this sentence must never claim the window is old.
        self.assertNotIn("from before ext 3", why)
        self.assertNotIn("running a module from before", why)

    def test_direction_is_decided_by_comparison_not_by_inequality(self):
        # Both sides of the same inequality, same inputs otherwise. If the code ever
        # stops comparing, these two sentences collapse into one and this fails.
        older = _mismatch_sentence("3", "5")
        newer = _mismatch_sentence("5", "3")
        self.assertNotEqual(older, newer)

    def test_unparseable_build_is_not_dressed_up_as_a_direction(self):
        why = _mismatch_sentence("banana", "5")
        self.assertIn("cannot tell which side is behind", why)
        self.assertNotIn("/reload", why)
        self.assertNotIn("READER is behind", why)

    def test_doctor_on_this_box_never_asserts_before_for_a_newer_window(self):
        """End to end: doctor's own output carries the direction, not just the helper."""
        home = tempfile.mkdtemp(prefix="dir-")
        state = os.path.join(home, "state")
        live = os.path.join(state, "live")
        os.makedirs(live)
        now = time.time() * 1000.0
        # A live window that is NEWER than the reader we run (5), with a fresh writer.
        with open(os.path.join(live, "pi-newer.json"), "w") as f:
            json.dump({"session": "pi-newer", "pid": os.getpid(),
                       "at": now, "code": "6"}, f)
        # A reader that expects 5: patch the module constant by running a wrapper that
        # sets it after import, so we test the real code path rather than a copy.
        wrapper = os.path.join(home, "run.py")
        with open(wrapper, "w") as f:
            f.write(
                f"import sys; sys.path.insert(0, {ROOT!r})\n"
                "import inbox_agent as ia\n"
                "ia.EXPECTED_EXT_VERSION = '5'\n"
                f"ia.STATE = {state!r}\n"
                "ia.HOME = {!r}\n"
                "ia.EXPECTED_EXT_VERSION = '5'\n".format(home) +
                "sys.argv = ['inbox_agent.py', 'doctor']\n"
                "sys.exit(ia.main())\n")
        env = dict(os.environ, TORAGENTS_HOME=home, TORAGENTS_STATE=state)
        out = subprocess.run([sys.executable, wrapper], capture_output=True, text=True,
                             env=env, timeout=120)
        text = out.stdout + out.stderr
        self.assertIn("ext=6", text, text)
        self.assertIn("READER is behind", text, text)
        self.assertNotIn("running a module from before ext 5", text,
                         "doctor told a newer window it was old:\n" + text)


if __name__ == "__main__":
    unittest.main(verbosity=2)
