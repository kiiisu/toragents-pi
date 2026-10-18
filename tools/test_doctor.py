"""doctor must not call a message PENDING when nothing will ever deliver it.

Three states look identical from outside and mean different things:
  PENDING      waiting for the next poll          -> will arrive
  for <session> addressed to another window       -> not this session's
  UNROUTABLE   untargeted mail under RECEIVE=targeted -> never, under this config
"""
import json, os, subprocess, sys, tempfile

HERE = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))

def run(receive, state, extension_reports=None):
    """extension_reports: what the pi process published, or None if it never ran.

    doctor's own env is deliberately allowed to disagree with it: that is the
    real situation (pi launched from a shell that had the var, doctor run from a
    fresh terminal), and it is the case that made the labels lie.
    """
    env = dict(os.environ, TORAGENTS_HOME=state, TORAGENTS_STATE=os.path.join(state, "state"),
               PI_SESSION_ID="01a14a4c-aaaa", TORAGENTS_SESSION="pi-01a14a4c")
    if receive:
        env["TORAGENTS_RECEIVE"] = receive
    info = os.path.join(state, "state", "extension.json")
    if extension_reports is None:
        if os.path.exists(info):
            os.unlink(info)
    else:
        with open(info, "w") as f:
            json.dump({"session": "pi-01a14a4c", "receive": extension_reports,
                       "pid": 1, "at": 1792325000000}, f)
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
# doctor's shell says "all" (nothing exported); the extension reported targeted.
# Before this, the label trusted the shell and printed PENDING.
shell_says_all_ext_says_targeted = run(None, H, extension_reports="targeted")
# and the inverse: doctor's shell exports targeted, but pi resolved "all"
shell_says_targeted_ext_says_all = run("targeted", H, extension_reports="all")

def line_for(out, mid):
    # Match the message row, not any line that happens to contain the id as a
    # substring: "a1" is inside the session name pi-01a14a4c, and matching
    # loosely made this test read the wrong line and fail for the wrong reason.
    for l in out.splitlines():
        parts = l.split()
        if parts and parts[0] == mid:
            return l
    return ""

checks = [
    ("delivered message labelled delivered", "delivered" in open_mode),
    ("untargeted is PENDING by default", "a1" in open_mode and "PENDING" in open_mode),
    ("mail for another session is labelled", "for pi-99999" in open_mode),
    ("untargeted becomes UNROUTABLE in targeted mode", "UNROUTABLE" in quiet_mode),
    ("the routed message is not called unroutable",
     "c3" in quiet_mode and "UNROUTABLE" not in line_for(quiet_mode, "c3")),
    ("label follows the EXTENSION, not doctor's shell 1",
     "UNROUTABLE" in line_for(shell_says_all_ext_says_targeted, "a1")),
    ("label follows the EXTENSION, not doctor's shell 2",
     "PENDING" in line_for(shell_says_targeted_ext_says_all, "a1")
     and "UNROUTABLE" not in line_for(shell_says_targeted_ext_says_all, "a1")),
    ("doctor says where the resolution came from",
     "extension.json" in shell_says_all_ext_says_targeted
     and "from env" in open_mode),
]
bad = [n for n, ok in checks if not ok]
for n, ok in checks:
    print(f"  {n:46} {'PASS' if ok else 'FAIL'}")
print(f"\n  {len(checks) - len(bad)} passed, {len(bad)} failed")
sys.exit(1 if bad else 0)
