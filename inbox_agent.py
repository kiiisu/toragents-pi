#!/usr/bin/env python3
"""inbox_agent.py -- a verified message inbox behind a tor hidden service.

Runs on HER machine. Listens on a plain localhost TCP port; tor forwards the
hidden service to it. Serving needs no tor API at all -- no control port, no
ADD_ONION, no keys to manage. Only outbound sends touch tor, via SOCKS.

Security model
    A message is accepted only if the sender proves possession of an
    allowlisted Ed25519 key over a one-use nonce. The sender's claimed onion
    address is never trusted for routing.

Usage
    inbox_agent.py serve                 run the daemon
    inbox_agent.py whoami                print this machine's onion + fingerprint
    inbox_agent.py add-peer FP --onion A allow a peer to send
    inbox_agent.py peers
    inbox_agent.py inbox
    inbox_agent.py read <id>
    inbox_agent.py ack <id>
    inbox_agent.py send --to LABEL --body TEXT
"""
import argparse
import base64
import hashlib
import json
from pathlib import Path
import os
import re
import secrets
import shutil
import subprocess
import socket
import sys
import threading
import time
import uuid

from wire import AgentKeys, PublicIdentity

HOME = os.environ.get("TORAGENTS_HOME", os.path.expanduser("~/.toragents"))
# TORAGENTS_STATE wins over $HOME/state so this CLI can operate on a flat state
# directory (the layout listener.py uses on the box). Without it the two tools
# disagree about where identity.json lives, and whoami silently mints a
# throwaway key and signs with the wrong one.
STATE = os.environ.get("TORAGENTS_STATE") or os.path.join(HOME, "state")
INBOX = os.path.join(STATE, "inbox")
ARCHIVE = os.path.join(STATE, "archive")
PEERS = os.path.join(STATE, "peers.json")
IDFILE = os.path.join(STATE, "identity.json")
HOSTNAME_FILE = os.path.join(HOME, "hs", "hostname")

CONFIG = os.path.join(STATE, "config.json")


def _installed_config():
    """Read the ports the installer actually configured.

    This exists because the CLI and the daemon must agree on the SOCKS port and
    the daemon is the only one that gets TORAGENTS_SOCKS from its service unit.
    The old fallback was 9050 while the installer writes 9061, so every bare
    `send`/`inbox` died with "Connection refused" until the user exported the
    variable by hand. Env still wins; the recorded config is the fallback.
    """
    try:
        with open(CONFIG) as f:
            return json.load(f)
    except (OSError, ValueError):
        return {}


_cfg = _installed_config()
LISTEN_PORT = int(os.environ.get("TORAGENTS_PORT") or _cfg.get("local_port") or 8799)
SOCKS_PORT = int(os.environ.get("TORAGENTS_SOCKS") or _cfg.get("socks_port") or 9050)
NONCE_TTL = 180.0

b64e = lambda b: base64.b64encode(b).decode("ascii")
b64d = lambda s: base64.b64decode(s)


# ---------------------------------------------------------------- state ---
def dirs():
    for d in (STATE, INBOX, ARCHIVE):
        os.makedirs(d, exist_ok=True)


def keys(create=False):
    """Load the identity. Returns None if absent and create is False.

    Minting is opt-in on purpose. A read-only command that creates an identity
    is a footgun: it writes a new key, and anything that then signs with it is
    silently using the wrong identity, which the peer rejects in a way that
    looks like a network fault.
    """
    dirs()
    if os.path.exists(IDFILE):
        with open(IDFILE) as f:
            return AgentKeys.from_secret_dict(json.load(f))
    if not create:
        return None
    k = AgentKeys.generate()
    fd = os.open(IDFILE, os.O_WRONLY | os.O_CREAT | os.O_TRUNC, 0o600)
    with os.fdopen(fd, "w") as f:
        json.dump(k.to_secret_dict(), f)
    return k


def need_keys():
    k = keys()
    if k is None:
        print(f"no identity at {IDFILE}", file=sys.stderr)
        print("  create one with:  inbox_agent.py init", file=sys.stderr)
        print("  (the installer does this for you; whoami never writes)",
              file=sys.stderr)
        raise SystemExit(2)
    return k


# Retry-on-failure keeps the peers we already have. That is right for a
# TRANSIENT fault, and wrong for a DELIBERATE one: if an operator revokes a
# compromised peer by rewriting peers.json and the result is unreadable or
# invalid, keeping stale peers would leave the revoked key allowlisted. So the
# two failure classes are treated differently:
#
#   absent      -> {}           nothing configured yet
#   unreadable  -> KEEP + retry I/O error: permissions, fd limit, unsynced file
#   corrupt     -> FAIL CLOSED  readable but not a valid allowlist: do not trust it
#
# save_peers() writes tmp+os.replace, so our own writes can never truncate the
# file; "corrupt" means someone edited it by hand or the disk lost a block.
PEERS_ABSENT = "absent"
PEERS_OK = "ok"
PEERS_UNREADABLE = "unreadable"
PEERS_CORRUPT = "corrupt"


def peers_state():
    """(status, mapping) — see the table above. Never raises."""
    if not os.path.exists(PEERS):
        return PEERS_ABSENT, {}
    try:
        with open(PEERS) as f:
            raw = f.read()
    except OSError:
        return PEERS_UNREADABLE, None
    try:
        data = json.loads(raw)
    except ValueError:
        return PEERS_CORRUPT, None
    if not isinstance(data, dict):
        return PEERS_CORRUPT, None
    return PEERS_OK, data


def peers():
    """Just the mapping; {} when absent or unusable. For read-only commands."""
    return peers_state()[1] or {}


