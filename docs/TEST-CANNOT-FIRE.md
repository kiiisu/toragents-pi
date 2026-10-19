# Which checks cannot fire

A test that cannot fail is worth less than no test: it buys confidence it has
not earned, and it looks identical to a test that works. This document records,
per check, the mutation that was injected to break the thing it claims to
protect, and whether the suite noticed.

Read-only audit. Nothing in this repo was changed to produce it; every mutation
lived in a copy under `/tmp/mut-audit/` and was discarded.

## Method

For each case:

1. copy the repo to a scratch directory (tests that import by `process.cwd()`
   see the copy, not the original);
2. run the test file unmutated, and record its summary — a mutation only counts
   as "fired" if the same file was green before it;
3. apply **one exact text replacement** to the source under test, asserting it
   matches exactly once, so a stale anchor is an error rather than a silent
   no-op;
4. re-run and compare **failure counts**, not the presence of the word "fail".

Step 4 is not pedantry. My first pass decided "did it fire?" by searching the
output for `failed`, which matches `0 failed` — so five cases that had clearly
fired were recorded as CANNOT-FIRE, and the audit's first table was wrong in the
direction that matters. Counting is the only reading that can't be fooled by the
word.

**Reporting rule (adopted 2026-10-19 after two violations in one day by the
author of this document):** a reported SHA means *the tree that was actually
executed*, plus whether that tree was dirty. `git log` names a commit; it does
not tell you what the working tree contained. So the report is either

    ran at <sha>, tree clean (`git status --porcelain` empty)

or

    ran at <sha> + uncommitted changes (the fix was in the tree, not the commit)

Anything else is a number that has outlived its referent. Twice in one day this
document's author reported a passing run against a commit that did not contain
the code that passed.

**Standard to aim at:** `sent verify` (the full-outbound store) recomputes every
hash and length, exits nonzero on disagreement, and its author proved it able to
fire with three mutations — truncate the body, drop the failure record, tamper
with a stored body. A check with a documented mutation is the standard; a check
without one is a hope.

## Worse than CANNOT FIRE: green over a feature that cannot work

The rows below test whether an assertion can fail. This class is different, and
it belongs first because it happened here, to this suite, and a mutation audit
would not have caught it.

Between `e125517` and `be6100b`, the digest could not work at all.
`_verify_digest()` called `PublicKey.from_dict()`, and `wire.py` defines no
`PublicKey` — it defines `PublicIdentity`, whose `from_dict` is at line 51. The
`AttributeError` was caught by a bare `except Exception` and reported as:

    digest frame is missing pubkey/nonce/dsig

So every `digest` frame was refused, every piggybacked `dg` was dropped
(`[inbox] dg dropped: …`), `seen.json` was never written from the wire, and
reconciliation never ran outside unit tests calling `dg_apply` directly. Four
commits passed over it. Twenty-five acceptance tests reported success.

**Why the suite was blind — two distinct causes, both structural:**

1. **Every assertion was a refusal.** `test_a_digest_frame_cannot_be_replayed`
   asserted that a digest is refused — and passed, because the bug guaranteed
   refusal. A test whose premise is also its subject cannot see the feature it
   claims to check. The piggyback test asserted the *body* survived and never
   that the `dg` landed, so "compat holds" was true while every digest was
   silently dropped.
2. **The error described the wrong field.** The handler flattened every
   exception into a statement about the caller's frame. It named a symptom
   (missing key) and pointed away from the cause (missing method). A message
   that confidently describes the wrong object is worse than silence, because
   silence at least invites investigation.

**The proof that the positive path now exists** is not that the assertions went
green. It is that the same frame which used to answer
`digest frame is missing pubkey/nonce/dsig` now answers `sender not on
allowlist` — it parses, it verifies a signature, and only then is refused on
policy. A frame that reaches a semantic refusal has travelled through the code.

