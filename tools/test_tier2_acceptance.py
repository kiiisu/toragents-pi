#!/usr/bin/env python3
"""Tier-2 acceptance tests. Written BEFORE the feature exists, on purpose.

Source of truth: docs/TIER2-DIGEST.md (read it first; section numbers here refer
to it). There is no `dg` implementation in inbox_agent.py or
toragents_watch.ts at the time of writing, so most of this file is EXPECTED TO
FAIL. That is the point: these tests are the specification made falsifiable, and
they are meant to drive the implementation rather than describe it afterwards.

They are written so that a failure is legible. Every missing piece fails with

    NOT IMPLEMENTED: <what> -- see TIER2-DIGEST.md <section>

and not with an ImportError or an AttributeError from three frames deep. A test
that fails because the module will not import proves nothing; a test that fails
because the contract is absent proves the contract is absent.

Two kinds of test live here, and the difference matters when reading the output:

  * CONTRACT tests fail today and must pass when tier 2 lands.
  * COMPAT tests pass today *by design* -- they pin the behaviour of the old
    code that tier 2 is not allowed to break (§5: old receivers must accept a
    digest-bearing send, old receivers must refuse an unknown op with a specific
    sentence). If one of these ever starts failing, someone has broken the
    pre-tier-2 wire and every deployed peer stops talking.

Run:  python3 tools/test_tier2_acceptance.py            # all of it
      python3 tools/test_tier2_acceptance.py -v         # per-test names
      python3 tools/test_tier2_acceptance.py Compat     # only the compat pins
"""
import json
import os
import socket
import subprocess
import sys
import tempfile
import time
import unittest

HERE = os.path.dirname(os.path.abspath(__file__))
ROOT = os.path.dirname(HERE)
AGENT = os.path.join(ROOT, "inbox_agent.py")
sys.path.insert(0, ROOT)

import inbox_agent  # noqa: E402  (must import: a missing IMPORT is not a finding)
import wire  # noqa: E402

PGM = "python3"
H = "a" * 32                      # a well-formed 128-bit hash, per §1
UNMINTED = "9f2c1a4b7e01"         # 12 hex chars: a plausible id nobody minted


# --------------------------------------------------------------- small tools --
def free_port():
    s = socket.socket()
    s.bind(("127.0.0.1", 0))
    port = s.getsockname()[1]
    s.close()
    return port


def need_api(obj, name, section):
    """Fetch a function tier 2 is supposed to introduce, or fail for THAT reason."""
    fn = getattr(obj, name, None)
    if fn is None:
        raise AssertionError(
            f"NOT IMPLEMENTED: {name}() does not exist on {getattr(obj, '__name__', obj)} "
            f"-- see TIER2-DIGEST.md {section}")
    return fn


def need_cli(*args, section):
    """Run an `inbox_agent.py <args>` subcommand that the spec requires to exist."""
    proc = subprocess.run([sys.executable, AGENT, *args], capture_output=True, text=True, timeout=60)
    if proc.returncode != 0 and "invalid choice" in (proc.stderr or ""):
        raise AssertionError(
            f"NOT IMPLEMENTED: `inbox_agent.py {' '.join(args)}` does not exist yet "
            f"-- see TIER2-DIGEST.md {section}")
    return proc


def dg(items_a=(), items_b=(), **over):
    """A digest object shaped exactly as §2 says. Overrides are for bad-input tests."""
    out = {"v": 1, "gen": time.time(), "a": list(items_a), "b": list(items_b)}
    out.update(over)
    return out


def item(id_=UNMINTED, h=H, n=12):
    return {"id": id_, "h": h, "n": n}


def canon(d):
    return json.dumps(d, sort_keys=True, separators=(",", ":")).encode()


def dsig_for(keys, nonce_b, d):
    return wire.b64e(keys.sign(nonce_b + b"toragents-dg1" + canon(d)))


# ------------------------------------------------------------------- framing --
def send_frame(sock, obj):
    body = json.dumps(obj).encode()
    sock.sendall(len(body).to_bytes(4, "big") + body)


def recv_frame(sock):
    def exact(n):
        buf = b""
        while len(buf) < n:
            chunk = sock.recv(n - len(buf))
            if not chunk:
                return None
            buf += chunk
        return buf

    head = exact(4)
    if head is None:
        return None
    body = exact(int.from_bytes(head, "big"))
    return json.loads(body.decode()) if body else None


