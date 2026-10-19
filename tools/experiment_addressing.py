#!/usr/bin/env python3
"""Two-window delivery experiment: who takes what, proven from files.

Why this exists: the report was "I opened a second pi session and it receives
messages too". Three shapes are possible and only files distinguish them:

  (a) two state dirs          -> two inboxes, both legitimate
  (b) one journal, two panes  -> a rendering defect, not a delivery bug
  (c) one journal, one inbox  -> real duplicate delivery, our bug

This script generates a temporary identity, allowlists it on the LOCAL daemon, and
sends three messages over the real framed protocol: one addressed to each session
label, one untargeted control. No Tor -- the daemon is the real receiver, and the
wire is the real wire. It prints receipts and leaves the rows for inspection.

A SINGLE OBSERVATION IS NOT REPRODUCIBLE, so pass/fail does not sit on it. Ownership
drift is a race: asserting "message X went to window A" would fail on an equally
correct rerun. The stable assertion is the LEDGER'S DISTRIBUTION -- which session
labels appear in claimed/ and how many messages each owns. The per-message mapping
below is a DESCRIPTION of one run, useful for reading it, useless as a verdict.

PRE-REGISTERED (written before any run, so the result cannot be reinterpreted):

  addressed window   -> a dir=in chat.jsonl row carrying to_session=<its label>
  other window       -> its INBOX gains nothing, though its pane may show a line
                        because it renders the shared journal
  inbox gains a file for the other window -> (c), real delivery bug
  inbox unchanged but pane shows it       -> (b), rendering defect

Refuses to run unless both labels are LIVE with a fresh record and a live pid: a
refusal produced by absent presence would look like the addressing rule misfiring.

Usage:
  python3 tools/experiment_addressing.py --label-a pi-xxxx --label-b pi-yyyy
  python3 tools/experiment_addressing.py --label-a pi-xxxx --label-b pi-yyyy --cleanup
"""
import argparse
import json
import os
import socket
import subprocess
import sys
import time

HERE = os.path.dirname(os.path.abspath(__file__))
REPO = os.path.dirname(HERE)
sys.path.insert(0, REPO)
import inbox_agent as ia  # noqa: E402


def state_dir():
    return os.environ.get("TORAGENTS_STATE") or os.path.join(ia.HOME, "state")


def numbers(tag):
    st = state_dir()
    chat = os.path.join(st, "chat.jsonl")
    inode = subprocess.run(["stat", "-c", "%i", chat], capture_output=True, text=True).stdout.strip()
    lines = 0
    if os.path.exists(chat):
        with open(chat, "rb") as f:
            lines = sum(1 for _ in f)
    inbox = len([n for n in os.listdir(os.path.join(st, "inbox")) if n.endswith(".json")]) \
        if os.path.isdir(os.path.join(st, "inbox")) else 0
    print(f"[{tag}] chat.jsonl inode={inode or 'none'} lines={lines} inbox_files={inbox}")
    return {"inode": inode, "lines": lines, "inbox": inbox}


def claims(tag):
    """Who WON each message. The claim file's first field is the session label.

    This is the field that separates ownership drift (two windows racing, each
    message going to exactly one of them, varying per message) from a rendering
    defect (one window wins everything, the other paints the same journal) and
    from two state dirs. Inbox counts alone cannot tell those apart.
    """
    d = os.path.join(state_dir(), "claimed")
    owners = {}
    if os.path.isdir(d):
        for name in os.listdir(d):
            try:
                with open(os.path.join(d, name)) as f:
                    parts = f.read().split()
            except OSError:
                continue
            if parts:
                owners[name] = parts[0]
    counts = {}
    for owner in owners.values():
        counts[owner] = counts.get(owner, 0) + 1
    print(f"[{tag}] claims by owner: {counts or '{}'} (total {len(owners)})")
    return owners


