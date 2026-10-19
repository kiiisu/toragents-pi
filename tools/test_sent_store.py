#!/usr/bin/env python3
"""Full-outbound store: the prerequisite tier 2 rests on.

The claim under test: each side holds its own outbound WHOLE, so a digest can
honestly attest to storage. Before this, chat.jsonl kept a display copy truncated
at 2000 chars and the only complete copy of what we sent lived in the PEER's
inbox -- a transcript split down the middle.

Every test here must be able to fail. The two that matter most:
  - a 5000-char body survives byte-identical (the 2000-char truncation cannot
    quietly come back),
    A number that travelled without its subject: commit 7c3925c's message says
    "the 3387-byte message stored byte-identical", measured live over Tor. The
    message lived once; it never entered the repo, so `grep -r 3387` finds
    nothing and the number cannot be checked by anyone reading this file. It is
    kept only as a signpost WITH its address (verified live, 3387 bytes,
    7c3925c); the evidence is the 5000-char test below, not the story.
    Rule: a number without its subject is prose wearing a measurement's
    clothes.
  - a send whose reply was lost stays visible as `failed`/`pending`, because
    recording only successes loses exactly the messages worth knowing about.
"""
import json
import os
import shutil
import subprocess
import sys
import tempfile
import unittest

HERE = os.path.dirname(os.path.abspath(__file__))
REPO = os.path.dirname(HERE)
AGENT = os.path.join(REPO, "inbox_agent.py")


class Base(unittest.TestCase):
    def setUp(self):
        self.home = tempfile.mkdtemp(prefix="sentstore-")
        self.env = {**os.environ, "TORAGENTS_HOME": self.home}

    def tearDown(self):
        shutil.rmtree(self.home, ignore_errors=True)

    def py(self, code):
        """Run code in-process-ish against the temp state, return (rc, out, err)."""
        r = subprocess.run([sys.executable, "-c", code], capture_output=True,
                           text=True, env=self.env, cwd=REPO)
        return r.returncode, r.stdout, r.stderr

    def cli(self, *args):
        r = subprocess.run([sys.executable, AGENT, *args], capture_output=True,
                           text=True, env=self.env, cwd=REPO)
        return r.returncode, r.stdout + r.stderr

    def seed(self, body, target='"peer"', reply="default"):
        """Write one record through the real API the send path uses."""
        if reply == "default":                # explicit None means "no answer"
            reply = {"ok": True, "id": "abc123def456"}
        code = (
            "import json,sys;sys.path.insert(0,%r);import inbox_agent as ia;"
            "body=json.loads(sys.stdin.read());"
            "rec=ia.sent_intent('kisu','x.onion',body);"
            "ia.sent_confirm(rec,None if %r is None else json.loads(%r));"
            "print(rec['local_id'])" % (REPO, json.dumps(reply), json.dumps(reply))
        )
        r = subprocess.run([sys.executable, "-c", code], input=json.dumps(body),
                           capture_output=True, text=True, env=self.env, cwd=REPO)
        self.assertEqual(r.returncode, 0, f"seeding failed: {r.stderr}")
        return r.stdout.strip()


class StoreWhole(Base):
    def test_a_5000_char_body_is_stored_whole(self):
        """The whole point. chat.jsonl truncates at 2000; this must not."""
        body = "".join(f"{i:04d} " for i in range(1000))[:5000]
        self.assertEqual(len(body), 5000)
        local = self.seed(body)
        rc, out = self.cli("sent", "show", local)
        self.assertEqual(rc, 0, out)
        rec = json.loads(out)
        self.assertEqual(rec["body"], body, "body was altered or truncated in the store")
        self.assertEqual(rec["len"], 5000)

    def test_a_body_with_multibyte_chars_hashes_over_bytes_not_chars(self):
        """Swedish is not hypothetical here. len(str) != len(bytes) is the trap."""
        body = "kloramin i duschvattnet — å ä ö ♡"
        local = self.seed(body)
        rc, out = self.cli("sent", "verify")
        self.assertEqual(rc, 0, f"verify rejected a correct multibyte record: {out}")
        self.assertIn('"hash_mismatch": []', out)
        rc, out = self.cli("sent", "show", local)
        self.assertEqual(json.loads(out)["len"], len(body.encode("utf-8")))

    def test_two_sends_to_different_peers_are_two_records(self):
        a = self.seed("first", reply={"ok": True, "id": "id-a"})
        b = self.seed("second", reply={"ok": True, "id": "id-b"})
        self.assertNotEqual(a, b)
        rc, out = self.cli("sent", "list")
        self.assertEqual(rc, 0, out)
        self.assertIn("id-a", out)
        self.assertIn("id-b", out)


