#!/usr/bin/env python3
"""Tests for tools/measure_publish.py -- the classifier, not the weather.

These are tests of a MEASUREMENT, so the dangerous failure is not an exception:
it is a wrong label. A probe that reports `descriptor-not-found` when the truth
is "your socks port is dead" produces a false story about a healthy peer, and
a probe that reports `connected+nonce` because it never checked anything is
worse than no tool at all.

So every test here builds a fake SOCKS5 server whose behaviour is scripted and
asserts the exact label that behaviour must produce. Two of them fail if the
classifier is merely "not crashing":

  * dead socks port  -> must NOT be connected+nonce, and must NOT be blamed on
                        the peer (descriptor-not-found). It is `other`, and the
                        detail has to say the socks port is unusable.
  * connect-after-OK -> must be connected-no-nonce, not connect-timeout: the
                        distinction is "listener wedged" vs "tor can't find it".

Run:  python3 tools/test_measure.py
"""
import csv
import json
import os
import socket
import sys
import tempfile
import threading
import time
import unittest

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
import measure_publish as M          # noqa: E402

ONION = "absnlmanroilpjiakx7q4eqmo6ufh722b5n2a4ib6z3457hegfdijkyd.onion"


def _recv(conn, n):
    buf = b""
    while len(buf) < n:
        c = conn.recv(n - len(buf))
        if not c:
            return None
        buf += c
    return buf


class FakeSocks(threading.Thread):
    """One-connection SOCKS5 server. `script` decides how it misbehaves."""

    def __init__(self, script="ok_nonce", nonce="AAECAwQFBgc=", hold=0.25):
        super().__init__(daemon=True)
        self.script, self.nonce, self.hold = script, nonce, hold
        self.srv = socket.socket()
        self.srv.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
        self.srv.bind(("127.0.0.1", 0))
        self.srv.listen(4)
        self.port = self.srv.getsockname()[1]
        self.requests = 0
        self.requested_host = None

    def run(self):
        try:
            conn, _ = self.srv.accept()
        except OSError:
            return
        with conn:
            try:
                self._handle(conn)
            except OSError:
                pass
        try:
            self.srv.close()
        except OSError:
            pass

    def _handle(self, conn):
        if self.script == "silent_greeting":
            time.sleep(self.hold)              # never answer the greeting
            return
        greeting = _recv(conn, 2)
        if greeting is None:
            return
        # version, nmethods, then nmethods method bytes: reading only 2 here
        # leaves the method byte in the buffer and desynchronises every later
        # read (this fixture bug looked exactly like a wedged peer).
        if _recv(conn, greeting[1]) is None:
            return
        conn.sendall(b"\x05\x00")
        head = _recv(conn, 4)
        if head is None:
            return
        if head[3] == 0x03:                    # DOMAIN + port; record what we were asked for
            n = _recv(conn, 1)[0]
            self.requested_host = _recv(conn, n).decode("ascii", "replace")   # onion names are base32, never idna
            _recv(conn, 2)
        elif head[3] == 0x01:
            _recv(conn, 6)

        if self.script == "rep04":
            conn.sendall(b"\x05\x04\x00\x01" + b"\x00" * 6)
            return
        if self.script == "rep05":
            conn.sendall(b"\x05\x05\x00\x01" + b"\x00" * 6)
            return

        conn.sendall(b"\x05\x00\x00\x01\x7f\x00\x00\x01\x1f\x90")   # success, bound

        if self.script == "connect_then_silent":
            time.sleep(self.hold)              # connected, then never speak
            return

        n = _recv(conn, 4)
        if n is None:
            return
        _recv(conn, int.from_bytes(n, "big"))
        self.requests += 1
        if self.script == "empty_nonce":
            body = json.dumps({"ok": True, "nonce": ""}).encode()
        elif self.script == "refused_hello":
            body = json.dumps({"ok": False, "error": "no"}).encode()
        else:
            body = json.dumps({"ok": True, "nonce": self.nonce}).encode()
        conn.sendall(len(body).to_bytes(4, "big") + body)
        time.sleep(0.05)


def free_port():
    """A port nothing is listening on (bound then released)."""
    s = socket.socket()
    s.bind(("127.0.0.1", 0))
    p = s.getsockname()[1]
    s.close()
    return p


