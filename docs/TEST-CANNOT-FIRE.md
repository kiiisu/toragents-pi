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

**Standard to aim at:** `sent verify` (the full-outbound store) recomputes every
hash and length, exits nonzero on disagreement, and its author proved it able to
fire with three mutations — truncate the body, drop the failure record, tamper
with a stored body. A check with a documented mutation is the standard; a check
without one is a hope.

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