class Node:
    """One `inbox_agent.py` home: its own state, identity, port, serve process."""

    def __init__(self, label):
        self.tmp = tempfile.TemporaryDirectory()
        self.home = self.tmp.name
        self.state = os.path.join(self.home, "state")
        self.label = label
        os.makedirs(self.state, exist_ok=True)
        self.port = free_port()
        with open(os.path.join(self.state, "config.json"), "w") as f:
            json.dump({"local_port": self.port}, f)
        self.env = dict(os.environ, TORAGENTS_HOME=self.home, TORAGENTS_STATE=self.state)
        self.env.pop("TORAGENTS_SOCKS", None)
        self._cli("init")
        self.proc = None

    def _cli(self, *args, timeout=60):
        return subprocess.run([sys.executable, AGENT, *args], capture_output=True,
                              text=True, env=self.env, timeout=timeout)

    @property
    def keys(self):
        with open(os.path.join(self.state, "identity.json")) as f:
            return wire.AgentKeys.from_secret_dict(json.load(f))

    @property
    def fingerprint(self):
        return self.keys.public().fingerprint

    @property
    def onion(self):
        return self.fingerprint.replace("0", "a").ljust(56, "b")[:56] + ".onion"

    def allow(self, peer):
        """Allowlist `peer` the way an operator would, through the CLI."""
        proc = self._cli("add-peer", peer.fingerprint, "--onion", peer.onion, "--label", peer.label)
        assert proc.returncode == 0, proc.stderr

    def start(self):
        self.proc = subprocess.Popen([sys.executable, AGENT, "serve"], env=self.env,
                                     stdout=subprocess.PIPE, stderr=subprocess.STDOUT, text=True)
        deadline = time.time() + 15
        while time.time() < deadline:
            try:
                with socket.create_connection(("127.0.0.1", self.port), timeout=0.3):
                    return self
            except OSError:
                time.sleep(0.05)
        raise AssertionError(f"receiver {self.label} never listened on {self.port}")

    def stop(self):
        if self.proc and self.proc.poll() is None:
            self.proc.terminate()
            try:
                self.proc.wait(timeout=5)
            except subprocess.TimeoutExpired:
                self.proc.kill()
        self.tmp.cleanup()

    # -- wire ---------------------------------------------------------------
    def hello(self):
        with socket.create_connection(("127.0.0.1", self.port), timeout=10) as s:
            send_frame(s, {"op": "hello"})
            reply = recv_frame(s)
        assert reply and reply.get("ok"), reply
        return reply["nonce"]

    def raw_op(self, payload):
        with socket.create_connection(("127.0.0.1", self.port), timeout=10) as s:
            send_frame(s, payload)
            return recv_frame(s)

    def send_with(self, sender, body, digest=None, corrupt_dsig=False, session="t"):
        """A real `send`, optionally carrying a digest exactly as §2 specifies."""
        nonce_b64 = self.hello()
        payload = {"op": "send", "nonce": nonce_b64, "body": body,
                   "pubkey": sender.keys.public().to_dict(),
                   "sig": wire.b64e(sender.keys.sign(wire.b64d(nonce_b64) + body.encode())),
                   "from_session": session}
        if digest is not None:
            sig = dsig_for(sender.keys, wire.b64d(nonce_b64), digest)
            if corrupt_dsig:
                sig = wire.b64e(b"\x00" * len(wire.b64d(sig)))
            payload["dg"] = digest
            payload["dsig"] = sig
        return self.raw_op(payload)

    def digest_op(self, sender, digest):
        nonce_b64 = self.hello()
        return self.raw_op({"op": "digest", "v": 1, "nonce": nonce_b64,
                            "pubkey": sender.keys.public().to_dict(),
                            "dsig": dsig_for(sender.keys, wire.b64d(nonce_b64), digest),
                            "dg": digest})


class Pair(unittest.TestCase):
    """Base: a real receiver (B) that has allowlisted a real sender (A)."""

    def setUp(self):
        self.a = Node("sender")
        self.b = Node("receiver")
        self.b.allow(self.a)
        self.b.start()

    def tearDown(self):
        self.b.stop()
        self.a.tmp.cleanup()

    def inbox_files(self):
        d = os.path.join(self.b.state, "inbox")
        return sorted(os.listdir(d)) if os.path.isdir(d) else []


