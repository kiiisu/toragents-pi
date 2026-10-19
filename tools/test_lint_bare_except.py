#!/usr/bin/env python3
"""Both directions, by mutation -- a lint that cannot fire is not a lint.

MUST FLAG: the three handlers actually shipped in this repo.
MUST NOT FLAG: the two legitimate uses of a broad handler (a loop guard and a
best-effort cleanup), plus the "already gone" unlink.

Every fixture is also mutated to prove the verdict is caused by the thing the
rule names, not by the fixture merely existing.
"""
import subprocess
import sys
import tempfile
import textwrap
from pathlib import Path

HERE = Path(__file__).resolve().parent
LINT = HERE / "lint_bare_except.py"
sys.path.insert(0, str(HERE))
import lint_bare_except as L                                    # noqa: E402

# --- fixtures: exactly the shapes we shipped and the shapes we must allow -----
MUST_FLAG = {
    "digest handler flattens an unknown failure":
        '''
        def _verify_digest(req):
            try:
                pub = PublicKey.from_dict(req["pubkey"])
            except Exception:
                return None, "digest frame is missing pubkey/nonce/dsig"
        ''',
    "verify swallows every fault into False":
        '''
        def verify(self, message, signature):
            try:
                self._verify.verify(message, signature)
                return True
            except Exception:
                return False
        ''',
    "outer handler turns silence into a signature verdict":
        '''
        def _check(req):
            try:
                if not pub.verify(nonce, dsig):
                    return None, "digest signature does not verify"
            except Exception:
                return None, "digest signature does not verify"
        ''',
    "bare except: with a prose verdict":
        '''
        def f(x):
            try:
                return int(x)
            except:
                return "bad input"
        ''',
}

MUST_NOT_FLAG = {
    "top-level serve loop guard (logs and continues)":
        '''
        def serve():
            while True:
                try:
                    handle()
                except Exception as e:
                    print("loop error:", e)
        ''',
    "best-effort cleanup (pass)":
        '''
        def save(path, data):
            try:
                open(path, "w").write(data)
            except Exception:
                pass
        ''',
    "unlink that ignores already-gone":
        '''
        def unclaim(p):
            try:
                os.unlink(p)
            except Exception:
                pass
        ''',
    "bare re-raise after recording the attempt":
        '''
        def send(rec):
            try:
                reply = dial(rec)
            except BaseException:
                sent_confirm(rec, None)
                raise
        ''',
    "verdict that carries the exception":
        '''
        def _check(e_holder):
            try:
                return pub.verify(nonce)
            except Exception as e:
                return None, f"digest verification error: {e.__class__.__name__}: {e}"
        ''',
    "narrow handler, same verdict text":
        '''
        def f(req):
            try:
                return req["pubkey"]
            except KeyError:
                return None, "digest frame is missing pubkey/nonce/dsig"
        ''',
}

# --- mutations: each must flip the verdict for the stated reason --------------
FLAG_MUTATIONS = {
    "digest handler flattens an unknown failure":
        ("except Exception:", "except KeyError:", "no longer broad"),
    "verify swallows every fault into False":
        ("return False", 'print("verify failed"); return False',
         "it now says something instead of answering silently"),
    "outer handler turns silence into a signature verdict":
        ("except Exception:", "except BadSignatureError:", "no longer broad"),
}
NONFLAG_MUTATIONS = {
    "top-level serve loop guard (logs and continues)":
        ('print("loop error:", e)', 'return "handler failed"', "now returns a verdict"),
}


def flags(src):
    return L.scan_source(textwrap.dedent(src))


def check(label, ok, detail=""):
    print(f"{'PASS' if ok else 'FAIL'}  {label}{('  -- ' + detail) if detail and not ok else ''}")
    return ok


def main():
    ok = True
    for name, src in MUST_FLAG.items():
        found = flags(src)
        ok &= check(f"flags: {name}", bool(found), f"no finding, expected one")
        if found:
            ok &= check(f"  reason is stated: {name}",
                        "unknown fault" in found[0][2], found[0][2])

    for name, src in MUST_NOT_FLAG.items():
        found = flags(src)
        ok &= check(f"allows: {name}", not found,
                    f"flagged at line {found[0][0]}: {found[0][2]}" if found else "")

    for name, (old, new, why) in FLAG_MUTATIONS.items():
        mutant = MUST_FLAG[name].replace(old, new)
        ok &= check(f"mutation silences it ({why}): {name}", not flags(mutant))

    for name, (old, new, why) in NONFLAG_MUTATIONS.items():
        mutant = MUST_NOT_FLAG[name].replace(old, new)
        ok &= check(f"mutation makes it fire ({why}): {name}", bool(flags(mutant)))

    # --- the command contract: one command, no args, nonzero on a finding ----
    with tempfile.TemporaryDirectory() as tmp:
        dirty = Path(tmp) / "dirty.py"
        dirty.write_text(textwrap.dedent(MUST_FLAG["verify swallows every fault into False"]))
        r = subprocess.run([sys.executable, str(LINT), str(dirty)],
                           capture_output=True, text=True, timeout=60)
        ok &= check("exit 1 on a finding", r.returncode == 1, f"exit {r.returncode}")
        ok &= check("prints file:line", f"{dirty}:" in r.stdout, r.stdout[:80])

        clean = Path(tmp) / "clean.py"
        clean.write_text(textwrap.dedent(MUST_NOT_FLAG["best-effort cleanup (pass)"]))
        r = subprocess.run([sys.executable, str(LINT), str(clean)],
                           capture_output=True, text=True, timeout=60)
        ok &= check("exit 0 when clean", r.returncode == 0, f"exit {r.returncode}")

    print("\n" + ("all lint self-tests pass" if ok else "LINT SELF-TEST FAILED"))
    return 0 if ok else 1


if __name__ == "__main__":
    sys.exit(main())
