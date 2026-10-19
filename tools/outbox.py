#!/usr/bin/env python3
"""outbox.py -- a pending message that survives the process that tried to send it.

`inbox_agent.py send` is a one-shot: it dials, and if the peer's onion has no
descriptor yet (which is the normal state of a brand-new peer for about three
minutes -- measured, see README) the call fails and the body, which lived in a
shell variable or a /tmp file, is somebody's problem again. A person will not
retype it and an agent will not remember it. So: queue first, send second.

    outbox add --to <peer> --body-file <path>     # durable, idempotent
    outbox flush [--until SECONDS]                # try each, delete only on success
    outbox list                                   # what is still pending, and why

Guarantees, and the honest edges of each:

  never lose a body      the body is inside the item file, written atomically
                         (tmp + os.replace) before add returns. A failed send
                         leaves the item in place with its attempt count and the
                         reason. Nothing deletes an item except a success.
  never double-send      two flushers cannot both send one item: each item is
                         claimed with O_EXCL first. A claim older than the stale
                         window (a flusher that was killed mid-send) can be taken
                         over, which is the one place a duplicate is possible --
                         see "ambiguous" below.
  supervisor-safe        exit 0 = nothing pending, 2 = items remain, 1 = broken.
                         A loop can `while outbox flush --until 30; do sleep 10; done`
                         and stop when it gets 0.

Ambiguity is the interesting case. Exit code alone cannot tell "the peer never
heard me" (safe to retry) from "the peer accepted it and my process died before
the reply arrived" (a retry duplicates). So an item whose send did not return a
verdict is marked `ambiguous: true` and is NOT retried by default; `flush
--retry-ambiguous` opts in. Sending is at-least-once by nature, and pretending
otherwise would be the kind of quiet lie this project keeps finding in other
people's code.
"""
import argparse
import hashlib
import json
import os
import shlex
import subprocess
import sys
import time

HERE = os.path.dirname(os.path.abspath(__file__))
DEFAULT_SEND = os.path.join(os.path.dirname(HERE), "inbox_agent.py")
CLAIM_STALE_S = 300.0        # a claim older than this belonged to a dead flusher
ITEM_TIMEOUT_S = 420.0       # the CLI's own dial ladder tops out around 390s


# --------------------------------------------------------------------- state --
def state_dir(explicit=None):
    return (explicit or os.environ.get("TORAGENTS_STATE")
            or os.path.join(os.path.expanduser("~"), ".toragents", "state"))


def paths(state):
    outbox = os.path.join(state, "outbox")
    return {"outbox": outbox,
            "claims": os.path.join(outbox, ".claims"),
            "tmp": os.path.join(outbox, ".tmp")}


def ensure(state):
    p = paths(state)
    for d in p.values():
        os.makedirs(d, mode=0o700, exist_ok=True)
    return p


def atomic_write(path, obj):
    tmp = f"{path}.tmp.{os.getpid()}"
    with open(tmp, "w", encoding="utf-8") as f:
        json.dump(obj, f, indent=2, sort_keys=True)
        f.flush()
        os.fsync(f.fileno())
    os.replace(tmp, path)


def items(state):
    """Pending items, oldest first. A name is <ts_ms>-<sha8>.json."""
    p = paths(state)
    try:
        names = sorted(n for n in os.listdir(p["outbox"]) if n.endswith(".json"))
    except FileNotFoundError:
        return []
    return [os.path.join(p["outbox"], n) for n in names]


def load(path):
    with open(path, encoding="utf-8") as f:
        return json.load(f)


# ----------------------------------------------------------------------- add --
def cmd_add(a, state):
    p = ensure(state)
    try:
        with open(a.body_file, "rb") as fh:
            raw = fh.read()
    except OSError as exc:
        sys.exit(f"cannot read body file {a.body_file}: {exc}")
    body = raw.decode("utf-8-sig")            # a Windows editor's BOM is not a message
    if not body.strip():
        sys.exit("refusing to queue an empty body")

    digest = hashlib.sha256(body.encode("utf-8")).hexdigest()
    for existing in items(state):
        try:
            if load(existing).get("sha256") == digest and load(existing).get("to") == a.to:
                print(f"already pending: {existing}")
                print(f"  sha256 {digest[:16]}... to {a.to} -- not queued twice")
                return 0
        except (OSError, ValueError):
            continue

    now = time.time()
    # Name is <ts>-<sha8>, but two different messages can share a second (a loop
    # queuing several) or even a body hash (same text to two peers), so a
    # collision must not silently overwrite -- that would destroy one body.
    base = f"{int(now * 1000)}-{digest[:8]}"   # ms so sorting by name == arrival order
    path = os.path.join(p["outbox"], base + ".json")
    n = 1
    while os.path.exists(path):
        path = os.path.join(p["outbox"], f"{base}-{n}.json")
        n += 1
    atomic_write(path, {"to": a.to, "body": body, "sha256": digest,
                        "created": now, "attempts": 0,
                        "last_error": None, "last_attempt": None,
                        "ambiguous": False, "sent_id": None})
    print(f"queued {path}")
    print(f"  {len(body)} chars to {a.to}, sha256 {digest[:16]}...")
    return 0


