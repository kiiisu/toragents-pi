#!/usr/bin/env python3
"""Ban the bare except that hides a fault behind a confident verdict.

The rule is NOT "no `except Exception`". A top-level serve loop legitimately
needs one, and cleanup paths legitimately ignore "already gone". The defect is
narrower and has bitten this repo three times:

    an over-broad handler whose body PRODUCES A VERDICT -- a string going on the
    wire, or a bare True/False/None -- for a fault it does not understand.

Why that is worse than silence: the handler relabels "this code is broken" as
"the peer sent something wrong". The peer then debugs their own machine, and the
crash becomes invisible because the caller receives an ordinary-looking answer.
Three shipped instances, all found by tripping over them:

  1. _verify_digest flattened an AttributeError into "missing pubkey/nonce/dsig"
  2. PublicIdentity.verify swallowed every exception into False
  3. the outer signature check turned that False into "signature does not verify"

The principle, in kisu's words (2026-10-19), because it is better than mine: an
except clause is a CLAIM about how the guarded block can fail. A bare except
claims "I understand every failure", which is never true, so it always lies about
at least one of them. Narrowing it is not tidiness -- it is making the claim true.
Catch what the block can actually raise; let the rest escape and say their own
name.

Usage:  python3 tools/lint_bare_except.py [paths...]     (no args = this repo)
Exit:   0 = clean, 1 = findings, 2 = the lint itself failed.
"""
import ast
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent

# Names that make a handler "bare" in the sense that matters: it will catch
# faults nobody anticipated.
BROAD = {"Exception", "BaseException"}

LOGGING = {"print", "log", "warn", "warning", "error", "debug", "info",
           "append", "write", "notify", "emit"}
VERDICT_CONSTANTS = {"True", "False", "None"}


def _is_broad(handler):
    if handler.type is None:                       # except:
        return "bare except:"
    t = handler.type
    name = t.id if isinstance(t, ast.Name) else getattr(t, "attr", None)
    if name in BROAD:
        return f"except {name}"
    if isinstance(t, ast.Tuple):
        for elt in t.elts:
            n = elt.id if isinstance(elt, ast.Name) else getattr(elt, "attr", None)
            if n in BROAD:
                return f"except (..., {n})"
    return None


def _mentions_call(node, names):
    for sub in ast.walk(node):
        if isinstance(sub, ast.Call):
            f = sub.func
            fn = getattr(f, "id", None) or getattr(f, "attr", None)
            if fn in names:
                return True
    return False


def _verdict_shape(node):
    """A value that reads as an answer rather than a log line."""
    if isinstance(node, ast.Constant):
        if isinstance(node.value, str) and node.value.strip():
            return "a prose verdict"
        if node.value is True or node.value is False or node.value is None:
            return "a bare verdict constant"
        return None
    if isinstance(node, ast.JoinedStr):
        return "a formatted verdict string"
    if isinstance(node, ast.Dict):
        keys = {k.value for k in node.keys if isinstance(k, ast.Constant)}
        if {"ok", "error"} & keys:
            return "a result envelope"
        return None
    if isinstance(node, ast.Tuple):
        for elt in node.elts:
            why = _verdict_shape(elt)
            if why:
                return why
        return None
    if isinstance(node, ast.Call):
        f = node.func
        fn = getattr(f, "id", None) or getattr(f, "attr", None)
        if fn in {"str", "format"}:
            return "a constructed verdict"
    return None


def _names_the_fault(handler, name):
    """Does the handler's own text carry the exception, or replace it?

    "digest verification error: AttributeError: ..." tells the caller what broke.
    "digest signature does not verify" tells them it was their fault. Only the
    second is the defect.
    """
    for sub in ast.walk(handler):
        if isinstance(sub, ast.Name) and sub.id == name:
            return True          # f-string / dict / + built from the exception
        if isinstance(sub, ast.Call):
            fn = getattr(sub.func, "id", None) or getattr(sub.func, "attr", None)
            if fn in {"str", "repr", "format"} and any(
                    isinstance(a, ast.Name) and a.id == name for a in sub.args):
                return True
    return False


def scan_source(src, filename="<fixture>"):
    """Return a list of findings: (line, handler_text, why)."""
    try:
        tree = ast.parse(src, filename=filename)
    except SyntaxError as e:
        return [(e.lineno or 0, "<syntax error>", f"could not parse: {e.msg}")]
    out = []
    for node in ast.walk(tree):
        if not isinstance(node, ast.Try):
            continue
        for handler in node.handlers:
            broad = _is_broad(handler)
            if not broad:
                continue
            body = handler.body
            if _mentions_call(handler, LOGGING):
                continue                      # it said something; not a silent lie
            if any(isinstance(x, ast.Raise) and x.exc is None for x in ast.walk(handler)):
                continue                      # bare re-raise: causality preserved
            if handler.name and _names_the_fault(handler, handler.name):
                continue                      # the verdict carries the actual error
            verdict = None
            for stmt in body:
                if isinstance(stmt, ast.Return) and stmt.value is not None:
                    verdict = _verdict_shape(stmt.value)
                elif isinstance(stmt, ast.Raise):
                    verdict = verdict or "a substituted exception"
                if verdict:
                    break
            if not verdict:
                continue                      # pass / continue / cleanup: allowed
            first = ast.unparse(body[0]).strip().splitlines()[0][:70]
            out.append((handler.lineno, first,
                        f"{broad} turns an unknown fault into {verdict}"))
    return out


def main(argv):
    targets = argv[1:] or [str(ROOT)]
    files = []
    for t in targets:
        p = Path(t)
        if p.is_dir():
            files.extend(sorted(x for x in p.rglob("*.py")
                                if ".git" not in x.parts and "mutants" not in x.parts))
        elif p.suffix == ".py":
            files.append(p)
    findings = 0
    scanned = 0
    for f in files:
        if f.name.startswith("test_lint_"):
            continue
        try:
            src = f.read_text(encoding="utf-8")
        except (OSError, UnicodeDecodeError) as e:
            print(f"{f}: cannot read: {e}", file=sys.stderr)
            continue
        scanned += 1
        for line, first, why in scan_source(src, str(f)):
            findings += 1
            print(f"{f}:{line}: {why}")
            print(f"    {first}")
    print(f"\nlint_bare_except: {scanned} file(s) scanned, {findings} finding(s)")
    return 1 if findings else 0


if __name__ == "__main__":
    try:
        sys.exit(main(sys.argv))
    except Exception as e:                             # the lint must not crash
        print(f"lint_bare_except: internal error: {e.__class__.__name__}: {e}",
              file=sys.stderr)
        sys.exit(2)