# ================================================================== COMPAT ===
class Compat(Pair):
    """§5: the old wire must keep working. These pass TODAY by design."""

    def test_1_new_sender_old_receiver_accepts_a_digest_bearing_send(self):
        body = "tier2 compat: unknown keys must be inert"
        reply = self.b.send_with(self.a, body, digest=dg([item()]))
        self.assertTrue(reply and reply.get("ok"), f"a dg-bearing send was refused: {reply}")
        files = self.inbox_files()
        self.assertEqual(len(files), 1, "the body was not stored")
        with open(os.path.join(self.b.state, "inbox", files[0])) as f:
            stored = json.load(f)
        self.assertEqual(stored["body"], body,
                         "the body must be byte-identical; a new key must not touch it")
        self.assertNotIn("dg", stored, "§2: the digest is an index, never stored as message content")

    def test_2_old_receiver_refuses_the_digest_op_with_that_exact_sentence(self):
        reply = self.b.digest_op(self.a, dg([item()]))
        self.assertEqual(reply, {"ok": False, "error": "unknown op 'digest'"},
                         "§5.3: the refusal is a capability answer, so its wording is part of the protocol")

    def test_3_old_receiver_ignores_a_corrupt_dsig_on_a_valid_send(self):
        reply = self.b.send_with(self.a, "still delivered", digest=dg([item()]), corrupt_dsig=True)
        self.assertTrue(reply and reply.get("ok"),
                        f"a bad digest signature must not break the message path (§5): {reply}")
        self.assertEqual(len(self.inbox_files()), 1)

    def test_4_ten_sends_are_ten_dials_and_no_phantom_message(self):
        bodies = [f"piggyback {i}" for i in range(10)]
        for body in bodies:
            self.assertTrue(self.b.send_with(self.a, body, digest=dg([item()]))["ok"])
        files = self.inbox_files()
        self.assertEqual(len(files), 10, "a digest must never arrive as a message of its own")
        for name in files:
            with open(os.path.join(self.b.state, "inbox", name)) as f:
                self.assertNotEqual(json.load(f)["body"], "",
                                    "an empty body would be a phantom message (§5)")


# ============================================================================
class Schema(Pair):
    """§2 bounds. Every one of these must be rejected WITHOUT losing the message."""

    def test_unknown_top_level_field_is_rejected(self):
        validate = need_api(inbox_agent, "dg_validate", "§2 (dg schema)")
        ok, why = self._as_pair(validate(dg([item()], surprise="x")))
        self.assertFalse(ok, "an unknown field in dg must be rejected, not ignored")
        self.assertIn("field", why.lower())

    def test_hash_must_be_32_hex_not_16(self):
        validate = need_api(inbox_agent, "dg_validate", "§1/§2 (h is 128-bit)")
        for bad in ("a" * 16, "a" * 64, "A" * 32, "g" * 32):
            with self.subTest(h=bad[:8] + "…", length=len(bad)):
                ok, why = self._as_pair(validate(dg([item(h=bad)])))
                self.assertFalse(ok, f"h={bad!r} was accepted; §1 says exactly 32 lowercase hex")
                self.assertIn("h", why.lower())

    def test_id_charset_and_length_are_enforced(self):
        validate = need_api(inbox_agent, "dg_validate", "§2 (id bounds)")
        for bad in ("", "x" * 65, "../etc/passwd", "has space", "emoji🙂"):
            with self.subTest(id=bad[:12]):
                ok, _ = self._as_pair(validate(dg([item(id_=bad)])))
                self.assertFalse(ok, f"id={bad!r} must be refused (1..64 of [0-9a-zA-Z_-])")

    def test_n_is_bounded(self):
        validate = need_api(inbox_agent, "dg_validate", "§2 (n bounds)")
        for bad in (-1, 16 * 1024 * 1024 + 1, "12", 1.5):
            with self.subTest(n=bad):
                ok, _ = self._as_pair(validate(dg([item(n=bad)])))
                self.assertFalse(ok, f"n={bad!r} must be refused (int 0..16 MiB)")

    def test_513_items_drop_the_whole_digest(self):
        validate = need_api(inbox_agent, "dg_validate", "§2 (a+b <= 512)")
        ok, why = self._as_pair(validate(dg([item(id_=f"id{i:04d}") for i in range(513)])))
        self.assertFalse(ok, "513 items must drop the whole dg, not truncate it")
        self.assertIn("512", why.replace("513", ""))

    def test_the_largest_schema_valid_digest_is_the_boundary_worth_testing(self):
        """MEASURED: the 64 KiB cap is unreachable through schema-valid items.

        512 items x (id 64 + h 32 + n 16777216 + punctuation) serialises to
        ~64,553 bytes -- under the 65,536 cap. So the byte ceiling can only fire
        on input the schema would already reject, which makes it defence in
        depth, not a bound a peer can hit. The item count is the bound that
        actually bites, and this pins the real boundary: the largest legal
        digest must be ACCEPTED, so an implementation that drops it has set the
        cap by eye instead of by arithmetic.
        """
        validate = need_api(inbox_agent, "dg_validate", "§2 (serialised <= 64 KiB)")
        biggest = dg([item(id_="x" * 64, n=16777216) for _ in range(512)])
        size = len(canon(biggest))
        self.assertLess(size, 64 * 1024, "premise changed: the cap is now reachable; rewrite this test")
        ok, why = self._as_pair(validate(biggest))
        self.assertTrue(ok, f"a {size}-byte digest of 512 maximal items must be accepted: {why}")

    def test_version_other_than_1_is_refused_with_the_reason(self):
        validate = need_api(inbox_agent, "dg_validate", "§2 (v must be 1)")
        ok, why = self._as_pair(validate(dg([item()], v=2)))
        self.assertFalse(ok)
        self.assertIn("version", why.lower())

    def test_gen_is_clamped_not_rejected(self):
        validate = need_api(inbox_agent, "dg_validate", "§2 (gen advisory)")
        ok, _ = self._as_pair(validate(dg([item()], gen=time.time() + 999999)))
        self.assertTrue(ok, "gen is advisory: a wild clock must not lose the digest")

    @staticmethod
    def _as_pair(result):
        """Accept either (ok, why) or a bare bool from the eventual implementation."""
        if isinstance(result, tuple):
            return result[0], str(result[1] if len(result) > 1 else "")
        return bool(result), ""