def save_peers(p):
    dirs()
    tmp = PEERS + ".tmp"
    with open(tmp, "w") as f:
        json.dump(p, f, indent=2, sort_keys=True)
    os.replace(tmp, PEERS)


def _self_fingerprint():
    """Short sha256 of the file that is actually executing.

    The box once ran a daemon no user ran, which is exactly how two shipped bugs
    stayed invisible. This makes "which code is live" answerable from the log.
    """
    try:
        with open(os.path.abspath(__file__), "rb") as f:
            return hashlib.sha256(f.read()).hexdigest()[:12]
    except OSError:
        return "unknown"


def onion():
    """Resolve our onion address without ever talking to tor.

    Two layouts exist:
      - static HiddenServiceDir (every install this script makes): tor writes
        $HOME/hs/hostname, which wins.
      - ADD_ONION (a daemon that registers through the control port): tor keeps
        no hostname file, so that daemon records the address it got in
        $STATE/onion.txt.

    This must stay read-only and work with tor stopped: during a cutover this
    is the evidence an operator reads, and "onion (not configured)" on a
    healthy migration would invite a false rollback.
    """
    for path in (HOSTNAME_FILE, os.path.join(STATE, "onion.txt")):
        try:
            with open(path) as f:
                val = f.read().strip()
            if val:
                return val
        except OSError:
            continue
    return None


# --------------------------------------------------------------- framing --
def send_msg(sock, obj):
    data = json.dumps(obj).encode()
    sock.sendall(len(data).to_bytes(4, "big") + data)


MAX_FRAME = 16 * 1024 * 1024  # same ceiling toragents uses; stops a hostile header


def recv_msg(sock):
    hdr = _read_exact(sock, 4)
    if hdr is None:
        return None
    n = int.from_bytes(hdr, "big")
    if n > MAX_FRAME:
        raise ValueError(f"declared frame {n} exceeds {MAX_FRAME}")
    body = _read_exact(sock, n)
    if body is None:
        return None
    return json.loads(body.decode())


def _read_exact(sock, n):
    buf = b""
    while len(buf) < n:
        chunk = sock.recv(n - len(buf))
        if not chunk:
            return None
        buf += chunk
    return buf


# ---------------------------------------------------------------- nonces --
class Nonces:
    def __init__(self):
        self._d, self._lock = {}, threading.Lock()

    def issue(self):
        n = secrets.token_bytes(32)
        with self._lock:
            now = time.time()
            self._d = {k: v for k, v in self._d.items() if v > now}
            self._d[b64e(n)] = now + NONCE_TTL
        return n

    def consume(self, token):
        with self._lock:
            exp = self._d.pop(token, None)
        if exp is None or exp < time.time():
            return None
        try:
            return b64d(token)
        except Exception:
            return None


# ---------------------------------------------------------------- server --
class Inbox:
    def __init__(self):
        self.k = keys(create=True)   # a daemon coming up is allowed to mint
        self.nonces = Nonces()
        status, loaded = peers_state()
        self.peers = loaded if loaded is not None else {}
        self._peers_warned = None
        self.lock = threading.Lock()
        self.accepted = self.rejected = 0
        self.addr = onion()
        self._peers_mtime = self._mtime()

    def _mtime(self):
        try:
            return os.path.getmtime(PEERS)
        except OSError:
            return None

    def _reload_peers_if_changed(self):
        """Pick up `add-peer` without restarting the daemon.

        On an unreadable file this KEEPS the peers already loaded and does not
        advance the stamp, so the read is retried on the next message instead of
        pinning an empty allowlist until the file next changes.
        """
        now = self._mtime()
        if now == self._peers_mtime:
            return
        status, loaded = peers_state()
        if status == PEERS_UNREADABLE:
            if now != self._peers_warned:
                print(f"[inbox] peers.json unreadable; keeping "
                      f"{len(self.peers)} allowed peer(s), will retry", flush=True)
                self._peers_warned = now
            return
        if status == PEERS_CORRUPT:
            # Readable but invalid: fail closed rather than trust stale peers,
            # because this is also what a botched revocation looks like.
            self.peers = {}
            self._peers_mtime = now
            self._peers_warned = None
            print(f"[inbox] peers.json is CORRUPT; failing closed with 0 allowed "
                  f"peer(s) until it is fixed: {PEERS}", flush=True)
            return
        # No lock here on purpose: the caller already holds self.lock and
        # threading.Lock is not reentrant. Assigning the dict reference is
        # atomic, and a reader that grabbed the old dict iterates a stable
        # snapshot, so a swap mid-read is safe.
        self.peers = loaded
        self._peers_mtime = now
        self._peers_warned = None
        print(f"[inbox] peer list reloaded ({len(self.peers)} allowed)", flush=True)

    def handle(self, req):
        op = req.get("op")
        if op == "ping":
            return {"ok": True, "service": "toragents-inbox", "onion": self.addr,
                    "fingerprint": self.k.public().fingerprint}
        if op == "hello":
            return {"ok": True, "nonce": b64e(self.nonces.issue())}
        if op == "send":
            return self._accept(req)
        return {"ok": False, "error": f"unknown op {op!r}"}

    def _accept(self, req):
        token, body, pub, sig = (req.get("nonce"), req.get("body"),
                                 req.get("pubkey"), req.get("sig"))
        if not all([token, isinstance(body, str), pub, sig]):
            self.rejected += 1
            return {"ok": False, "error": "malformed send"}

        nonce = self.nonces.consume(token)
        if nonce is None:
            self.rejected += 1
            return {"ok": False, "error": "stale or unknown nonce"}

        try:
            sender = PublicIdentity.from_dict(pub)
            signature = b64d(sig)
        except Exception:
            self.rejected += 1
            return {"ok": False, "error": "bad pubkey or signature encoding"}

        if not sender.verify(nonce + body.encode("utf-8"), signature):
            self.rejected += 1
            return {"ok": False, "error": "signature does not verify"}

        fp = sender.fingerprint
        self._reload_peers_if_changed()
        with self.lock:
            peer = self.peers.get(fp)
        if peer is None:
            self.rejected += 1
            return {"ok": False, "error": "sender not on allowlist",
                    "fingerprint": fp}

        mid = uuid.uuid4().hex[:12]
        rec = {"id": mid, "received": time.time(), "from_fingerprint": fp,
               "from_label": peer.get("label", ""), "from_onion": peer.get("onion", ""),
               "body": body}
        for f in ("from_session", "to_session"):
            v = req.get(f)
            if isinstance(v, str) and v.strip():
                rec[f] = v.strip()[:64]
        path = os.path.join(INBOX, f"{time.time():.0f}-{mid}.json")
        with open(path + ".tmp", "w") as f:
            json.dump(rec, f, indent=2)
        os.replace(path + ".tmp", path)
        self.accepted += 1
        print(f"[inbox] +{mid} from {peer.get('label') or fp[:12]}", flush=True)
        return {"ok": True, "id": mid}


