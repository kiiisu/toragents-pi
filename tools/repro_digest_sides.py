#!/usr/bin/env python3
"""The live digest reconciles nothing. Reproduce in one command.

`dg_attach` builds our digest from the SENT store only, and hands those rows to
`dg_build`'s FIRST list (`a`). `dg_apply` anchors `a` against the receiver's SENT
store. But a row built from our sent store carries `peer_id` -- the id the PEER
minted for a body WE sent -- and on the peer's side that id lives in their INBOX,
not in their sent store. So `a` can never match, on any machine, ever:

    spec:  a = ids WE minted (bodies we got)   -> receiver looks in ITS sent
           b = ids YOU minted (bodies we sent) -> receiver looks in ITS inbox
    code:  dg_attach puts our sent rows in a, leaves b empty

Both boxes saw the same thing independently: every row `never-stored`, including
messages both sides hold on disk. The 28 green tests missed it because they call
dg_apply with hand-built digests -- nothing drove dg_attach -> wire -> dg_apply.

Exit 0 = the digest reconciled (fixed).  Exit 3 = every row never-stored (live bug).
"""
import hashlib
import json
import os
import sys
import tempfile

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
import inbox_agent as I                                             # noqa: E402

BODY = "the message both machines hold, byte for byte"
H = hashlib.sha256(BODY.encode()).hexdigest()[:32]
N = len(BODY.encode())
PEER_FP = "b" * 32
PEER_ID = "a986730406f8"        # the id the PEER minted when it received our body


def write(path, obj):
    os.makedirs(os.path.dirname(path), exist_ok=True)
    with open(path, "w") as f:
        json.dump(obj, f)


def main():
    root = tempfile.mkdtemp(prefix="dg-sides-")
    ours = os.path.join(root, "ours")          # we sent a body, the peer minted PEER_ID
    theirs = os.path.join(root, "theirs")      # the peer received it, and mints ids

    # our sent store: the record of the send, keyed by the id the peer minted
    write(os.path.join(ours, "sent", "1.json"),
          {"local_id": "loc000000001", "peer_id": PEER_ID, "state": "confirmed",
           "sha256": H, "len": N, "to": "peer"})

    # their inbox: the body we sent, stored under the id they minted
    write(os.path.join(theirs, "inbox", f"1792409421-{PEER_ID}.json"),
          {"id": PEER_ID, "received": 1792409421, "from_fingerprint": "a" * 32,
           "from_label": "peer", "from_onion": "x.onion", "body": BODY})

    dg = I.dg_attach(ours)
    a_ids = [it["id"] for it in dg.get("a", [])]
    b_ids = [it["id"] for it in dg.get("b", [])]
    print(f"dg_attach: a={a_ids} b={b_ids}")
    print(f"  expected under the spec: a=[] (we received nothing) b=[{PEER_ID!r}]")

    res = I.dg_apply(theirs, PEER_FP, dg)
    rows = res.get("rows") or {}
    states = {mid: (r.get("state"), r.get("why")) for mid, r in rows.items()}
    print(f"dg_apply on the receiver: {json.dumps(states, indent=2)}")

    if not rows:
        print("\nVERDICT: the digest carried nothing the receiver could anchor (dead).")
        return 3
    good = [mid for mid, (st, _) in states.items() if st == "delivered-both"]
    bad = [mid for mid, (st, _) in states.items() if st == "never-stored"]
    if good and not bad:
        print(f"\nVERDICT: reconciled {good} as delivered-both (alive).")
        return 0
    print(f"\nVERDICT: DEAD -- {len(bad)} row(s) never-stored although both sides hold the body.")
    print("        a row built from OUR sent store in `a` can never match on the peer,")
    print("        because `a` is looked up in the peer's INBOX.")
    return 3


if __name__ == "__main__":
    sys.exit(main())
