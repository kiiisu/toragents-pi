#!/usr/bin/env python3
"""One-command reproduction: does the digest op accept a properly signed frame?

    python3 tools/repro_digest_dead.py [repo-root]

Exit 0 = accepted (the positive path runs). Exit 3 = refused (the feature is
dead). Exit 2 = the harness could not import. Prints the receiver's own words
either way, so the answer is the program's output, not a summary of it.

No Tor: a real `inbox_agent.py serve` on loopback, a real second identity, the
real framed protocol. Exists because a defect report that needs a paragraph of
reconstruction is half a report.
"""
import sys
import os

root = sys.argv[1] if len(sys.argv) > 1 else os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
os.chdir(root)
sys.path.insert(0, "tools")
sys.path.insert(0, ".")

try:
    import test_tier2_acceptance as T
    import wire
except Exception as e:                       # noqa: BLE001 - the reason is the output
    print("cannot import the harness:", e)
    raise SystemExit(2)

a = T.Node("sender")
b = T.Node("receiver")
b.allow(a)
b.start()
try:
    d = T.dg([T.item()])
    nonce = b.hello()
    reply = b.raw_op({"op": "digest", "v": 1, "nonce": nonce,
                      "pubkey": a.keys.public().to_dict(),
                      "dsig": T.dsig_for(a.keys, wire.b64d(nonce), d), "dg": d})
    print("receiver said:", reply)
    raise SystemExit(0 if (reply or {}).get("ok") else 3)
finally:
    b.stop()
    a.tmp.cleanup()
