#!/usr/bin/env python3
"""measure_publish.py -- how long is a peer's onion actually unreachable?

The number this project keeps mis-deriving by hand ("restart the peer, wait,
try again, is it dead?") is a curve, not a boolean: after a peer's tor starts,
the onion has NO DESCRIPTOR for a while, and during that window every dial
fails in a way that is indistinguishable from the peer being switched off.
This tool measures that window instead of guessing at it.

It speaks the REAL protocol, because half the interesting failure modes live
above the socket:

    open TCP to 127.0.0.1:<socks>
    SOCKS5 greeting  ->  0x05 0x00
    SOCKS5 CONNECT   ->  REP byte, which is the whole point (see below)
    (if connected) send {"op":"hello"} framed with a 4-byte BE length prefix
    (if connected) expect {"ok":true,"nonce":"<b64>"}

The SOCKS5 reply code is read BY HAND rather than through a library, because
`PySocks` folds every failure into one exception and we need the difference:

    0x04 host-unreachable  -> tor has no descriptor to route to: DESCRIPTOR NOT
                              FOUND. This is the publish window. The peer is
                              fine; the network simply cannot find out how to
                              reach it yet.
    0x05 connection refused -> descriptor found, peer's onion service answered
                              with a refusal. The peer's tor is up and its
                              listener is NOT. A different bug entirely, and
                              "wait three minutes" is the wrong advice for it.
    0x00 then silence      -> connected, hello sent, no answer: the listener
                              accepted but is wedged. Also not the publish window.

Results are written as CSV, one row per attempt, flushed and fsync'd per row,
so a measurement killed halfway still has every sample it took:

    t_rel,result,detail

`result` is one of exactly five strings:

    connected+nonce       the peer answered hello with a fresh nonce
    descriptor-not-found  SOCKS 0x04: no descriptor to route to
    connect-timeout       no SOCKS reply inside the budget (tor still hunting)
    connected-no-nonce    TCP+SOCKS succeeded, hello got silence or garbage
    other                 anything else, with the raw detail recorded

Usage:

    python3 tools/measure_publish.py --to kisu --seconds 240 --out /tmp/x.csv
    python3 tools/measure_publish.py --to kisu --seconds 600 --detach --out /tmp/x.csv

--detach forks and calls setsid(), so killing the terminal, the tool call, or
the parent shell does not kill the measurement. It prints its PID either way;
use that PID to stop it. This exists because a poller that dies with its parent
reports "still unreachable" for a window nobody was actually watching.

The socks port defaults to the same chain the CLI uses (TORAGENTS_SOCKS, then
<state>/config.json socks_port, then 9050) -- NOT hardcoded to 9050, because a
machine running a dedicated tor for this project has its circuits on that
instance only, and measuring through the wrong one reports descriptor-not-found
for a peer that is perfectly reachable.
"""
import argparse
import csv
import json
import os
import socket
import struct
import sys
import time

# Exactly these five, in the order the docstring promises.
CONNECTED_WITH_NONCE = "connected+nonce"
DESCRIPTOR_NOT_FOUND = "descriptor-not-found"
CONNECT_TIMEOUT = "connect-timeout"
CONNECTED_NO_NONCE = "connected-no-nonce"
OTHER = "other"
RESULTS = (CONNECTED_WITH_NONCE, DESCRIPTOR_NOT_FOUND, CONNECT_TIMEOUT,
           CONNECTED_NO_NONCE, OTHER)

MAX_FRAME = 16 * 1024 * 1024          # same ceiling the CLI uses
SOCKS_REP = {
    0x00: "succeeded",
    0x01: "general socks server failure",
    0x02: "connection not allowed by ruleset",
    0x03: "network unreachable",
    0x04: "host unreachable",
    0x05: "connection refused",
    0x06: "ttl expired",
    0x07: "command not supported",
    0x08: "address type not supported",
}


# ------------------------------------------------------------------ framing --
def send_frame(sock, obj):
    body = json.dumps(obj).encode("utf-8")
    sock.sendall(len(body).to_bytes(4, "big") + body)


def _recv_exact(sock, n):
    buf = b""
    while len(buf) < n:
        chunk = sock.recv(n - len(buf))
        if not chunk:
            return None
        buf += chunk
    return buf