def live_gate(labels):
    live = ia.live_labels(state_dir())
    raw = {}
    d = os.path.join(state_dir(), "live")
    if os.path.isdir(d):
        for name in os.listdir(d):
            if name.endswith(".json"):
                try:
                    with open(os.path.join(d, name)) as f:
                        raw[name[:-5]] = json.load(f)
                except Exception:
                    pass
    print(f"live registry: {sorted(k for k, v in live.items() if v)}")
    for lab in labels:
        rec = raw.get(lab)
        if not live.get(lab):
            print(f"  VOID: {lab} is not live (record={rec})")
            print("        a refusal now would be absent presence, not a broken rule.")
            print("        /reload both windows and re-run.")
            return False
        age = (time.time() * 1000 - float(rec.get("at") or 0)) / 1000.0
        print(f"  ok: {lab} pid={rec.get('pid')} age={age:.1f}s")
    return True


def claim_winners_for(plan, claim_map):
    """Map our three message bodies to the session label that claimed them.

    Matching is by the id minted at receipt, which we do not know here; so we match
    on the bodies present in the inbox store, which do carry the body text. If that
    cannot be resolved, return None rather than guess -- a fabricated owner is worse
    than an admitted gap.
    """
    st = state_dir()
    inbox = os.path.join(st, "inbox")
    bodies = {}
    if os.path.isdir(inbox):
        for name in os.listdir(inbox):
            if not name.endswith(".json"):
                continue
            try:
                with open(os.path.join(inbox, name)) as f:
                    rec = json.load(f)
            except (OSError, ValueError):
                continue
            body = rec.get("body") or ""
            if body.startswith("EXPERIMENT-"):
                bodies[body] = name[:-5]
    out = []
    for body, _target in plan:
        mid = bodies.get(body)
        if mid is None or mid not in claim_map:
            return None
        out.append((body, claim_map[mid]))
    return out


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--label-a", required=True, help="session label of window A")
    ap.add_argument("--label-b", required=True, help="session label of window B")
    ap.add_argument("--onion", default="selftest.onion", help="onion the temp peer claims")
    ap.add_argument("--cleanup", action="store_true", help="remove the temp peer and exit")
    a = ap.parse_args()

    k = ia.AgentKeys.load() if hasattr(ia.AgentKeys, "load") else ia.keys(create=True)
    fp = k.public().fingerprint

    if a.cleanup:
        p = ia.peers()
        if fp in p:
            del p[fp]
            with open(ia.PEERS, "w") as f:
                json.dump(p, f, indent=2)
            print(f"removed temp peer {fp[:12]}")
        return 0

    # Order matters and is now asserted, not assumed: the gate runs BEFORE the
    # temp identity is allowlisted, so a voided run leaves the allowlist exactly
    # as it found it. The counts are printed either way so residue cannot hide.
    before_peers = len(ia.peers())
    print(f"allowlist: {before_peers} peer(s) before")
    if not live_gate([a.label_a, a.label_b]):
        after_peers = len(ia.peers())
        print(f"allowlist: {after_peers} peer(s) after (nothing sent, nothing added)")
        if after_peers != before_peers:
            print("  WARNING: the allowlist changed on a VOID run -- investigate before trusting any run")
        return 3

    subprocess.run([sys.executable, ia.__file__, "add-peer", fp,
                    "--onion", a.onion, "--label", "experiment"],
                   check=True, capture_output=True, text=True)
    print(f"temp peer {fp[:12]} allowlisted (remove with --cleanup)")
    print(f"allowlist: {len(ia.peers())} peer(s) after adding the temp identity")

    before = numbers("before")
    claims_before = claims("before")
    stamp = int(time.time())
    plan = [
        (f"EXPERIMENT-A-{stamp}", a.label_a),
        (f"EXPERIMENT-B-{stamp}", a.label_b),
        (f"EXPERIMENT-CONTROL-{stamp}", None),
    ]
    receipts = []
    for body, target in plan:
        s = socket.create_connection(("127.0.0.1", ia.LISTEN_PORT), timeout=15)
        try:
            ia.send_msg(s, {"op": "hello"})
            hello = ia.recv_msg(s)
            nonce = ia.b64d(hello["nonce"])
            payload = {"op": "send", "nonce": hello["nonce"], "body": body,
                       "pubkey": k.public().to_dict(),
                       "sig": ia.b64e(k.sign(nonce + body.encode("utf-8")))}
            if target:
                payload["to_session"] = target
            ia.send_msg(s, payload)
            reply = ia.recv_msg(s)
        finally:
            s.close()
        where = target or "UNTARGETED"
        receipts.append(reply)
        print(f"send -> {where}: {reply}")

    after = numbers("after")
    claims_after = claims("after")
    winners = claim_winners_for(plan, claims_after)
    print("\nPRE-REGISTERED EXPECTATIONS")
    print(f"  journal lines: {before['lines']} -> {after['lines']} "
          f"(+{after['lines'] - before['lines']}; three sends, two addressed + one control)")
    print(f"  inbox files:   {before['inbox']} -> {after['inbox']} "
          f"(+{after['inbox'] - before['inbox']})")
    print("  A addressed -> only A's window takes it; B's inbox gains nothing")
    print("  B addressed -> only B's window takes it")
    print("  control     -> the owning window takes it, other stays silent")
    print("  a SECOND file for one message anywhere = (c), real duplicate delivery")

    print("\nWHO WON EACH MESSAGE (the field that separates (d) from (a) and (b))")
    if winners is None:
        print("  could not map message ids to claims -- inspect declared/ or claimed/ by hand")
    else:
        labels = [w for _b, w in winners]
        for body, who in winners:
            print(f"  {body.split('-')[1]:<9} -> {who}   (addressed to {dict(plan)[body] or 'anyone'})")
        distinct = sorted(set(labels))
        dist = {d: labels.count(d) for d in distinct}
        print(f"  DISTRIBUTION: {dist}   distinct claimers: {len(distinct)}")
        print("  (per-message rows above describe THIS run only; a rerun may differ)")
        if len(distinct) == 1:
            print("  VERDICT: one claimer for every message -> the election is holding")
        else:
            print("  VERDICT: the stream SPLIT across windows -> ownership drift (d):")
            print("           two windows taking turns, each message delivered exactly once.")
            print("           On a build WITH the election this would be a finding about main.")
    print("  interpretation must carry (d): a split stream is not two state dirs")

    # The mapping's precondition, asserted rather than assumed: claims persist after
    # delivery (success never unclaims; the claim is the durable "who owns this").
    # If a message lands without a claim, that is a FINDING about the build, not a
    # mapping bug -- and it must not be reported as "not delivered".
    ok_sends = sum(1 for r in receipts if isinstance(r, dict) and r.get("ok"))
    new_claims = {k: v for k, v in claims_after.items() if k not in claims_before}
    print("\nCLAIM INTEGRITY (the mapping depends on this)")
    print(f"  accepted sends: {ok_sends}   new claim files: {len(new_claims)}   "
          f"total claims: {len(claims_before)} -> {len(claims_after)}")
    empty = [k for k, v in new_claims.items() if not str(v).strip()]
    if empty:
        print(f"  FINDING: {len(empty)} claim file(s) with no session field: {empty[:4]}")
    if ok_sends != len(new_claims):
        print(f"  FINDING: {ok_sends} accepted send(s) but {len(new_claims)} new claim(s).")
        print("  Either a build that UNCLAIMS on success, or a delivery with no claim.")
        print("  Claims are the durable ownership record by design; a delivered message")
        print("  without one cannot be attributed, and must be reported as UNKNOWN")
        print("  ownership -- never as 'not delivered'.")
    else:
        print("  consistent: every accepted send produced exactly one durable claim")
    return 0


if __name__ == "__main__":
    sys.exit(main())