def client_loop(conn, box):
    try:
        while True:
            req = recv_msg(conn)
            if req is None:
                break
            try:
                reply = box.handle(req)
            except Exception as exc:
                reply = {"ok": False, "error": f"handler: {exc}"}
            send_msg(conn, reply)
    except Exception:
        pass
    finally:
        try:
            conn.close()
        except Exception:
            pass


def serve():
    box = Inbox()
    srv = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
    srv.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
    srv.bind(("127.0.0.1", LISTEN_PORT))
    srv.listen(16)
    print(f"listening 127.0.0.1:{LISTEN_PORT}", flush=True)
    print(f"onion      {box.addr or '(hidden service not configured yet)'}", flush=True)
    print(f"fingerprint {box.k.public().fingerprint}", flush=True)
    print(f"peers      {len(box.peers)}", flush=True)
    try:
        while True:
            conn, _ = srv.accept()
            threading.Thread(target=client_loop, args=(conn, box), daemon=True).start()
    except KeyboardInterrupt:
        pass
    finally:
        srv.close()


# ---------------------------------------------------------------- client --
# Escalating ladder: fetching a COLD descriptor (a peer whose tor has only just
# published, i.e. every brand-new user's first message) routinely takes longer
# than a single short timeout. A flat 4x30s budget produced a spurious
# ConnectionError on first contact, then succeeded instantly on retry.
DIAL_LADDER = [int(x) for x in
               os.environ.get("TORAGENTS_DIAL_LADDER", "30,60,120,180").split(",")]


def dial(target_onion, timeout=None):
    """Plain SOCKS5 dial through the local tor. No tor API, just socks."""
    import socks
    host = target_onion[:-6] if target_onion.endswith(".onion") else target_onion
    last = None
    for i, budget in enumerate(DIAL_LADDER):
        s = socks.socksocket()
        s.set_proxy(socks.SOCKS5, "127.0.0.1", SOCKS_PORT, rdns=True)
        s.settimeout(budget)
        if i:
            print(f"  retry {i}/{len(DIAL_LADDER) - 1} "
                  f"(waiting up to {budget}s for the descriptor)",
                  file=sys.stderr)
        try:
            s.connect((host + ".onion", 80))
            return s
        except Exception as exc:
            last = exc
            try:
                s.close()
            except Exception:
                pass
    raise ConnectionError(
        f"cannot reach {target_onion[:16]}... after {len(DIAL_LADDER)} attempts: {last}\n"
        f"  MEASURED: a peer whose tor has just restarted takes about THREE MINUTES\n"
        f"  to republish its descriptor (182 s measured on this project). Every\n"
        f"  reinstall on either side restarts tor, so a fresh install is followed by\n"
        f"  three minutes in which that peer is unreachable and looks dead. Wait, do\n"
        f"  not reinstall again to 'fix' it -- that restarts the clock.\n"
        f"  Check the peer is running and tor is up on 127.0.0.1:{SOCKS_PORT}.")


def cmd_reach(a):
    """Is the peer actually reachable, right now, through OUR tor?

    This exists because two separate mistakes produced a false "the peer is
    down" in one session:

      1. dialling the peer's LOCAL port. The onion exposes ONE virtual port (80)
         which their tor forwards to their listener; their local port number is
         invisible from here and dialling it is refused.
      2. dialling through the wrong tor. If the state config has no socks_port
         the CLI falls back to 9050 -- the machine's default tor, which may not
         be the instance that carries these circuits -- and descriptor fetches
         stall there while the dedicated instance answers in seconds.

    So: dial port 80, report WHICH socks port did it, and time it.
    """
    import time
    p = peers()
    target = None
    for fp, v in p.items():
        if not a.to or a.to in (fp, v.get("label"), fp[:12]):
            target = v
            break
    if target is None:
        sys.exit(f"unknown peer {a.to!r}; try: inbox_agent.py peers")
    label = target.get("label") or target["onion"][:16]
    print(f"peer      {label}")
    print(f"onion     {target['onion']}")
    print(f"dialling  {target['onion'][:16]}...:80   via socks 127.0.0.1:{SOCKS_PORT}")
    t0 = time.time()
    try:
        s = dial(target["onion"], )
    except ConnectionError as exc:
        print(f"\n  UNREACHABLE after {time.time() - t0:.1f}s")
        print(f"  {exc}")
        if SOCKS_PORT == 9050:
            print("  note: this used tor's default SOCKS port. If this machine runs a")
            print("        dedicated tor for toragents, set socks_port in")
            print("        <state>/config.json or TORAGENTS_SOCKS, and retry.")
        sys.exit(2)
    dt = time.time() - t0
    try:
        s.close()
    except Exception:
        pass
    print(f"\n  REACHABLE in {dt:.1f}s -- their tor published, their listener accepted")