def recv_frame(sock):
    hdr = _recv_exact(sock, 4)
    if hdr is None:
        return None
    n = int.from_bytes(hdr, "big")
    if n > MAX_FRAME:
        raise ValueError(f"declared frame {n} exceeds {MAX_FRAME}")
    body = _recv_exact(sock, n)
    if body is None:
        return None
    return json.loads(body.decode("utf-8"))


# -------------------------------------------------------------------- socks --
class SocksError(Exception):
    """Malformed or refused SOCKS5 conversation (the peer never heard about it)."""


def socks5_connect(sock, host, port):
    """Do the SOCKS5 handshake on an already-open socket. Returns the REP byte.

    Raises SocksError for protocol-level trouble. A non-zero REP is NOT raised:
    it is returned, because the caller classifies on it.
    """
    sock.sendall(b"\x05\x01\x00")                 # version 5, one method, no-auth
    greeting = _recv_exact(sock, 2)
    if greeting is None or len(greeting) < 2:
        raise SocksError("socks: connection closed during greeting")
    if greeting[0] != 0x05:
        raise SocksError(f"socks: bad version in greeting: 0x{greeting[0]:02x}")
    if greeting[1] != 0x00:
        raise SocksError(f"socks: auth method 0x{greeting[1]:02x} refused (only 0x00 offered, no auth)")

    hostb = host.encode("idna") if isinstance(host, str) else host
    if len(hostb) > 255:
        raise SocksError(f"socks: hostname too long ({len(hostb)} bytes)")
    sock.sendall(b"\x05\x01\x00\x03" + bytes([len(hostb)]) + hostb
                 + struct.pack(">H", port))
    head = _recv_exact(sock, 4)
    if head is None or len(head) < 4:
        raise SocksError("socks: connection closed during CONNECT reply")
    ver, rep, _rsv, atyp = head
    if ver != 0x05:
        raise SocksError(f"socks: bad version in CONNECT reply: 0x{ver:02x}")
    # Consume BND.ADDR/BND.PORT so the stream is positioned at application data.
    if atyp == 0x01:
        _recv_exact(sock, 4 + 2)
    elif atyp == 0x03:
        n = _recv_exact(sock, 1)
        if n is None:
            raise SocksError("socks: truncated BND.ADDR")
        _recv_exact(sock, n[0] + 2)
    elif atyp == 0x04:
        _recv_exact(sock, 16 + 2)
    else:
        raise SocksError(f"socks: unknown address type 0x{atyp:02x} in CONNECT reply")
    return rep


# --------------------------------------------------------------------- probe --
def probe(onion, socks_port=9050, connect_timeout=10.0, hello_timeout=5.0):
    """One attempt. Returns (result, detail). Never raises for peer-side trouble.

    Local trouble (nothing listening on the socks port) is classified as
    `other` with a detail that says so loudly, because it is a misconfigured
    measurement, not evidence about the peer -- and reporting it as
    descriptor-not-found would be exactly the false negative this tool exists
    to kill.
    """
    # Keep the ".onion" suffix: tor matches onion addresses by their FULL
    # hostname. Sending the bare 56-char base32 makes tor treat it as a clearnet
    # name, do a DNS lookup, and answer 0x04 host-unreachable -- which reads as
    # "the peer is unpublished" for every peer, forever. (This bug shipped once
    # in this very file and was caught only by dialling a known-good onion.)
    host = onion if onion.endswith(".onion") else onion + ".onion"
    t0 = time.monotonic()

    try:
        sock = socket.create_connection(("127.0.0.1", socks_port), timeout=connect_timeout)
    except socket.timeout:
        return CONNECT_TIMEOUT, f"local socks 127.0.0.1:{socks_port} did not accept within {connect_timeout:.1f}s"
    except OSError as exc:
        return OTHER, (f"local socks 127.0.0.1:{socks_port} unusable: "
                       f"{exc.strerror or exc} (fix the measurement, this says nothing about the peer)")

    try:
        sock.settimeout(connect_timeout)
        try:
            rep = socks5_connect(sock, host, 80)
        except socket.timeout:
            return CONNECT_TIMEOUT, (f"no SOCKS reply within {connect_timeout:.1f}s "
                                     f"(tor is still hunting for the descriptor)")
        except SocksError as exc:
            return OTHER, str(exc)
        except OSError as exc:
            return OTHER, f"socket error during SOCKS: {type(exc).__name__}: {exc}"

        dt = time.monotonic() - t0
        if rep == 0x04:
            return DESCRIPTOR_NOT_FOUND, (f"socks-rep=0x04 host-unreachable after {dt:.2f}s "
                                          f"-- tor has no descriptor to route to")
        if rep != 0x00:
            name = SOCKS_REP.get(rep, "unknown")
            return OTHER, f"socks-rep=0x{rep:02x} {name} after {dt:.2f}s"

        # Connected at the SOCKS layer. Now the application layer decides.
        sock.settimeout(hello_timeout)
        try:
            send_frame(sock, {"op": "hello"})
        except OSError as exc:
            return CONNECTED_NO_NONCE, f"connected in {dt:.2f}s, hello write failed: {exc}"

        try:
            hello = recv_frame(sock)
        except socket.timeout:
            return CONNECTED_NO_NONCE, (f"connected in {dt:.2f}s, hello sent, "
                                        f"silence for {hello_timeout:.1f}s")
        except (OSError, ValueError) as exc:
            return CONNECTED_NO_NONCE, f"connected in {dt:.2f}s, then {type(exc).__name__}: {exc}"

        if hello is None:
            return CONNECTED_NO_NONCE, f"connected in {dt:.2f}s, then closed with no reply"

        nonce = hello.get("nonce") if isinstance(hello, dict) else None
        if isinstance(hello, dict) and hello.get("ok") and isinstance(nonce, str) and nonce:
            return CONNECTED_WITH_NONCE, (f"nonce={nonce[:12]}... ({len(nonce)} chars) "
                                          f"handshake done in {dt:.2f}s")
        return CONNECTED_NO_NONCE, f"connected in {dt:.2f}s, unexpected reply: {str(hello)[:120]}"
    finally:
        try:
            sock.close()
        except OSError:
            pass