# --------------------------------------------------------------------- flush --
def claim(state, path, force=False):
    """Take ownership of one item, atomically. Returns the claim path or None.

    This is the whole defence against two flushers (or a supervisor plus a
    human) sending the same body twice. O_EXCL is the only primitive that makes
    "I own this" atomic across processes; a lock in a file we then delete is not
    a lock (this exact bug shipped once already).
    """
    p = paths(state)
    name = os.path.basename(path)
    cpath = os.path.join(p["claims"], name + ".claim")
    payload = json.dumps({"pid": os.getpid(), "at": time.time()}).encode()
    for attempt in (0, 1):
        try:
            fd = os.open(cpath, os.O_CREAT | os.O_EXCL | os.O_WRONLY, 0o600)
            with os.fdopen(fd, "wb") as f:
                f.write(payload)
            return cpath
        except FileExistsError:
            if attempt or not force:
                try:
                    age = time.time() - os.path.getmtime(cpath)
                except OSError:
                    age = 0.0
                if age < CLAIM_STALE_S:
                    return None
                # Stale claim: a flusher died holding it. Take it over.
                try:
                    os.replace(cpath, cpath + f".stale.{int(time.time())}")
                except OSError:
                    return None
                continue
    return None


def release(cpath):
    try:
        os.unlink(cpath)
    except OSError:
        pass


def send_command():
    """The command that actually sends. Overridable so tests can drive it."""
    env = os.environ.get("TORAGENTS_SEND_CMD")
    if env:
        return shlex.split(env)
    return [sys.executable, DEFAULT_SEND, "send"]


def try_item(state, path, timeout, force_claim=False):
    """Returns (outcome, info) where outcome is 'sent' | 'failed' | 'ambiguous' | 'claimed'."""
    p = paths(state)
    cpath = claim(state, path, force=force_claim)
    if cpath is None:
        return "claimed", {"error": "another flusher owns this item"}
    body_path = os.path.join(p["tmp"], os.path.basename(path) + ".body")
    try:
        item = load(path)
        with open(body_path, "w", encoding="utf-8") as f:
            f.write(item["body"])                 # exactly the bytes that were queued
        cmd = send_command() + ["--to", item["to"], "--body-file", body_path]
        started = time.time()
        try:
            proc = subprocess.run(cmd, capture_output=True, text=True, timeout=timeout)
            out, err, code = proc.stdout or "", proc.stderr or "", proc.returncode
        except subprocess.TimeoutExpired as exc:
            # No verdict: the peer may well have accepted it.
            out = (exc.stdout or b"").decode("utf-8", "replace") if isinstance(exc.stdout, bytes) else (exc.stdout or "")
            err = (exc.stderr or b"").decode("utf-8", "replace") if isinstance(exc.stderr, bytes) else (exc.stderr or "")
            item.update(attempts=item.get("attempts", 0) + 1, ambiguous=True,
                        last_attempt=time.time(),
                        last_error=f"TIMEOUT after {timeout:.0f}s -- may or may not have arrived")
            atomic_write(path, item)
            return "ambiguous", {"error": item["last_error"], "elapsed": time.time() - started}

        elapsed = time.time() - started
        sent_id = None
        verdict_ok = False
        if code == 0:
            try:
                reply = json.loads(out)
                if isinstance(reply, dict) and reply.get("ok") is True:
                    verdict_ok = True
                    sent_id = reply.get("id")
            except ValueError:
                verdict_ok = False
        if verdict_ok:
            # Record the verdict BEFORE deleting, so a crash here cannot make the
            # item look unsent and get it sent twice.
            item.update(attempts=item.get("attempts", 0) + 1, sent_id=sent_id,
                        last_attempt=time.time(), last_error=None, ambiguous=False)
            atomic_write(path, item)
            os.unlink(path)
            return "sent", {"id": sent_id, "elapsed": elapsed}

        if code == 0:
            # Exit 0 without a parsed {"ok": true} is NOT a delivery. A wrapper
            # that swallows the reply must not make the outbox throw the body away.
            head = "exit 0 but no ok verdict from the sender"
        else:
            head = ""
        # Keep the FIRST of stdout (where the CLI puts the error) and the LAST of
        # stderr (where the three-minute hint lands). Tail-slicing the join would
        # drop the actual reason whenever stderr is chatty.
        pieces = [head, (out or "").strip()[:200], (err or "").strip()[-200:]]
        tail = " | ".join(p for p in pieces if p)[:500]
        item.update(attempts=item.get("attempts", 0) + 1, last_attempt=time.time(),
                    last_error=f"exit {code}: {tail}")
        atomic_write(path, item)
        return "failed", {"error": item["last_error"], "elapsed": elapsed, "code": code}
    finally:
        try:
            os.unlink(body_path)
        except OSError:
            pass
        release(cpath)