class TestResultVocabulary(unittest.TestCase):
    def test_exactly_the_five_documented_labels(self):
        self.assertEqual(M.RESULTS, ("connected+nonce", "descriptor-not-found",
                                     "connect-timeout", "connected-no-nonce", "other"))

    def test_no_label_is_a_substring_trap(self):
        # 'connected+nonce' vs 'connected-no-nonce' differ by punctuation only;
        # a startswith() classifier would merge them. Keep them distinct.
        self.assertFalse(M.CONNECTED_WITH_NONCE == M.CONNECTED_NO_NONCE)


class TestClassification(unittest.TestCase):
    def _run(self, script, **kw):
        srv = FakeSocks(script, **kw)
        srv.start()
        try:
            return M.probe(ONION, srv.port, connect_timeout=0.6, hello_timeout=0.4)
        finally:
            srv.join(timeout=3)

    def test_success_is_connected_plus_nonce(self):
        result, detail = self._run("ok_nonce", nonce="AAECAwQFBgcICQ==")
        self.assertEqual(result, M.CONNECTED_WITH_NONCE, detail)
        self.assertIn("nonce=", detail)

    def test_socks_request_carries_the_full_onion_hostname(self):
        """The bug this test exists for: sending the bare 56-char base32.

        Tor resolves onion addresses by full hostname. Strip the ".onion" and tor
        treats the name as clearnet, attempts DNS, and answers 0x04
        host-unreachable -- so every peer, including a healthy one, classifies as
        descriptor-not-found. A measurement bug that looks exactly like a network
        fact, which is the worst kind.
        """
        srv = FakeSocks("ok_nonce")
        srv.start()
        try:
            M.probe(ONION, srv.port, connect_timeout=1.0, hello_timeout=1.0)
        finally:
            srv.join(timeout=3)
        self.assertIsNotNone(srv.requested_host, "no CONNECT request reached the fake socks server")
        self.assertTrue(srv.requested_host.endswith(".onion"),
                        f"tor was asked for {srv.requested_host!r}: a bare base32 host is clearnet")
        self.assertEqual(srv.requested_host, ONION)

    def test_rep_04_is_descriptor_not_found_not_other(self):
        result, detail = self._run("rep04")
        self.assertEqual(result, M.DESCRIPTOR_NOT_FOUND, detail)
        self.assertIn("0x04", detail)

    def test_rep_05_is_other_and_names_refused(self):
        # descriptor WAS found (tor answered) but the listener refused:
        # this must not be reported as the publish window.
        result, detail = self._run("rep05")
        self.assertEqual(result, M.OTHER, detail)
        self.assertIn("connection refused", detail)

    def test_no_socks_reply_is_connect_timeout(self):
        result, detail = self._run("silent_greeting", hold=1.2)
        self.assertEqual(result, M.CONNECT_TIMEOUT, detail)

    def test_connect_then_silence_is_connected_no_nonce(self):
        # The listener accepted the TCP connection, so this is NOT a timeout and
        # NOT a missing descriptor: someone is home and not answering.
        result, detail = self._run("connect_then_silent", hold=1.2)
        self.assertEqual(result, M.CONNECTED_NO_NONCE, detail)
        self.assertIn("silence", detail)

    def test_empty_nonce_is_connected_no_nonce(self):
        result, detail = self._run("empty_nonce")
        self.assertEqual(result, M.CONNECTED_NO_NONCE, detail)

    def test_refused_hello_is_connected_no_nonce(self):
        result, detail = self._run("refused_hello")
        self.assertEqual(result, M.CONNECTED_NO_NONCE, detail)

    def test_dead_socks_port_is_classified_and_never_blames_the_peer(self):
        """The test that must fail if the classifier is optimistic.

        Nothing is listening on the socks port. The correct answer is `other`
        with a detail that points at the measurement. Any answer of
        connected+nonce, or any answer of descriptor-not-found (which would read
        as "the peer is unpublished"), is a false story.
        """
        port = free_port()
        result, detail = M.probe(ONION, port, connect_timeout=0.5, hello_timeout=0.2)
        self.assertIn(result, M.RESULTS)
        self.assertNotEqual(result, M.CONNECTED_WITH_NONCE, f"reported success with no server: {detail}")
        self.assertNotEqual(result, M.DESCRIPTOR_NOT_FOUND,
                            f"blamed the peer for our dead socks port: {detail}")
        self.assertEqual(result, M.OTHER, detail)
        self.assertIn("socks", detail.lower())
        self.assertIn("unusable", detail.lower())

    def test_probe_always_returns_a_known_label(self):
        for script in ("ok_nonce", "rep04", "rep05", "connect_then_silent", "empty_nonce"):
            with self.subTest(script=script):
                result, detail = self._run(script)
                self.assertIn(result, M.RESULTS)
                self.assertTrue(detail)