def auto_session() -> str:
    """Who is speaking, without asking anyone to configure anything.

    pi exports PI_SESSION_ID to everything it spawns, and that id is stable per
    conversation and survives --resume. So a short prefix is a name that
    identifies the session to the other machine for its whole life: no setup, no
    env var to remember, and two sessions on one box stop looking like one.
    """
    explicit = (os.environ.get("TORAGENTS_SESSION") or "").strip()
    if explicit:
        return explicit[:64]
    for var, prefix in (("PI_SESSION_ID", "pi"),):
        v = (os.environ.get(var) or "").strip()
        if v:
            return f"{prefix}-{v.replace('-', '')[:8]}"
    # No id in the environment: something other than pi is calling. There is
    # nothing left to guess from -- the hook that used to leave a note here is
    # gone with the other harnesses. Callers pass --session or set
    # TORAGENTS_SESSION; an empty name is honest and the header shows the peer.
    return ""


def send_to(target_onion, body, k=None, session=None, to_session=None):
    k = k or need_keys()             # never sign with a freshly minted key
    sock = dial(target_onion)
    try:
        send_msg(sock, {"op": "hello"})
        hello = recv_msg(sock)
        if not hello or not hello.get("ok"):
            raise RuntimeError(f"hello refused: {hello}")
        nonce = b64d(hello["nonce"])
        payload = {"op": "send", "nonce": hello["nonce"], "body": body,
                   "pubkey": k.public().to_dict(),
                   "sig": b64e(k.sign(nonce + body.encode("utf-8")))}
        # Session names are ADVISORY labels, deliberately outside the signature:
        # the peer is authenticated by key, and which of that peer's live
        # sessions is talking is not something the signature can bind anyway.
        # Old receivers ignore these fields, so this stays compatible.
        if session:
            payload["from_session"] = session
        if to_session:
            payload["to_session"] = to_session
        send_msg(sock, payload)
        return recv_msg(sock)
    finally:
        try:
            sock.close()
        except Exception:
            pass


# ------------------------------------------------------------------- cli --

MY_SESSION = ""
RECEIVE_TARGETED = False


