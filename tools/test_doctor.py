"""doctor must not call a message PENDING when nothing will ever deliver it.

Three states look identical from outside and mean different things:
  PENDING      waiting for the next poll          -> will arrive
  for <session> addressed to another window       -> not this session's
  UNROUTABLE   untargeted mail under RECEIVE=targeted -> never, under this config
"""
import json, os, subprocess, sys, tempfile

HERE = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))

def run(receive, state):
    env = dict(os.environ, TORAGENTS_HOME=state, TORAGENTS_STATE=os.path.join(state, "state"),
               PI_SESSION_ID="01a14a4c-aaaa", TORAGENTS_SESSION="pi-01a14a4c")
    if receive:
        env["TORAGENTS_RECEIVE"] = receive
    return subprocess.run([sys.executable, os.path.join(HERE, "inbox_agent.py"), "doctor"],
                          env=env, capture_output=True, text=True).stdout

H = tempfile.mkdtemp(prefix="toragents-doctor-")
inbox = os.path.join(H, "state", "inbox")
os.makedirs(inbox)
for name, msg in {
    "1-a1.json": {"id": "a1", "received": 1, "from_label": "peer"},
    "2-b2.json": {"id": "b2", "received": 2, "from_label": "peer", "to_session": "pi-99999999"},
    "3-c3.json": {"id": "c3", "received": 3, "from_label": "peer", "to_session": "pi-01a14a4c"},
}.items():
    with open(os.path.join(inbox, name), "w") as f:
        json.dump(msg, f)
with open(os.path.join(H, "state", "announced.json"), "w") as f:
    json.dump(["c3"], f)

open_mode = run(None, H)
quiet_mode = run("targeted", H)

checks = [
    ("delivered message labelled delivered", "delivered" in open_mode),
    ("untargeted is PENDING by default", "a1" in open_mode and "PENDING" in open_mode),
    ("mail for another session is labelled", "for pi-99999" in open_mode),
    ("untargeted becomes UNROUTABLE in targeted mode", "UNROUTABLE" in quiet_mode),
    ("the routed message is not called unroutable", "c3" in quiet_mode and "UNROUTABLE" not in
     [l for l in quiet_mode.splitlines() if "c3" in l][0]),
]
bad = [n for n, ok in checks if not ok]
for n, ok in checks:
    print(f"  {n:46} {'PASS' if ok else 'FAIL'}")
print(f"\n  {len(checks) - len(bad)} passed, {len(bad)} failed")
sys.exit(1 if bad else 0)