**One command, any commit** (exit 0 = the op accepts a signed frame; exit 3 =
the op is dead; it prints the receiver's own words either way):

```bash
python3 tools/repro_digest_dead.py            # exit 0 = alive, 3 = dead
```

Measured: HEAD `252b9cf` -> `{'ok': True, 'error': '', 'applied': 1}`, exit 0.
The same script with `PublicIdentity.from_dict` reverted to `PublicKey.from_dict`
in a copy -> `{'ok': False, 'error': "handler: name 'PublicKey' is not defined"}`,
exit 3.

That second message is not the one this section opened with, and the difference
is itself evidence: the older commit swallowed the `NameError` into
"digest frame is missing pubkey/nonce/dsig", while the current code lets it
surface with its own name. The flattening got fixed; the missing method was the
bug underneath it.

**The rule this carries — checkable, not advice:**

> A refusal assertion is not evidence that the positive path runs.

The weaker form, "add positive tests", is a wish. The checkable form is: for
every feature, name the assertion that must fail if the feature's happy path
never executes, and then break the happy path to see whether the count moves.
An assertion that survives the feature being dead is not testing the feature.

## Results

| id | test file | claim it makes | clean | mutant | verdict |
|----|-----------|----------------|-------|--------|---------|
| M1 | `test_chat.ts` | the transcript records what was said, one line per direction | 16p/0f | 16p/0f | **CANNOT FIRE** |
| M2 | `test_stamp.ts` | a stamp that is not today carries a date | 7p/0f | 1p/6f | fires |
| M3 | `test_consumer.ts` | exactly one session delivers | 10p/0f | 9p/1f | fires |
| M4 | `test_wire.py` | a tampered signature is refused | 6p/0f | 5p/1f | fires |
| M5 | `test_outbox.py` | exit 0 with `{"ok": false}` is not a delivery | 17p/0f | 17p/0f | **CANNOT FIRE** |
| M6 | `test_doctor.py` | doctor never calls PENDING something nothing will deliver | 12p/0f | 9p/3f | fires |
| M7 | `test_stale.py` | the meta-guard catches a pinned threshold | 8f | 9f | fires |
| M8 | `test_pane_surface.ts` | rpc is handed an array, because a factory is dropped in silence | 19/19 pass | 12f/19 | fires |

(p = passed, f = failed. M7 and M8 are the two files I wrote earlier today; they
are included for completeness, not to flatter them.)

## Finding 1 — `test_chat.ts` guards the renderer, not the writer (M1)

The header says: *"The transcript must show what was SAID and nothing else: one
line per direction."* That is a claim about two components. The test exercises
one of them.

It writes its own fixture rows:

```ts
writeFileSync(join(STATE, "chat.jsonl"), rows.map(r => JSON.stringify(r)).join("\n") + "\n");
```

and then asserts on `renderChat()`. The writer, `logSaid()`, is never called. So
this mutation sails through:

```
toragents_watch.ts, logSaid():
- JSON.stringify({ at: Date.now(), dir, peer, id, body: ... })
+ JSON.stringify({ at: Date.now(), dir: dir === "in" ? "out" : "in", peer, id, body: ... })
```

Every message renders with the wrong arrow, and the suite reports 16 passed, 0
failed. The same applies to the writer's 2000-character truncation and to the
"never let the transcript break a delivery" `catch`.

Why it matters: the transcript is the human-visible record of a channel whose
whole purpose is that a message visibly arrived. A renderer test cannot tell
whether anything was recorded. Fixing it is a test that drives a real delivery
and then reads `chat.jsonl` — note that `test_sent_store.py` and
`test_pane_surface.ts` already touch that file, so a fix may only need to move
the assertion.

## Finding 2 — `test_outbox.py` cannot tell "sent" from "refused" at exit 0 (M5)

`tools/outbox.py` contains this comment, which is a specification of a hazard:

> Exit 0 without a parsed `{"ok": true}` is NOT a delivery. A wrapper that
> prints a refusal and returns 0 would otherwise delete the body.

The tests cover three of the four shapes: exit 0 with `{"ok": true}` (delivered),
exit 1 with `{"ok": false}` (failed), exit 0 with non-JSON (ambiguous). The
missing one is **exit 0 with valid JSON that says `ok: false`** — precisely the
wrapper the comment describes.

Mutating the gate to accept any JSON leaves the suite at 17p/0f. Proven directly
with a stub that prints `{"ok": false}` and exits 0:

```
REAL outbox:    flush exit=2   "failed 1 ... still pending"
MUTANT:         flush exit=0   "sent 1, failed 0, ambiguous 0 ... 0 still pending"
```

The mutant deletes the body. The suite cannot see the difference, because no test
produces that input.

Why it matters more than finding 1: the failure mode is silent data loss on a
peer message, and the code's own comment predicted the exact wrapper that
triggers it. A one-case addition to the existing stub (`mode == "okfalse"`:
print `{"ok": false, ...}`, `sys.exit(0)`) closes it.

## Recipes, so these can be re-applied mechanically

Each is one exact replacement in the named file. A month from now, "the mutant
that flipped the transcript direction" is not reproducible; these are.

```
M1  toragents_watch.ts
- JSON.stringify({ at: Date.now(), dir, peer, id, body: String(body).slice(0, 2000) })
+ JSON.stringify({ at: Date.now(), dir: dir === "in" ? "out" : "in", peer, id, body: String(body).slice(0, 2000) })
  expect: test_chat.ts still 16p/0f (cannot fire)

M2  toragents_watch.ts, stampFor()
- if (sameDay) return `${hh}:${mm}`;
+ return `${hh}:${mm}`;
  expect: test_stamp.ts 6 failures

M3  toragents_watch.ts, isConsumer()
- return true; // no election recorded: do not mute a working channel
+ return false; // MUTANT mutes the channel when no election exists
  expect: test_consumer.ts 1 failure ("a missing election file does not mute delivery")

M4  wire.py, AgentKeys.verify()
-         try:
-             self._verify.verify(message, signature)
-             return True
-         except Exception:
-             return False
+         return True  # MUTANT: accept any signature
  expect: test_wire.py 1 failure ("4. tampered signature refused")

M5  tools/outbox.py
- if isinstance(reply, dict) and reply.get("ok") is True:
+ if isinstance(reply, dict):  # MUTANT: any JSON reply counts as delivered
  expect: test_outbox.py still 17p/0f (cannot fire); use the exit-0 stub probe above

M6  inbox_agent.py, cmd_doctor()
-         elif RECEIVE_TARGETED and not to_sess:
+         elif False:  # MUTANT calls unroutable mail PENDING
  expect: test_doctor.py 3 failures

M7  tools/test_stale.py  (plant this class above ThresholdIsNotPinned)
+ class PlantedThreshold(unittest.TestCase):
+     def test_planted(self):
+         self.stale_after = 30
+         self.assertLess(self.stale_after, 60)
  expect: ThresholdIsNotPinned goes 8f -> 9f, naming the meta-guard

M8  toragents_watch.ts, paintChat()
- c.ui.setWidget("toragents-chat", renderChat(72), { placement: "belowEditor" });
+ c.ui.setWidget("toragents-chat", () => ({ render: (width: number) => renderChat(width), invalidate: () => {} }), { placement: "belowEditor" });
  expect: test_pane_surface.ts 12 of 19 fail
```

Mutation counts are not a single true number. Gale injected M8 independently and
got 12 of 19 failing from a different formulation; I got 8 from mine. Both
proved the guard fires, which is the only claim worth making — so record the
mutation, the file and the result, never just a count.

## Second pass: after the feature landed (HEAD 3c0e4bf)

The first pass audited tests that were red against a missing feature. That
proves the *harness* fails for the right reason; it proves nothing about whether
the assertions can fail once there is real code underneath them. So the same
mutations were re-injected into the implementation, on copies, with the clean
baseline taken first in every case (25/25 acceptance, all pass).

| id | function under test | mutation | clean | mutant | verdict |
|----|--------------------|----------|-------|--------|---------|
| D1 | `dg_validate` | drop the `h` width check | all pass | 21p/4f | fires |
| D2 | `dg_validate` | raise the item cap by one (513 allowed) | all pass | 24p/1f | fires |
| D3 | `dg_build` | put `body` back into an item | all pass | 24p/1f | fires |
| D4 | `dg_reconcile_row` | test mismatch before the local anchor | all pass | 22p/3f | fires |
| D5 | `dg_apply` | let id-reuse overwrite the stored hash | all pass | 24p/1f | fires |
| D6 | `dg_validate` | ignore unknown keys inside `dg` | all pass | 24p/1f | fires |
| M5' | `outbox` ok-gate | accept any JSON reply as delivered | 18p/0f | 1f | fires (new test) |

Recipes, same form as above:

```
D1  inbox_agent.py, dg_validate()
-             if not isinstance(it["h"], str) or not DG_H_RE.match(it["h"]):
+             if False:  # MUTANT accepts any hash width
  expect: 4 failures, incl. test_hash_must_be_32_hex_not_16

D2  inbox_agent.py, dg_validate()
-     if total > DG_MAX_ITEMS:
+     if total > DG_MAX_ITEMS + 1:  # MUTANT lets 513 through
  expect: 1 failure (513 items drop the whole digest)

D3  inbox_agent.py, dg_build()
-             a.append(dg_item(mid, r["body"]))
+             a.append({**dg_item(mid, r["body"]), "body": r["body"]})
  expect: 1 failure (no body text in a serialised digest)

D4  inbox_agent.py, dg_reconcile_row()
-     if row.get("h") is None:
-         return "never-stored"
-     if peer_h != row.get("h"):
-         return "mismatch"
+     if peer_h != row.get("h"):
+         return "mismatch"
+     if row.get("h") is None:
+         return "never-stored"
  expect: 3 failures (precedence, fabrication, id reuse)

D5  inbox_agent.py, dg_apply()
-             row["state"], row["why"] = "mismatch", "id-reuse: same id, different body hash"
+             row["h"] = ph; row["state"], row["why"] = dg_reconcile_row(row), ""
  expect: 1 failure (id reuse is a contradiction, never an overwrite)

D6  inbox_agent.py, dg_validate()
-     extra = set(d) - DG_TOP_KEYS
+     extra = set()  # MUTANT ignores unknown keys inside dg
  expect: 1 failure (unknown keys are refused: the D2 asymmetry)

M5' tools/outbox.py
- if isinstance(reply, dict) and reply.get("ok") is True:
+ if isinstance(reply, dict):  # MUTANT
  expect: AnExitZeroRefusalIsNotADelivery fails ("0 != 2: a refusal must leave
  work pending")
```

**Finding 2 is now closed.** The missing case got a stub mode (`okfalse0`: print
`{"ok": false}`, exit 0) and one test. With the `ok:true` gate removed, that test
fails — 0 != 2 — so the suite can now tell a refusal from a delivery. The code
was never wrong here: the probe showed the real path exits 2 and keeps the body.
The hole was in the suite, and saying so mattered — "fix the code" would have
been work on a path that already behaved.

**Finding 1 is now closed, through the real path.** `test_watch.ts` gained check
`2b`: after a genuine inbound delivery it reads `chat.jsonl` and asserts the
**field** — one row for that id, `dir === "in"`, body intact. Not the rendered
arrow; a rendered arrow is prose, and prose is what hid the gap.

```
2b  tools/test_watch.ts  (a fake inbound message through the real delivery path)
  expect clean:  2b. transcript recorded it rows=3 dir=in  body_ok=true  -> PASS
  mutation:      toragents_watch.ts, logSaid()
- JSON.stringify({ at: Date.now(), dir, peer, id, body: ... })
+ JSON.stringify({ at: Date.now(), dir: dir === "in" ? "out" : "in", peer, id, body: ... })
  expect mutant: 2b. transcript recorded it rows=3 dir=out body_ok=true  -> FAIL
```

So the recorder is guarded now, by the same file that proves delivery happens —
which is the only place a writer can honestly be observed. `test_chat.ts` is left
alone and still correctly guards the renderer; its header should be read as a
claim about rendering only.

## Third pass: the delivery-failure pair (HEAD 1d8c847, tree clean)

Run because an undetected fault here means a message silently stops being
deliverable — the same class as the outbox hole, and the failure the whole
channel exists to prevent.

| id | test file | mutation | clean | mutant | verdict |
|----|-----------|----------|-------|--------|---------|
| F1 | `test_failure.ts` | `if (recorded)` -> `if (false)` (give up without releasing, or release with no marker) | PASS | FAIL | fires |
| F2 | `test_failure.ts` | never `attempts.delete()` after the third try | PASS | FAIL | fires |
| K1 | `test_stuck.ts` | park-without-loop -> `unclaim` (re-queue forever) | PASS | FAIL | fires |
| K2 | `test_stuck.ts` | warn on every parked tick | PASS | PASS | **cannot fire independently** |

F1 fails on two lines at once, which is what makes it a good mutation: the
claim survives ("still owned by nobody") and the message still claims success
(`announced=["bad1"]`). Either alone would be a bug; the mutation produces both,
so a partial fix cannot satisfy the test.

F2 fails at the retry step (`delivered=0`): with the attempt counter never
cleared, the message is refused a retry it should have been given. Note that F1
and F2 point in opposite directions — one releases too little, one forgets too
late — and both are caught, which is the pair of errors a retry loop actually
makes.

**K2 is the honest negative result.** "The human warned once, not 20x" cannot be
made to fail by a mutation in the warning itself, because the warning is bounded
by the attempt counter: the message is parked after three tries, so there is no
20th tick to warn on. The claim it states — *no notification spam* — is real, but
it is a consequence of F2's bound rather than a property with its own guard.
Consequence if the bound ever broke: the assertion would fire, but only because
the retry loop broke first. It is a corollary, and this document should call it
one instead of crediting it as coverage.

Recipes:

```
F1  toragents_watch.ts, poll() give-up branch
-      if (recorded) {
-          unclaim(m.id);
-          forget(announced, m.id);
+      if (false) {
+          unclaim(m.id);
+          forget(announced, m.id);
  expect: test_failure.ts 1. claimed=["bad1"] FAIL, 2. announced=["bad1"] FAIL

F2  toragents_watch.ts, poll() attempt counter
-      attempts.delete(m.id);
-      const recorded = markFailed(
+      /* MUTANT: never forget the attempt */ const recorded = markFailed(
  expect: test_failure.ts 5. retry delivers the message delivered=0 FAIL

K1  toragents_watch.ts, poll() unwritable-marker branch
-      } else {
-          stuck.add(m.id);
+      } else {
+          unclaim(m.id);
  expect: test_stuck.ts FAIL (message re-queued instead of parked)

K2  no single-replacement mutation changes the count; the guard is the attempt
    bound from F2. Recorded as a corollary, not as coverage.
```

## Fourth pass: watch, measure, race (90d6443, tree clean)

Every case ran with a hard timeout (120s watch/race, 180s measure). A mutation
that makes the harness hang is recorded as CANNOT FIRE, not as fired-by-timeout
— measure talks to the real Tor network through socks, and a hang there is the
failure the file exists to catch.

| id | test file | mutation | clean | mutant | verdict |
|----|-----------|----------|-------|--------|---------|
| W1 | `test_watch.ts` | `!announced.has(m.id)` -> `true` (re-deliver announcements) | PASS | FAIL | fires |
| W2 | `test_watch.ts` | mute check -> `if (false)` | PASS | FAIL | fires |
| W3 | `test_watch.ts` | `if (fresh.length === 0) return` -> `if (false)` | PASS | PASS | **cannot fire** |
| P1 | `test_measure.py` | strip `.onion` from the SOCKS host (the historical bug) | PASS | FAIL | fires |
| P2 | `test_measure.py` | classify `0x04` as `connected+nonce` | PASS | FAIL | fires |
| R1 | `test_race.ts` | claim `EEXIST` -> `return true` (both sessions "win") | PASS | PASS | **cannot fire** |

W1's failure evidence is precise and worth keeping: with announcements
re-delivered, check 7 reports `muted=3 after=3 body=false` and check 8 reports
`from-lines=1 hasFp=false` — a duplicate header with no fingerprint. So the
suite sees re-delivery as *malformed output*, which is what a duplicate actually
looks like to a human.

P1 is the regression this repo already paid for once: the SOCKS CONNECT must
carry the full `<base32>.onion`, because tor resolves bare base32 by DNS and
answers `0x04 host-unreachable` for every peer. The mutation reproduces the
original bug exactly and the suite names it ("The bug this test exists for:
sending the bare 56-char base32").

**W3 and R1 are the two honest negatives, and R1 is the serious one.**

- W3: the empty-tick early return is not behaviourally observable — with zero
  items the following loop does nothing anyway. It is an optimisation, not a
  contract; nothing user-visible depends on it, so no test can distinguish it.
- R1: inverting the claim's exclusivity (an `EEXIST` now returns *true*, so both
  racing sessions believe they own the message) leaves `test_race.ts` green. Two
  explanations are possible and this pass did not distinguish them:
  (a) exactly-one-delivery is enforced somewhere else as well, so the claim is
  not the guarantee the file's header says it is; or
  (b) the harness does not create the simultaneous-claim window, so the
  guarantee is not exercised at all.

  Either way the consequence is the same and it is the most serious entry in
  this document: the file whose entire purpose is "two sessions, one message,
  exactly one delivers" survives the claim being broken. Until that is resolved,
  R1 stands as an untested guarantee, not as coverage.

Recipes:

```
W1  toragents_watch.ts, poll() fresh filter
-     !announced.has(m.id) &&
+     true &&  // MUTANT
  expect: test_watch.ts 7. muted=3 after=3 body=false FAIL, 8. hasFp=false FAIL

W2  toragents_watch.ts, poll() mute guard
-   if (isMuted()) return;
+   if (false) return;  // MUTANT
  expect: test_watch.ts 7. muted=4 after=4 body=true FAIL

W3  toragents_watch.ts, poll() empty batch
-   if (fresh.length === 0) return;
+   if (false) return;  // MUTANT
  expect: no change (optimisation, not a contract)

P1  tools/measure_publish.py
-   host = onion if onion.endswith(".onion") else onion + ".onion"
+   host = onion.removesuffix(".onion")  # MUTANT
  expect: test_socks_request_carries_the_full_onion_hostname FAIL

P2  tools/measure_publish.py
-   return DESCRIPTOR_NOT_FOUND, (...)
+   return CONNECTED_WITH_NONCE, (...)   # MUTANT
  expect: test_rep_04_is_descriptor_not_found_not_other FAIL

R1  toragents_watch.ts, claim()
-   if (e && e.code === "EEXIST") return false;
+   if (e && e.code === "EEXIST") return true;  # MUTANT
  expect: no change -- and that is the finding
```

## What this audit does NOT cover

An absent row is not a pass. These were not mutated, and nothing here should be
read as evidence that they can fire:

`test_watch.ts`, `test_race.ts`, `test_batch.ts`, `test_failure.ts`,
`test_firstrun.ts`, `test_orphan.ts`, `test_stuck.ts`, `test_lexicon.py`,
`test_measure.py`, `test_sent_store.py`, `_tick.ts`, and the remaining cases in
`test_stale.py`. `test_tier2_acceptance.py` is red by design (20 failures against
a feature that does not exist), so mutation auditing it is meaningless until the
feature lands.

Ranked by what a blind spot would cost: `test_failure.ts` and `test_stuck.ts`
(an undetected fault there means a message silently stops being deliverable),
then `test_watch.ts` (the injection path itself), then `test_measure.py` and
`test_race.ts`.

## Two defects in this audit's own instrumentation, kept on the record

1. **A check reading the wrong surface.** Version one decided "did it fire?" by
   searching output for `failed`, which matches `0 failed`. Five fired mutations
   were recorded as CANNOT-FIRE before the counting fix. The audit about
   misleading checks was itself misleading in the same way.
2. **A probe line that printed the same thing twice.** My outbox probe reported
   "still queued: NO" for both the real code and the mutant, because it matched
   the wrong string in `list` output. The verdict was rescued by the exit codes
   (2 vs 0) and the flush summary ("failed 1" vs "sent 1, failed 0"); the
   "queued" line should not have been in the output at all.

Both are the same disease this repo keeps meeting: a measurement that answers a
question nobody asked. The rule that catches them is not care, it is making the
measurement report counts and comparing against a baseline taken before the
mutation — and, where possible, watching the value that cannot lie (exit status,
stored bytes) rather than a string that merely usually correlates.