def cmd_doctor(a):
    """Answer "is the receiving half wired up on THIS machine?" -- measured.

    pi has one receiving path, the extension, so there is exactly one question:
    is the extension installed in the directory THIS pi reads, and is it keeping
    up with the inbox? No harness config to parse and no hook that can fail
    silently, which is the whole reason this project is pi-only.
    """
    home = os.path.expanduser("~")
    state = STATE
    inbox = os.path.join(state, "inbox")
    # The extension resolves RECEIVE from the PI process's environment. doctor
    # usually runs in a different shell, where the same variable may be set,
    # unset, or set differently -- so its own env is NOT evidence of what the
    # extension does. Prefer what the extension published at session start; fall
    # back to env only when there is no report, and say which one was used.
    global MY_SESSION, RECEIVE_TARGETED
    ext_info, ext_source = None, "env (extension has never reported)"
    # The extension resolves its state directory by its own rules (explicit
    # TORAGENTS_STATE, then TORAGENTS_HOME/state, then a scored choice among the
    # defaults). Reading the report from doctor's default two-line guess meant
    # that on any machine where those differ -- which is the whole reason the
    # extension has scoring -- doctor found nothing and fell back to the shell.
    candidates = []
    if os.environ.get("TORAGENTS_STATE"):
        candidates.append(os.environ["TORAGENTS_STATE"])
    if os.environ.get("TORAGENTS_HOME"):
        candidates.append(os.path.join(os.environ["TORAGENTS_HOME"], "state"))
    candidates.append(state)
    candidates.append(os.path.join(os.path.expanduser("~"), ".toragents", "state"))
    candidates.append("/var/lib/toragents-inbox")
    seen, uniq = set(), []
    for c in candidates:
        if c and c not in seen:
            seen.add(c)
            uniq.append(c)
    for cand in uniq:
        try:
            with open(os.path.join(cand, "extension.json"), encoding="utf-8") as f:
                ext_info = json.load(f)
        except (OSError, ValueError, TypeError):
            continue
        when = time.strftime("%H:%M", time.localtime(float(ext_info.get("at", 0)) / 1000))
        ext_source = f"extension.json (last seen {when})"
        reported_state = str(ext_info.get("state") or "").strip()
        if reported_state and reported_state != state:
            # The extension is watching a different directory than this command
            # is inspecting. Say so and read ITS state, because that is the world
            # the labels are supposed to describe.
            print(f"  WARNING      the extension watches {reported_state}")
            print(f"               this command looked in {state}")
            state = reported_state
            inbox = os.path.join(state, "inbox")
        break
    if ext_info is None:
        state_line = state
        print(f"  state        {state_line}")
    if isinstance(ext_info, dict):
        MY_SESSION = str(ext_info.get("session") or "").strip()
        RECEIVE_TARGETED = str(ext_info.get("receive") or "all").strip().lower() == "targeted"
    else:
        MY_SESSION = (os.environ.get("TORAGENTS_SESSION") or "").strip() or auto_session()
        RECEIVE_TARGETED = (os.environ.get("TORAGENTS_RECEIVE") or "all").strip().lower() == "targeted"

    def count(d):
        try:
            return len([n for n in os.listdir(d) if n.endswith(".json")])
        except OSError:
            return -1

    if ext_info is not None:
        print(f"  state        {state}")
    n_inbox = count(inbox)
    print(f"  inbox        {'no directory yet (nothing has arrived)' if n_inbox < 0 else str(n_inbox) + ' file(s) waiting'}")
    try:
        import json as _j
        announced = _j.load(open(os.path.join(state, "announced.json")))
        announced = announced if isinstance(announced, list) else list(announced)
    except Exception:
        announced = []
    print(f"  announced    {len(announced)} already delivered")
    for name in ("onion.txt", "config.json", "peers.json"):
        fp = os.path.join(state, name)
        print(f"  {name:12} {'present' if os.path.exists(fp) else 'MISSING'}")

    # The only thing that turns a file into a message the agent can see.
    print("\n  pi extension")
    # pi honours PI_CODING_AGENT_DIR; assuming ~/.pi/agent is the same class of
    # mistake as assuming a settings file path. Resolve it, and say which.
    pi_dir = os.environ.get("PI_CODING_AGENT_DIR") or os.path.join(home, ".pi", "agent")
    ext_dir = os.path.join(pi_dir, "extensions")
    ext = os.path.join(ext_dir, "toragents-inbox.ts")
    if os.path.exists(ext):
        size = os.path.getsize(ext)
        print(f"  installed    {ext} ({size} bytes)")
    else:
        print(f"  MISSING      {ext}")
        print("               messages will pile up and never reach the session.")
        print("               Re-run the installer, or copy toragents_watch.ts there.")
    print(f"  pi dir       {pi_dir} "
          f"({'present' if os.path.isdir(pi_dir) else 'MISSING'}"
          f"{', from PI_CODING_AGENT_DIR' if os.environ.get('PI_CODING_AGENT_DIR') else ''})")
    print(f"  resolves     receive={('targeted' if RECEIVE_TARGETED else 'all')} "
          f"session={MY_SESSION or '(none)'} -- from {ext_source}")

    # Who took the mail, read from the claim files themselves. The extension
    # writes "<session> <ms>" when it claims a message, so this needs no second
    # file and survives announced.json being lost. (The previous version read
    # delivered_by.json, whose only writer was the hook -- so on every pi-only
    # machine this section printed nothing and claimed otherwise.)
    try:
        claim_dir = os.path.join(state, "claimed")
        claims = []
        for name in os.listdir(claim_dir):
            try:
                with open(os.path.join(claim_dir, name), encoding="utf-8") as f:
                    who = f.read().strip()
            except OSError:
                who = "?"
            parts = who.split()
            claims.append((int(parts[1]) if len(parts) > 1 and parts[1].isdigit() else 0,
                           name, parts[0] if parts else "?"))
        claims.sort(reverse=True)
        print(f"  claimed      {len(claims)} message(s) owned")
        if claims:
            print("  recently delivered by")
            for _ts, mid, who in claims[:5]:
                when = time.strftime("%H:%M", time.localtime(_ts / 1000)) if _ts else "?"
                print(f"  {mid:14} {when}  by {who}")
    except FileNotFoundError:
        pass
    except OSError:
        pass

    # Failures must be visible somewhere, or "delivered" is a lie.
    try:
        failed = sorted(os.listdir(os.path.join(state, "failed")))
    except OSError:
        failed = []
    if failed:
        print(f"  failed       {len(failed)}: {', '.join(failed[:5])}")
        print("               NOT delivered. Run /toragents-retry inside pi.")

    # What is sitting there, and whether the extension has already announced it.
    print("\n  waiting")
    try:
        names = sorted((n for n in os.listdir(inbox) if n.endswith(".json")), reverse=True)
    except OSError:
        names = []
    if not names:
        print("  none")
        return 0
    for n in names[:8]:
        try:
            import json as _j
            m = _j.load(open(os.path.join(inbox, n), encoding="utf-8"))
        except Exception:
            print(f"  {n}  unreadable")
            continue
        mid = str(m.get("id") or "?")
        to_sess = (m.get("to_session") or "").strip()
        if mid in announced:
            state_word = "delivered"
        elif RECEIVE_TARGETED and not to_sess:
            # In the documented quiet-box mode an untargeted message is filtered
            # out before it is ever claimed. No tick, retry or restart will
            # deliver it, so calling it PENDING (waiting for something that will
            # happen) is a lie.
            state_word = "UNROUTABLE"
        elif to_sess and to_sess != MY_SESSION:
            # Correctly addressed to another session. Unlabelled, a human running
            # doctor in the wrong window reads it as stuck mail.
            state_word = f"for {to_sess[:9]}"
        else:
            state_word = "PENDING"
        print(f"  {mid:14} {state_word:12} to={to_sess or '-':18} "
              f"from={m.get('from_label') or m.get('from') or '?'}")
    return 0


def cmd_serve(a):
    print(f"[inbox] running inbox_agent.py sha256={_self_fingerprint()} "
          f"state={STATE}", flush=True)
    serve()


def cmd_init(a):
    existed = os.path.exists(IDFILE)
    k = keys(create=True)
    print(f"{'existing' if existed else 'created'}  {IDFILE}")
    print(f"fingerprint  {k.public().fingerprint}")
    if existed:
        print("onion        unchanged (identity reused)")


def cmd_whoami(a):
    k = keys()                      # never creates
    print(f"onion        {onion() or '(not configured)'}")
    if k is None:
        print(f"fingerprint  (no identity yet - run: inbox_agent.py init)")
        raise SystemExit(2)
    print(f"fingerprint  {k.public().fingerprint}")


def _writable_peers():
    """Allowlist for a read-modify-write. Refuses to start from a bad file.

    Rewriting a file you could not read is how one broken allowlist becomes one
    peer and a silent revocation of everyone else.
    """
    status, p = peers_state()
    if status in (PEERS_UNREADABLE, PEERS_CORRUPT):
        sys.exit(f"refusing to modify {PEERS}: it is {status}. "
                 f"Fix or delete the file first.")
    return p or {}