# ---------------------------------------------------------------- peer lookup --
def resolve_targets(to, state):
    """('label', onion) from peers.json, or (None, onion) when `to` IS an onion."""
    t = (to or "").strip()
    if t.endswith(".onion") or (len(t) == 56 and t.isalnum()):
        return None, t if t.endswith(".onion") else t + ".onion"
    path = os.path.join(state, "peers.json")
    try:
        with open(path, encoding="utf-8") as f:
            peers = json.load(f)
    except FileNotFoundError:
        sys.exit(f"no peers.json at {path} (and {to!r} is not an onion address)")
    except (OSError, ValueError) as exc:
        sys.exit(f"cannot read {path}: {exc}")
    for fp, val in peers.items():
        if t in (fp, val.get("label"), fp[:12]):
            return val.get("label") or t, val["onion"]
    sys.exit(f"unknown peer {to!r}; known labels: "
             + ", ".join(sorted(v.get("label") or k[:12] for k, v in peers.items())))


def resolve_socks(explicit, state):
    """Same precedence the CLI uses, so a measurement through the wrong tor is impossible."""
    if explicit:
        return int(explicit)
    env = os.environ.get("TORAGENTS_SOCKS")
    if env:
        return int(env)
    try:
        with open(os.path.join(state, "config.json"), encoding="utf-8") as f:
            cfg = json.load(f)
        if cfg.get("socks_port"):
            return int(cfg["socks_port"])
    except (OSError, ValueError):
        pass
    return 9050


