"""The wire protocol, tested without Tor: same handler, plain localhost.

Six things must be true, and four of them are refusals. A permissive daemon
passes the happy path; only the negatives show whether the auth is real.
"""
import base64, json, os, socket, subprocess, sys, tempfile, time

HERE = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, HERE)

# Point the module at a throwaway state BEFORE importing it: STATE and HOME are
# resolved at import time, and a test that imports first writes into the real
# ~/.toragents. That happened once and overwrote a real allowlist.
H = tempfile.mkdtemp(prefix="toragents-wire-")
STATE = os.path.join(H, "state")
os.makedirs(os.path.join(STATE, "inbox"), exist_ok=True)
os.environ["TORAGENTS_HOME"] = H
os.environ["TORAGENTS_STATE"] = STATE

import inbox_agent as IA
from wire import AgentKeys

port = 0
for candidate in range(18900, 19000):
    s = socket.socket()
    try:
        s.bind(("127.0.0.1", candidate))
        port = candidate
        break
    except OSError:
        continue
    finally:
        s.close()

with open(os.path.join(STATE, "config.json"), "w") as f:
    json.dump({"local_port": port, "socks_port": 1}, f)

# Keys and allowlist FIRST: the daemon reads peers.json at startup, and a test
# that writes it afterwards is testing a daemon that has never heard of the peer.
peer = AgentKeys.generate()
stranger = AgentKeys.generate()
IA.save_peers({peer.public().fingerprint: {
    "onion": "test.onion", "label": "peer", "added": time.time()}})

env = dict(os.environ, TORAGENTS_HOME=H, TORAGENTS_STATE=STATE)
daemon = subprocess.Popen([sys.executable, os.path.join(HERE, "inbox_agent.py"), "serve"],
                          env=env, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)

def connect():
    for _ in range(50):
        try:
            return socket.create_connection(("127.0.0.1", port), timeout=5)
        except OSError:
            time.sleep(0.1)
    raise SystemExit("daemon never listened")

def hello(sock):
    IA.send_msg(sock, {"op": "hello"})
    return IA.recv_msg(sock)

def send(sock, keys, body, nonce_tok, *, sig=None, pub=None):
    nonce = base64.b64decode(nonce_tok)
    payload = {
        "op": "send",
        "nonce": nonce_tok,
        "body": body,
        "pubkey": (pub or keys.public().to_dict()),
        "sig": base64.b64encode(sig if sig is not None
                                else keys.sign(nonce + body.encode())).decode(),
    }
    IA.send_msg(sock, payload)
    return IA.recv_msg(sock)

results = []
def check(name, ok, detail=""):
    results.append((name, ok, detail))
    print(f"  {name:52} {'PASS' if ok else 'FAIL'} {detail}")

try:
    # 1 happy path
    s = connect(); r = hello(s)
    ok_hello = bool(r and r.get("ok") and r.get("nonce"))
    check("1. hello issues a one-use nonce", ok_hello)
    r = send(s, peer, "the wire works", r["nonce"])
    check("2. allowlisted + signed message accepted", bool(r and r.get("ok")), str(r.get("id", "")))
    s.close()

    # 3 unknown sender
    s = connect(); r = hello(s)
    r = send(s, stranger, "let me in", r["nonce"])
    check("3. unknown sender refused", bool(r and not r.get("ok")) and "allowlist" in str(r.get("error")), str(r.get("error"))[:40])
    s.close()

    # 4 tampered signature
    s = connect(); r = hello(s)
    nonce = base64.b64decode(r["nonce"])
    good = peer.sign(nonce + b"tampered")
    bad = bytes([good[0] ^ 1]) + good[1:]
    r = send(s, peer, "tampered", r["nonce"], sig=bad)
    check("4. tampered signature refused", bool(r and not r.get("ok")) and "signature" in str(r.get("error")), str(r.get("error"))[:40])
    s.close()

    # 5 nonce replay
    s = connect(); r = hello(s)
    tok = r["nonce"]
    first = send(s, peer, "replay one", tok)
    second = send(s, peer, "replay two", tok)
    check("5. reused nonce refused", bool(first and first.get("ok")) and bool(second and not second.get("ok")), str(second.get("error"))[:40])
    s.close()

    # 6 oversized frame
    s = connect(); hello(s)
    try:
        s.sendall((IA.MAX_FRAME + 1).to_bytes(4, "big"))
        r = IA.recv_msg(s)
        refused = r is None
    except OSError:
        refused = True
    check("6. oversized frame refused", refused)
    s.close()
finally:
    daemon.terminate()
    try:
        daemon.wait(timeout=5)
    except subprocess.TimeoutExpired:
        daemon.kill()

failed = [n for n, ok, _ in results if not ok]
print(f"\n  {len(results) - len(failed)} passed, {len(failed)} failed")
sys.exit(1 if failed else 0)