class TestCsvOutput(unittest.TestCase):
    def test_rows_are_durable_complete_and_ordered(self):
        seq = [(M.DESCRIPTOR_NOT_FOUND, "socks-rep=0x04"),
               (M.DESCRIPTOR_NOT_FOUND, "socks-rep=0x04"),
               (M.CONNECTED_WITH_NONCE, "nonce=AAAA... (12 chars)"),
               (M.OTHER, "socks-rep=0x05 connection refused")]
        it = iter(seq)

        def fake_probe():
            try:
                return next(it)
            except StopIteration:
                return (M.CONNECTED_WITH_NONCE, "nonce=BBBB... (12 chars)")

        with tempfile.TemporaryDirectory() as d:
            out = os.path.join(d, "m.csv")
            counts, first_ok, last_ok = M.measure(ONION, out, seconds=0.45, interval=0.05,
                                                  probe_fn=fake_probe, log=lambda *a: None)
            with open(out, newline="", encoding="utf-8") as f:
                rows = list(csv.reader(f))
        self.assertEqual(rows[0], ["t_rel", "result", "detail"], "header must be present")
        body = rows[1:]
        self.assertGreaterEqual(len(body), 4, f"too few samples: {len(body)}")
        for _t, result, detail in body:
            self.assertIn(result, M.RESULTS, detail)
        times = [float(r[0]) for r in body]
        self.assertEqual(times, sorted(times), "t_rel must be monotonic")
        self.assertIsNotNone(first_ok, "a connected+nonce sample existed but first_ok is None")
        self.assertGreaterEqual(counts[M.CONNECTED_WITH_NONCE], 1)

    def test_a_never_reachable_window_says_so(self):
        with tempfile.TemporaryDirectory() as d:
            out = os.path.join(d, "m.csv")
            counts, first_ok, _ = M.measure(
                ONION, out, seconds=0.25, interval=0.05,
                probe_fn=lambda: (M.DESCRIPTOR_NOT_FOUND, "socks-rep=0x04"),
                log=lambda *a: None)
            self.assertIsNone(first_ok, "must not claim a success it never saw")
            self.assertEqual(counts[M.DESCRIPTOR_NOT_FOUND], counts.get(M.DESCRIPTOR_NOT_FOUND))
            self.assertEqual(counts[M.CONNECTED_WITH_NONCE], 0)


class TestTargetResolution(unittest.TestCase):
    def test_raw_onion_passes_through(self):
        label, onion = M.resolve_targets(ONION, "/nonexistent")
        self.assertIsNone(label)
        self.assertEqual(onion, ONION)

    def test_label_lookup_uses_peers_json(self):
        with tempfile.TemporaryDirectory() as d:
            with open(os.path.join(d, "peers.json"), "w", encoding="utf-8") as f:
                json.dump({"abcdef0123456789": {"label": "kisu", "onion": ONION}}, f)
            label, onion = M.resolve_targets("kisu", d)
        self.assertEqual((label, onion), ("kisu", ONION))

    def test_unknown_label_exits_with_the_known_ones(self):
        with tempfile.TemporaryDirectory() as d:
            with open(os.path.join(d, "peers.json"), "w", encoding="utf-8") as f:
                json.dump({"abcdef0123456789": {"label": "kisu", "onion": ONION}}, f)
            with self.assertRaises(SystemExit) as cm:
                M.resolve_targets("nobody", d)
        self.assertIn("kisu", str(cm.exception))

    def test_socks_precedence_env_over_config(self):
        with tempfile.TemporaryDirectory() as d:
            with open(os.path.join(d, "config.json"), "w", encoding="utf-8") as f:
                json.dump({"socks_port": 9052}, f)
            old = os.environ.pop("TORAGENTS_SOCKS", None)
            try:
                self.assertEqual(M.resolve_socks(None, d), 9052, "config must beat the 9050 default")
                os.environ["TORAGENTS_SOCKS"] = "9101"
                self.assertEqual(M.resolve_socks(None, d), 9101, "env must beat config")
                self.assertEqual(M.resolve_socks("9102", d), 9102, "explicit must beat env")
            finally:
                os.environ.pop("TORAGENTS_SOCKS", None)
                if old is not None:
                    os.environ["TORAGENTS_SOCKS"] = old


if __name__ == "__main__":
    unittest.main(verbosity=2)