class NoBodies(Pair):
    """The peer's hard constraint: index, not bodies. Nothing here may carry text."""

    def test_a_serialised_digest_contains_no_body_text(self):
        build = need_api(inbox_agent, "dg_build", "§appendix (index, not bodies)")
        secret = "THE-BODY-MUST-NOT-MOVE-9f3a"
        digest = build([{"id": UNMINTED, "body": secret}], [], now=time.time())
        blob = canon(digest).decode("utf-8", "replace")
        self.assertNotIn(secret, blob, "a body appeared inside the digest")
        self.assertNotIn("body", blob, "the digest carries a body field at all")
        self.assertEqual(sorted(digest.get("b", [{}])[0].keys()) if digest.get("b") else ["h", "id", "n"],
                         ["h", "id", "n"], "dg items are {id,h,n} and nothing else")


# ============================================================================
class Reconciler(Pair):
    """§4 states, computed from fixtures. Precedence: never-stored > mismatch >
    delivered-both > accepted-only."""

    def reconcile(self, row):
        fn = need_api(inbox_agent, "dg_reconcile_row", "§4 (reconciler)")
        out = fn(row)
        return out[0] if isinstance(out, tuple) else out

    def row(self, **over):
        base = {"id": UNMINTED, "owner": "peer", "at": time.time(), "peer_fp": "f" * 32,
                "n": None, "h": None, "body": False, "peer_at": None,
                "peer_n": None, "peer_h": None, "state": "", "why": "",
                "peer_first": False, "announced": False}
        base.update(over)
        return base

    def test_both_match_is_delivered_both(self):
        self.assertEqual(self.reconcile(self.row(h=H, n=12, peer_h=H, peer_n=12)), "delivered-both")

    def test_hash_divergence_is_mismatch(self):
        self.assertEqual(self.reconcile(self.row(h=H, n=12, peer_h="b" * 32, peer_n=12)), "mismatch")

    def test_length_divergence_alone_is_mismatch(self):
        """The 2000-char trap: same hash is impossible, so n differing must still
        surface as a contradiction rather than being rounded away."""
        self.assertEqual(self.reconcile(self.row(h=H, n=4096, peer_h=H, peer_n=2000)), "mismatch")

    def test_no_attestation_is_accepted_only_never_a_loss(self):
        self.assertEqual(self.reconcile(self.row(h=H, n=12)), "accepted-only",
                         "absence of an attestation is not evidence of loss (§4)")

    def test_claim_without_any_local_anchor_is_never_stored(self):
        self.assertEqual(self.reconcile(self.row(peer_h=H, peer_n=12)), "never-stored")

    def test_never_stored_outranks_mismatch(self):
        """A peer that attests to an id we never held must not be able to make the
        row look like a *near miss* instead of a fabrication."""
        self.assertEqual(self.reconcile(self.row(h=None, peer_h="b" * 32, peer_n=12)),
                         "never-stored")