def cmd_add_peer(a):
    p = _writable_peers()
    fp = a.fingerprint.strip().lower()
    if len(fp) != 32:
        sys.exit(f"fingerprint must be 32 hex chars (got {len(fp)})")
    if not a.onion.endswith(".onion"):
        sys.exit("--onion must end in .onion")
    p[fp] = {"onion": a.onion.strip(), "label": a.label or fp[:12],
             "added": time.time()}
    save_peers(p)
    print(f"allowlisted {p[fp]['label']} ({fp[:12]}...) -> {a.onion}")


def cmd_tone(a):
    """Per-peer voice: read it when composing, don't try to remember it."""
    p = _writable_peers()
    wanted = (a.label or "").strip().lower()
    matches = [(fp, v) for fp, v in p.items()
               if (v.get("label") or "").lower() == wanted or fp.startswith(wanted)]
    if not matches:
        sys.exit(f"no peer matching {a.label!r} (see `inbox peers`)")
    fp, v = matches[0]

    if a.clear:
        v.pop("tone", None)
        v.pop("styles", None)
        p[fp] = v
        save_peers(p)
        print(f"cleared tone and styles for {v.get('label')}")
        return

    changed = False
    if a.styles is not None:
        # Styles drive which decks the picker draws from: an agent-flavoured
        # profile for one peer, plain for another.
        v["styles"] = [x.strip().lower() for x in a.styles.split(",") if x.strip()]
        changed = True
    if a.text:
        v["tone"] = " ".join(a.text).strip()
        changed = True
    if changed:
        p[fp] = v
        save_peers(p)
    print(v.get("tone") or f"(no tone set for {v.get('label')} — plain register)")
    if v.get("styles"):
        print(f"styles: {', '.join(v['styles'])}")


def _seed_path():
    """Installers drop the seed beside the daemon; a repo checkout keeps it in
    docs/. Both are legitimate, so look in both rather than assuming one."""
    here = Path(__file__).resolve().parent
    for cand in (here / "lexicon.seed.json", here / "docs" / "lexicon.seed.json"):
        if cand.exists():
            return cand
    return here / "lexicon.seed.json"


LEXICON_SEED = _seed_path()


def _lexicon_path():
    return Path(STATE) / "lexicon.json"


def _lexicon():
    """The pool, seeded on first use and self-healing if it is unreadable.

    A corrupt pool must never take the channel down: this is decoration, and
    falling back to the seed costs one file read. But a *programming* error here
    must not be swallowed either -- the first version of this merge caught its own
    UnboundLocalError and quietly half-merged, so `stats` reported counts the pool
    did not hold.
    """
    path = _lexicon_path()
    if not path.exists():
        try:
            seed = json.loads(LEXICON_SEED.read_text())
        except Exception:
            return {"version": 1, "names": [], "moves": []}
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(json.dumps(seed, indent=1, ensure_ascii=False))
        return seed

    try:
        d = json.loads(path.read_text())
    except Exception:
        try:
            return json.loads(LEXICON_SEED.read_text())
        except Exception:
            return {"version": 1, "names": [], "moves": []}

    d.setdefault("names", [])
    d.setdefault("moves", [])
    try:
        seed = json.loads(LEXICON_SEED.read_text())
    except Exception:
        return d          # no seed to learn from; the pool we have is still valid

    # Absorb decks added since this pool was created. Keyed by (style, text), not
    # text alone: "good machine" is a praise word AND an earnest one, and matching
    # on text dropped it from whichever deck it did not already belong to.
    added = 0
    for kind in ("names", "moves"):
        have = {((r.get("style") or "custom"), (r.get("text") or "").lower())
                for r in d.get(kind, [])}
        for r in seed.get(kind, []):
            key = (r.get("style") or "custom", (r.get("text") or "").lower())
            if key not in have:
                d[kind].append(dict(r, used=0, last=0))
                have.add(key)
                added += 1
        # old unstyled rows adopt the style of the seed entry they match
        index = {(r.get("text") or "").lower(): (r.get("style") or "custom")
                 for r in seed.get(kind, [])}
        for r in d.get(kind, []):
            if not r.get("style") or r["style"] == "custom":
                r["style"] = index.get((r.get("text") or "").lower(), "custom")
    if added:
        _lexicon_save(d)
    return d


def _lexicon_save(d):
    try:
        _lexicon_path().parent.mkdir(parents=True, exist_ok=True)
        _lexicon_path().write_text(json.dumps(d, indent=1, ensure_ascii=False))
    except Exception as e:
        print(f"warn: could not save lexicon ({e})", file=sys.stderr)


def _pick(entries, n, rng):
    """Least-recently-used, with a little shuffle inside the same usage tier.

    Rotation is the point: a pool that keeps handing back the same word is a
    pool that is shrinking in practice, however many rows it has.
    """
    ranked = sorted(entries, key=lambda e: (e.get("used", 0), e.get("last", 0)))
    pool = ranked[: max(n * 3, n)]
    rng.shuffle(pool)
    return pool[:n]


def _styles_summary(d):
    seen = {}
    for k in ("names", "moves"):
        for r in d.get(k, []):
            sid = r.get("style") or "custom"
            e = seen.setdefault(sid, {"names": 0, "moves": 0})
            e[k] += 1
    return seen


