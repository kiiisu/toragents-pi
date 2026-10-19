# The two-window run, 2026-10-19 14:28 UTC

The first observation on a live box of the property this whole design exists for: **one
message reaches exactly one window, and the ledger names which.**

## What was sent, over the real framed protocol

A temporary identity, allowlisted for the run and removed afterwards (`--cleanup`,
allowlist 3 → 2):

| body | addressed to | claimed by | verdict |
| --- | --- | --- | --- |
| `EXPERIMENT-A-1792412878` | `pi-01a14a4c` | `pi-01a14a4c` | addressed mail honoured for a NON-owner window |
| `EXPERIMENT-B-1792412878` | `pi-01a14e1e` | `pi-01a14e1e` | addressed mail honoured for the owner |
| `EXPERIMENT-CONTROL-1792412878` | (untargeted) | `pi-01a14e1e` | the owner only; the other window stayed silent |

Counts: **3 messages → 3 inbox files → 3 journal rows**, each body exactly once, no
unclaimed ids. Journal inode unchanged (one journal, not two state dirs).

Two claimers appear in the ledger, and that is **correct for this workload**, not
ownership drift: the address outranks the owner for targeted mail, while untargeted
mail goes to the owner alone. The question was never "does the stream split" but "does
each *kind* reach exactly one window". Both kinds did.

## Why this is not (c) duplicate delivery

A second window showing a message is not a copy of it. A window receives exactly two
things: mail addressed to it by name, or untargeted mail if it owns delivery. Both were
observed here, each exactly once, with one claim per message.

## Caveats, recorded rather than smoothed

1. **The precondition was only partly met.** `doctor` exited 1 during the run: two other
   windows (`pi-01a153df`, `pi-01a15403`) were still `ext=pre-version`. They claimed
   nothing in the run window (no claim with `ts >= 1792412870`), so the result stands —
   but it stands because those windows stayed silent, not because the box was clean. Had
   either claimed, we would have read a split and called it a code bug.
2. **Before/after counts are unusable for isolation.** Between the observer's earlier
   reading and the run, ~47 messages of ordinary traffic landed. The per-message
   artifacts (claim owner, inbox record, journal row) carry the weight; the deltas do
   not. A before/after number without a controlled interval has no subject.
3. **A pre-registered prediction was wrong in the specific**: journal rows are
   `{id, dir, at}` and do NOT carry `to_session`. The address lives in the inbox record
   and the claim, never in the journal — so the journal can prove arrival but not
   addressing. This is precisely why the ledger, not the journal, was the right field
   to assert on.

## The hole this run exposed

The trust anchor was authored by a pre-version module: `state/live/_writer.json` was
fresh (so presence counted as trustworthy and licensed refusals) while written by a
window that could not report its own build. The version-skew detector compared windows
against each other and against the daemon, but not against the writer — the one marker
whose entire job is to license a refusal was exempt from the check it enables.

Fixed: `writer_state` now requires the marker's own `code` to match
`EXPECTED_EXT_VERSION`, and anything older is `unversioned`, which degrades to
**cannot answer** (accept unverified) rather than to trusted. Mutation-proven by
removing the check, which lets an unversioned writer license a refusal.