def cmd_flush(a, state):
    p = ensure(state)
    pending = items(state)
    if not pending:
        print("outbox empty -- nothing to send")
        return 0

    started = time.time()
    sent = failed = ambiguous = skipped = 0
    print(f"pending {len(pending)} item(s) in {p['outbox']}")
    for path in pending:
        if a.until is not None:
            left = a.until - (time.time() - started)
            if left <= 0:
                print(f"  budget of {a.until:.0f}s spent -- stopping, items remain")
                break
        else:
            left = ITEM_TIMEOUT_S
        try:
            item = load(path)
        except (OSError, ValueError) as exc:
            print(f"  CORRUPT {os.path.basename(path)}: {exc} -- left in place, not deleted")
            failed += 1
            continue
        name = os.path.basename(path)
        if item.get("ambiguous") and not a.retry_ambiguous:
            print(f"  skip {name}: previous attempt was AMBIGUOUS ({item.get('last_error')})")
            print("       -- add --retry-ambiguous if a duplicate is acceptable")
            skipped += 1
            continue

        outcome, info = try_item(state, path, timeout=ITEM_TIMEOUT_S)
        if outcome == "sent":
            sent += 1
            print(f"  sent {name}  id={info.get('id')}  in {info['elapsed']:.1f}s")
        elif outcome == "failed":
            failed += 1
            print(f"  FAILED {name} (attempt {item.get('attempts', 0) + 1}) -- kept")
            print(f"         {info['error']}")
        elif outcome == "ambiguous":
            ambiguous += 1
            print(f"  AMBIGUOUS {name} -- kept and marked; will not retry without --retry-ambiguous")
            print(f"         {info['error']}")
        else:
            skipped += 1
            print(f"  skip {name}: {info['error']}")

    remaining = len(items(state))
    print(f"\nsent {sent}, failed {failed}, ambiguous {ambiguous}, skipped {skipped}; "
          f"{remaining} still pending, {time.time() - started:.1f}s elapsed")
    if remaining:
        print("  next flush will retry the non-ambiguous ones")
        return 2
    return 0


# ---------------------------------------------------------------------- list --
def cmd_list(a, state):
    pending = items(state)
    if not pending:
        print("outbox empty")
        return 0
    for path in pending:
        try:
            it = load(path)
        except (OSError, ValueError) as exc:
            print(f"  CORRUPT {os.path.basename(path)}: {exc}")
            continue
        flag = " AMBIGUOUS" if it.get("ambiguous") else ""
        print(f"  {os.path.basename(path)}  to={it.get('to')}  attempts={it.get('attempts', 0)}{flag}")
        print(f"     {len(it.get('body') or '')} chars, created {time.strftime('%H:%M:%S', time.localtime(it.get('created', 0)))}")
        if it.get("last_error"):
            print(f"     last: {it['last_error']}")
    return 2


def main(argv=None):
    ap = argparse.ArgumentParser(description=__doc__,
                                 formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--state", default=None, help="state dir (default TORAGENTS_STATE or ~/.toragents/state)")
    sub = ap.add_subparsers(dest="cmd", required=True)

    ad = sub.add_parser("add", help="queue a body for a peer")
    ad.add_argument("--to", required=True, help="peer label as inbox_agent.py knows it")
    ad.add_argument("--body-file", required=True)
    ad.set_defaults(fn=cmd_add)

    fl = sub.add_parser("flush", help="try every pending item")
    fl.add_argument("--until", type=float, default=None,
                    help="total wall-clock budget in seconds; gates STARTING items only, "
                         "so a send already in flight may run up to its own 420s timeout")
    fl.add_argument("--retry-ambiguous", action="store_true",
                    help="also retry items whose last attempt may have arrived (duplicates possible)")
    fl.set_defaults(fn=cmd_flush)

    ls = sub.add_parser("list", help="show what is pending and why")
    ls.set_defaults(fn=cmd_list)

    a = ap.parse_args(argv)
    return a.fn(a, state_dir(a.state))


if __name__ == "__main__":
    sys.exit(main())