def cmd_lexicon(a):
    import random
    d = _lexicon()
    kind = a.kind
    if a.action == "stats":
        for k in ("names", "moves"):
            rows = d.get(k, [])
            used = sum(1 for r in rows if r.get("used"))
            print(f"{k:6} {len(rows):3} entries · {used} used at least once")
        print("-- by style")
        for sid, c in sorted(_styles_summary(d).items()):
            print(f"   {sid:16} {c['names']:3} names · {c['moves']:3} moves")
        return
    if a.action == "styles":
        for sid, c in sorted(_styles_summary(d).items()):
            print(f"{sid:16} {c['names']:3} names · {c['moves']:3} moves")
        return
    if a.action == "add":
        # REMAINDER swallows options that follow the action, so pull --kind back
        # out instead of treating "--kind names" as part of the phrase. (It did.)
        toks, keep, i = (a.text or []), [], 0
        while i < len(toks):
            if toks[i] == "--kind" and i + 1 < len(toks):
                kind = toks[i + 1]
                i += 2
                continue
            if toks[i].startswith("--kind="):
                kind = toks[i].split("=", 1)[1]
                i += 1
                continue
            keep.append(toks[i])
            i += 1
        text = " ".join(keep).strip()
        if not text:
            sys.exit("nothing to add")
        rows = d.setdefault(kind, [])
        if any(r["text"].lower() == text.lower() for r in rows):
            print(f"already in {kind}: {text}")
            return
        prefix = "n" if kind == "names" else "m"
        style = (a.style or "custom").split(",")[0].strip().lower()
        rows.append({"id": f"{prefix}{len(rows)+1:03d}", "text": text, "style": style,
                     "used": 0, "last": 0, "at": time.time()})
        _lexicon_save(d)
        print(f"added to {kind} ({len(rows)} total): {text}")
        return
    if a.action == "pick":
        rng = random.Random(a.seed) if a.seed is not None else random.Random()
        out = {}
        # A body that already used a word marks it used, so the next pick
        # rotates away from it without anyone having to keep a ledger by hand.
        body = ""
        if a.body_file:
            body = sys.stdin.read() if a.body_file == "-" else Path(a.body_file).read_text()
        touched = []
        low = body.lower()
        if low:
            for k in ("names", "moves"):
                for r in d.get(k, []):
                    if r["text"].lower().split("{")[0].strip() and r["text"].lower().split("{")[0].strip() in low:
                        r["used"] = r.get("used", 0) + 1
                        r["last"] = int(time.time())
                        touched.append(r["id"])
        wanted = [x.strip().lower() for x in (a.style or "").split(",") if x.strip()]
        if wanted:
            known = set(_styles_summary(d))
            unknown = [w for w in wanted if w not in known]
            if unknown:
                sys.exit(f"no such style: {', '.join(unknown)} (known: {', '.join(sorted(known))})")
        def allowed(r):
            if wanted and (r.get("style") or "custom") not in wanted:
                return False
            # A rare word (hunni, honey) is offered only occasionally: everyday
            # rotation of a diminutive is what makes a pet name feel like a field
            # in a form. --rare-chance 0 keeps them out entirely, 1 always allows.
            if r.get("rare") and rng.random() >= a.rare_chance:
                return False
            return True
        for k in ("names", "moves"):
            picks = _pick([r for r in d.get(k, []) if allowed(r)], a.count, rng)
            out[k] = [{"id": r["id"], "text": r["text"]} for r in picks]
            if not a.peek:
                # peek must not consume rotation: prompt-time suggestions are
                # shown before anyone knows whether they were used.
                for r in picks:
                    r["used"] = r.get("used", 0) + 1
                    r["last"] = int(time.time())
        if not a.peek:
            _lexicon_save(d)
        if a.json:
            print(json.dumps({"ok": True, "picked": out, "marked": touched,
                              "sizes": {k: len(d.get(k, [])) for k in ("names", "moves")}}))
        else:
            for k in ("names", "moves"):
                print(f"-- {k}")
                for r in out[k]:
                    print(f"   {r['text']}")
            if touched:
                print(f"marked used: {', '.join(touched)}")
        return
    sys.exit(f"unknown lexicon action {a.action}")


def cmd_peers(a):
    p = peers()
    if not p:
        print("(no peers allowlisted)")
    for fp, v in sorted(p.items(), key=lambda kv: kv[1].get("label", "")):
        tone = v.get("tone")
        print(f"{v.get('label',''):18} {fp[:16]}...  {v['onion']}"
              + (f"\n{'':18} tone: {tone}" if tone else ""))


def _msgs(include_archived=False):
    out = []
    for d, arch in ((INBOX, False), (ARCHIVE, True)):
        if not os.path.isdir(d):
            continue
        if arch and not include_archived:
            continue
        for n in sorted(os.listdir(d)):
            if n.endswith(".json"):
                try:
                    with open(os.path.join(d, n)) as f:
                        m = json.load(f)
                    m["_archived"] = arch
                    out.append(m)
                except Exception:
                    pass
    return sorted(out, key=lambda m: m.get("received", 0))


def cmd_inbox(a):
    ms = _msgs(a.all)
    if not ms:
        print("(inbox empty)")
    for m in ms:
        when = time.strftime("%Y-%m-%d %H:%M", time.localtime(m.get("received", 0)))
        who = m.get("from_label", "?")
        if m.get("from_session"):
            who = f"{who}/{m['from_session']}"
        if m.get("to_session"):
            who = f"{who} -> {m['to_session']}"
        print(f"{m['id']}  {when}  {who[:30]:30} "
              f"{(m.get('body') or '').replace(chr(10),' ')[:60]}")


def cmd_read(a):
    for m in _msgs(True):
        if m["id"] == a.id:
            print(json.dumps(m, indent=2))
            return
    sys.exit(f"no message {a.id}")


def cmd_ack(a):
    for n in (os.listdir(INBOX) if os.path.isdir(INBOX) else []):
        if a.id in n:
            os.replace(os.path.join(INBOX, n), os.path.join(ARCHIVE, n))
            print(f"archived {a.id}")
            return
    sys.exit(f"no unarchived message {a.id}")