class FailuresStayVisible(Base):
    def test_a_refused_send_is_recorded_as_refused_not_dropped(self):
        self.seed("never arrived", reply={"ok": False, "error": "hello refused"})
        rc, out = self.cli("sent", "verify")
        self.assertEqual(rc, 0, out)          # a recorded refusal is not corruption
        self.assertNotIn('"refused": []', out, "a refused send vanished from the store")

    def test_silence_is_unknown_never_refused_never_failed(self):
        """The peer did not answer. That is not knowledge, and must not be filed as
        one. Kisu's crash window: bytes may be with the peer while we hold only an
        unanswered intent -- calling that "failed" asserts something we cannot know."""
        self.seed("no reply at all", reply=None)
        rc, out = self.cli("sent", "verify")
        self.assertEqual(rc, 0, out)
        self.assertNotIn('"unknown": []', out, "silence was not recorded as unknown")
        self.assertIn('"refused": []', out, "silence was misfiled as a refusal")
        self.assertIn('"pending": []', out, "silence was left pending after a verdict")

    def test_a_confirmed_send_is_not_filed_under_unknown_or_refused(self):
        self.seed("delivered fine", reply={"ok": True, "id": "ok123"})
        rc, out = self.cli("sent", "verify")
        self.assertEqual(rc, 0, out)
        self.assertIn('"unknown": []', out)
        self.assertIn('"refused": []', out)

    def test_an_intent_without_a_reply_surfaces_as_pending(self):
        """A send whose reply was lost still went out. Silence must announce itself."""
        code = ("import sys;sys.path.insert(0,%r);import inbox_agent as ia;"
                "rec=ia.sent_intent('kisu','x.onion','half-sent');print(rec['local_id'])"
                % REPO)
        rc, out, err = self.py(code)
        self.assertEqual(rc, 0, err)
        rc, out = self.cli("sent", "verify")
        self.assertEqual(rc, 0, out)
        self.assertNotIn('"pending": []', out,
                         "an unconfirmed send was reported as if nothing happened")

    def test_a_corrupt_store_is_reported_and_does_not_throw(self):
        self.seed("good one")
        sent_dir = os.path.join(self.home, "state", "sent")
        with open(os.path.join(sent_dir, "broken.json"), "w") as f:
            f.write("{not json")
        rc, out = self.cli("sent", "verify")
        self.assertEqual(rc, 1, "corruption must fail verify")
        self.assertIn("broken.json", out)
        self.assertNotIn("Traceback", out)

    def test_a_tampered_body_is_caught(self):
        local = self.seed("original text")
        sent_dir = os.path.join(self.home, "state", "sent")
        path = os.path.join(sent_dir, local + ".json")
        rec = json.load(open(path))
        rec["body"] = "tampered text"
        json.dump(rec, open(path, "w"))
        rc, out = self.cli("sent", "verify")
        self.assertEqual(rc, 1, "a body that no longer matches its hash passed verify")
        self.assertIn(local, out)


class EmptyStore(Base):
    def test_an_empty_store_says_nothing_stored_rather_than_zero_health(self):
        rc, out = self.cli("sent", "list")
        self.assertEqual(rc, 0, out)
        self.assertIn("nothing stored yet", out)


if __name__ == "__main__":
    unittest.main(verbosity=2)