# -------------------------------------------------------------------- measuring --
def measure(onion, out_path, seconds, interval=2.0, socks_port=9050,
            connect_timeout=10.0, hello_timeout=5.0, probe_fn=None, log=print):
    """Poll until `seconds` have elapsed. CSV is durable after every row."""
    probe_fn = probe_fn or (lambda: probe(onion, socks_port, connect_timeout, hello_timeout))
    started = time.monotonic()
    counts = {r: 0 for r in RESULTS}
    first_ok = last_ok = None
    rows = 0

    with open(out_path, "w", newline="", encoding="utf-8") as fh:
        w = csv.writer(fh)
        w.writerow(["t_rel", "result", "detail"])
        fh.flush()
        os.fsync(fh.fileno())
        while True:
            t = time.monotonic() - started
            if t >= seconds:
                break
            result, detail = probe_fn()
            counts[result] = counts.get(result, 0) + 1
            if result == CONNECTED_WITH_NONCE:
                if first_ok is None:
                    first_ok = t
                last_ok = t
            w.writerow([f"{t:.3f}", result, detail])
            fh.flush()                     # a killed measurement keeps its samples
            try:
                os.fsync(fh.fileno())
            except OSError:
                pass
            rows += 1
            log(f"  t+{t:7.2f}s  {result:<21} {detail}")
            nxt = started + rows * interval
            nap = nxt - time.monotonic()
            if nap > 0:
                time.sleep(nap)

    elapsed = time.monotonic() - started
    log("")
    log(f"  {rows} samples over {elapsed:.1f}s via socks 127.0.0.1:{socks_port}")
    for r in RESULTS:
        if counts[r]:
            log(f"    {r:<22} {counts[r]:>4}")
    if first_ok is None:
        log("  verdict: NEVER reached -- no connected+nonce sample in this window")
    else:
        log(f"  verdict: reachable from t+{first_ok:.1f}s"
            + (f" (last success t+{last_ok:.1f}s)" if last_ok != first_ok else ""))
    log(f"  csv: {out_path}")
    return counts, first_ok, last_ok


def detach(out_path):
    """Fork + setsid so the measurement outlives the shell that started it."""
    sys.stdout.flush()
    sys.stderr.flush()
    pid = os.fork()
    if pid:
        print(f"detached, pid {pid} -- measurement survives this shell")
        print(f"  csv  {out_path}")
        print(f"  log  {out_path}.log")
        print(f"  stop kill {pid}")
        return pid
    os.setsid()                            # new session: no SIGHUP from the old tty
    logf = open(out_path + ".log", "a", encoding="utf-8", buffering=1)
    devnull = open(os.devnull, "rb")
    os.dup2(devnull.fileno(), 0)
    os.dup2(logf.fileno(), 1)
    os.dup2(logf.fileno(), 2)
    return 0


def main(argv=None):
    ap = argparse.ArgumentParser(
        description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--to", required=True,
                    help="peer label from peers.json, or a full .onion address")
    ap.add_argument("--seconds", type=float, default=240.0, help="wall-clock window (default 240)")
    ap.add_argument("--interval", type=float, default=2.0, help="seconds between attempts (default 2)")
    ap.add_argument("--out", default=None, help="CSV path (default /tmp/measure-<peer>-<ts>.csv)")
    ap.add_argument("--socks", default=None, help="socks port (default TORAGENTS_SOCKS > config > 9050)")
    ap.add_argument("--connect-timeout", type=float, default=10.0,
                    help="budget for the TCP+SOCKS phase (default 10; each attempt blocks this long)")
    ap.add_argument("--hello-timeout", type=float, default=5.0,
                    help="budget for the hello reply once connected (default 5)")
    ap.add_argument("--state", default=None, help="state dir (default TORAGENTS_STATE or ~/.toragents/state)")
    ap.add_argument("--detach", action="store_true", help="fork+setsid so the parent dying does not stop it")
    ap.add_argument("--quiet", action="store_true", help="do not print a line per attempt")
    a = ap.parse_args(argv)

    state = a.state or os.environ.get("TORAGENTS_STATE") or os.path.expanduser("~/.toragents/state")
    label, onion = resolve_targets(a.to, state)
    socks_port = resolve_socks(a.socks, state)
    out_path = a.out or f"/tmp/measure-{(label or onion[:12])}-{int(time.time())}.csv"

    print(f"target    {label or '(raw onion)'}")
    print(f"onion     {onion}")
    print(f"socks     127.0.0.1:{socks_port}")
    print(f"window    {a.seconds:.0f}s, one attempt every {a.interval:.1f}s")
    print(f"pid       {os.getpid()}")
    sys.stdout.flush()

    if a.detach and detach(out_path):
        # detach() returns the child's pid in the PARENT and 0 in the CHILD, so
        # this only returns here in the parent. (An earlier version tested
        # os.getpid(), which is true in both -- so the parent returned and the
        # child returned too, and nothing was ever measured.)
        return 0

    log = (lambda *x: None) if a.quiet else print
    try:
        measure(onion, out_path, a.seconds, a.interval, socks_port,
                a.connect_timeout, a.hello_timeout, log=log)
    except KeyboardInterrupt:
        print(f"\n  interrupted -- samples so far are in {out_path}")
        return 130
    return 0


if __name__ == "__main__":
    sys.exit(main())