def _read_body(a):
    """Body from a file is taken verbatim; body from argv is not trustworthy.

    A double-quoted argument is expanded by the sending shell, so prose that
    mentions a substitution loses it before this process runs -- measured on the
    other side of this wire five times, in sentences describing that very
    mechanism. Reading the file means the bytes on the wire are the bytes in the
    file, which is also the only way a send-side fingerprint can mean anything.
    """
    path = getattr(a, "body_file", None)
    if not path:
        if a.body is None:
            sys.exit("send: give --body or --body-file")
        return a.body
    if a.body is not None:
        sys.exit("send: --body and --body-file are mutually exclusive")
    try:
        if path in ("-", "/dev/stdin"):
            data = sys.stdin.buffer.read()
        else:
            with open(path, "rb") as f:
                data = f.read()
    except OSError as e:
        sys.exit(f"send: cannot read --body-file {path}: {e.strerror}")
    try:
        # A BOM is an encoding marker, not content: Windows editors and
        # `Set-Content -Encoding utf8` add one, and it would otherwise arrive as
        # the first character of the message.
        return data.decode("utf-8-sig")
    except UnicodeDecodeError as e:
        sys.exit(f"send: --body-file is not valid UTF-8: {e}")


def cmd_send(a):
    a.body = _read_body(a)
    p = peers()
    target = None
    for fp, v in p.items():
        if a.to in (fp, v.get("label"), fp[:12]):
            target = v
            break
    if target is None:
        sys.exit(f"unknown peer {a.to!r}; try: inbox_agent.py peers")
    reply = send_to(target["onion"], a.body,
                    session=getattr(a, "session", None) or auto_session() or None,
                    to_session=getattr(a, "to_session", "") or None)
    print(json.dumps(reply, indent=2))
    sys.exit(0 if reply and reply.get("ok") else 1)


def main():
    ap = argparse.ArgumentParser(description=__doc__,
                                 formatter_class=argparse.RawDescriptionHelpFormatter)
    s = ap.add_subparsers(dest="cmd", required=True)
    s.add_parser("serve").set_defaults(fn=cmd_serve)
    s.add_parser("whoami").set_defaults(fn=cmd_whoami)
    s.add_parser("doctor").set_defaults(fn=cmd_doctor)
    s.add_parser("init").set_defaults(fn=cmd_init)
    s.add_parser("peers").set_defaults(fn=cmd_peers)
    p = s.add_parser("add-peer")
    p.add_argument("fingerprint")
    p.add_argument("--onion", required=True)
    p.add_argument("--label", default="")
    p.set_defaults(fn=cmd_add_peer)
    p = s.add_parser("lexicon")
    p.add_argument("action", choices=["pick", "add", "stats", "styles"], nargs="?", default="pick")
    p.add_argument("--style", default=None,
                   help="restrict to one or more styles: --style annoying,deadpan "
                        "(see `lexicon styles`)")
    p.add_argument("--kind", choices=["names", "moves", "both"], default="both")
    p.add_argument("--count", type=int, default=3)
    p.add_argument("--seed", type=int, default=None)
    p.add_argument("--peek", action="store_true",
                   help="sample without marking anything used (prompt-time suggestions)")
    p.add_argument("--rare-chance", type=float, default=0.0,
                   help="probability of offering a word flagged rare (default 0)")
    p.add_argument("--body-file", default=None, help="mark words this body already used")
    p.add_argument("--json", action="store_true")
    p.add_argument("text", nargs="*", default=None,
                   help="for `add`: the words, as trailing arguments")
    p.set_defaults(fn=cmd_lexicon)
    p = s.add_parser("tone")
    p.add_argument("label")
    p.add_argument("text", nargs="*", default=None,
                   help="omit to print the current tone, give words to set it")
    p.add_argument("--styles", default=None,
                   help="preferred lexicon styles for this peer: --styles feral,annoying")
    p.add_argument("--clear", action="store_true")
    p.set_defaults(fn=cmd_tone)
    p = s.add_parser("inbox")
    p.add_argument("--all", action="store_true")
    p.set_defaults(fn=cmd_inbox)
    p = s.add_parser("read")
    p.add_argument("id")
    p.set_defaults(fn=cmd_read)
    p = s.add_parser("ack")
    p.add_argument("id")
    p.set_defaults(fn=cmd_ack)
    p = s.add_parser("reach")
    p.add_argument("--to", default="")
    p.set_defaults(fn=cmd_reach)
    p = s.add_parser("send")
    p.add_argument("--to", required=True)
    p.add_argument("--body", default=None,
                   help="message text. Prefer --body-file for prose: a body on "
                        "the command line is shell-expanded before we ever see it, "
                        "so a literal $(cat file) in the text is executed and "
                        "replaced by nothing, and trailing blank lines are stripped")
    p.add_argument("--body-file", default=None, metavar="PATH",
                   help="read the body from PATH as UTF-8, byte for byte (use - for stdin)")
    p.add_argument("--session", default=None,
                   help="name of THIS session; auto-derived from the harness if omitted")
    p.add_argument("--to-session", default="", help="only deliver to this session name")
    p.set_defaults(fn=cmd_send)
    a, extra = ap.parse_known_args()
    # `lexicon add --kind names some free words`: options first, phrase last.
    # argparse cannot always split two positionals, so take what is left over.
    if extra:
        if hasattr(a, "text") and a.text is not None:
            a.text = list(a.text) + list(extra)
        else:
            ap.error(f"unrecognized arguments: {' '.join(extra)}")
    a.fn(a)


if __name__ == "__main__":
    main()