class Adversarial(Pair):
    """The acceptance criterion: a digest must not be able to claim an id the peer
    never stored. The design must survive a peer that lies."""

    def test_fabricated_entry_is_never_rendered_delivered(self):
        fn = need_api(inbox_agent, "dg_apply", "§Acceptance criterion")
        result = fn(self.b.state, self.a.fingerprint, dg([item(id_=UNMINTED)]))
        stored = result if isinstance(result, dict) else {}
        row = (stored.get("rows") or {}).get(UNMINTED, {})
        self.assertNotEqual(row.get("state"), "delivered-both",
                            "a fabricated entry rendered as delivered -- the one thing tier 2 must never do")
        self.assertEqual(row.get("state"), "never-stored",
                         "§4: an id we never minted and never received is never-stored")
        self.assertFalse(row.get("announced"), "a fabrication must not influence announced")

    def test_id_reuse_is_a_contradiction_not_an_overwrite(self):
        apply_ = need_api(inbox_agent, "dg_apply", "§Threat model (id reuse)")
        apply_(self.b.state, self.a.fingerprint, dg([item(id_=UNMINTED, h=H, n=12)]))
        second = apply_(self.b.state, self.a.fingerprint, dg([item(id_=UNMINTED, h="b" * 32, n=12)]))
        row = ((second if isinstance(second, dict) else {}).get("rows") or {}).get(UNMINTED, {})
        self.assertEqual(row.get("state"), "mismatch")
        self.assertIn("id-reuse", (row.get("why") or ""), "the reason must name the reuse")
        self.assertNotEqual(row.get("h"), "b" * 32, "the first hash must never be overwritten")

    def test_a_digest_frame_cannot_be_replayed(self):
        nonce_b64 = self.b.hello()
        payload = {"op": "digest", "v": 1, "nonce": nonce_b64,
                   "pubkey": self.a.keys.public().to_dict(),
                   "dsig": dsig_for(self.a.keys, wire.b64d(nonce_b64), dg([item()])),
                   "dg": dg([item()])}
        first = self.b.raw_op(payload)
        seen = os.path.join(self.b.state, "seen.json")
        before = open(seen, "rb").read() if os.path.exists(seen) else b""
        replay = self.b.raw_op(payload)
        after = open(seen, "rb").read() if os.path.exists(seen) else b""
        self.assertFalse(first.get("ok"), "an unimplemented digest op must refuse, not accept")
        self.assertFalse(replay.get("ok"), "a replayed frame was accepted")
        self.assertEqual(before, after, "a replay must not mutate the row store")


# ============================================================================
class Surface(unittest.TestCase):
    """§7: the panel is part of the contract, and it must never throw."""

    def test_dg_show_exists_and_does_not_throw_on_a_corrupt_store(self):
        with tempfile.TemporaryDirectory() as tmp:
            state = os.path.join(tmp, "state")
            os.makedirs(state)
            with open(os.path.join(state, "seen.json"), "w") as f:
                f.write('{"v":1,"peers":{"deadbeef":{"rows":{')       # truncated mid-object
            env = dict(os.environ, TORAGENTS_STATE=state, TORAGENTS_HOME=tmp)
            proc = subprocess.run([sys.executable, AGENT, "dg", "show"], capture_output=True,
                                  text=True, env=env, timeout=60)
            self.assertNotIn("invalid choice", proc.stderr,
                             "NOT IMPLEMENTED: `inbox_agent.py dg show` -- see TIER2-DIGEST.md §7")
            self.assertEqual(proc.returncode, 0,
                             f"§7: nothing here may throw; delivery keeps working. stderr={proc.stderr[:200]}")
            self.assertIn("unavailable", (proc.stdout + proc.stderr).lower(),
                          "§7: a corrupt store renders the unavailable line")


if __name__ == "__main__":
    unittest.main(verbosity=2)
