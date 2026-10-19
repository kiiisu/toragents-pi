# Tier-2 acceptance test plan

Companion to `tools/test_tier2_acceptance.py`. Spec: `docs/TIER2-DIGEST.md`.
Written **before** the implementation exists; 18 of 23 tests fail today, each
naming the missing piece and the spec section it comes from.

Run it:

    python3 tools/test_tier2_acceptance.py          # 23 tests
    python3 tools/test_tier2_acceptance.py -v       # per-test
    python3 tools/test_tier2_acceptance.py Compat   # the compatibility pins only

Reading the output: a **CONTRACT** test fails today and must pass when tier 2
lands. A **COMPAT** test passes today *by design* — it pins the old wire that
tier 2 is forbidden to break. If a COMPAT test ever fails, deployed peers stop
talking and no amount of tier-2 correctness saves you.

The harness is real, not mocked: each test starts an actual `inbox_agent.py
serve` in a temp state, allowlists a real second identity through the CLI, and
speaks the actual framed protocol over loopback (`hello` -> nonce -> `send`).
That is deliberate — a mocked `_accept` would let the code pass while the wire
stayed broken.

---

## COMPAT — the old wire (passes today by design)

| test | asserts | fails if | why it matters |
|---|---|---|---|
| `test_1_new_sender_old_receiver_accepts_a_digest_bearing_send` | a `send` carrying `dg`+`dsig` is accepted, body byte-identical, and no `dg` key is stored on the message | any unknown key starts rejecting, or the body changes | §5.1. Every deployed peer is an "old receiver" today; one strict parser turns the whole network dark. |
| `test_2_old_receiver_refuses_the_digest_op_with_that_exact_sentence` | an unmodified receiver answers exactly `{"ok":false,"error":"unknown op 'digest'"}` | the wording drifts | §5.3 makes that sentence a *capability answer* the sender keys off (`dg_caps[fp].dg=false`). Reword it and the sender cannot tell "unsupported" from "broken". |
| `test_3_old_receiver_ignores_a_corrupt_dsig_on_a_valid_send` | a zeroed `dsig` does not stop the message | a bad digest signature blocks the body | §5. Two signatures, two responsibilities: the digest path must never be able to break the message path. |
| `test_4_ten_sends_are_ten_dials_and_no_phantom_message` | 10 digest-bearing sends produce 10 inbox messages, none empty | a digest arrives as a message, or an empty body is accepted | §5: "never a phantom message". `_accept` treats `""` as malformed, so a flush implemented as `send` with an empty body would either fail loudly or, worse, land as a message. |

## Schema — §2 bounds (CONTRACT)

Rejecting a digest must never cost the message: in every one of these, the body
is still delivered and only the digest is dropped.

| test | asserts | fails if | why it matters |
|---|---|---|---|
| `test_unknown_top_level_field_is_rejected` | an unrecognised key in `dg` is refused | extra fields pass through | An open schema is an unbounded attacker-controlled store inside our state file. |
| `test_hash_must_be_32_hex_not_16` | `h` is exactly 32 lowercase hex; 16, 64, uppercase and non-hex are refused | a 64-bit or uppercase hash passes | §1 chose 128-bit over 64 specifically to kill the 2^32 birthday attack. Accepting 16 bytes silently reinstates it. |
| `test_id_charset_and_length_are_enforced` | `id` is 1..64 chars of `[0-9a-zA-Z_-]` | `""`, 65 chars, `../etc/passwd`, spaces or emoji pass | The id becomes a filename-ish key in `seen.json` and in logs. Path characters in an id are how an "index" becomes a directory traversal. |
| `test_n_is_bounded` | `n` is an int in 0..16 MiB | `-1`, 16 MiB+1, `"12"` or `1.5` pass | `n` is used to prove a length divergence; a bool/float/string there makes the comparison meaningless. |
| `test_513_items_drop_the_whole_digest` | 513 items drops the digest entirely | it truncates to 512, or is partially applied | §2: "the whole `dg` is dropped". Partial application means the peer's ack says `stored:512` for something we did not store. |
| `test_the_largest_schema_valid_digest_is_the_boundary_worth_testing` | the biggest legal digest (512 items x 64-char id, n=16777216, **measured 64,553 bytes**) is accepted | the implementation drops it | Measured finding: the 64 KiB cap is **unreachable** through schema-valid items, so it is defence in depth, not a bound. The item count is what actually bites. An implementation that guesses the byte cap by eye will reject a legal digest. |
| `test_version_other_than_1_is_refused_with_the_reason` | `v != 1` is refused with a reason mentioning version | v2 is half-understood, or refused silently | §5.5: v2 must stay additive; a silent drop makes a future rollout undebuggable. |
| `test_gen_is_clamped_not_rejected` | an absurd `gen` is accepted and clamped | a wild clock loses the digest | `gen` is advisory (§2). Rejecting on it would let a peer with a broken clock take the channel down for a cosmetic field. |

## No bodies — the peer's hard constraint (CONTRACT)

| test | asserts | fails if | why it matters |
|---|---|---|---|
| `test_a_serialised_digest_contains_no_body_text` | a digest built from a body contains neither the body text, nor a `body` key, and items are exactly `{id,h,n}` | any body text or body field appears | The appendix is explicit: "bodies are exactly the thing that must not move." A digest that leaks text is a transcript being copied, which is the thing the peer refused. |

## Reconciler — §4 states (CONTRACT)

Precedence under test: `never-stored` > `mismatch` > `delivered-both` > `accepted-only`.

| test | asserts | fails if | why it matters |
|---|---|---|---|
| `test_both_match_is_delivered_both` | `h == peer_h` and `n == peer_n` is `delivered-both` | the obvious case is wrong | The baseline; everything else is defined against it. |
| `test_hash_divergence_is_mismatch` | differing `h` is `mismatch` | a differing hash still renders delivered | This is the contradiction case: same id, two bodies. |
| `test_length_divergence_alone_is_mismatch` | same `h`, differing `n` is `mismatch` | a 4096-vs-2000 pair renders delivered | The 2000-char trap: if anyone ever hashes the *transcript* instead of the stored body, this is the row that shows it. |
| `test_no_attestation_is_accepted_only_never_a_loss` | no `peer_h` is `accepted-only` | absence becomes a loss verdict | §4: "absence of an attestation is not evidence." Turning silence into a loss accusation is the failure mode this whole tier exists to avoid. |
| `test_claim_without_any_local_anchor_is_never_stored` | `peer_h` set with no local `h` and no anchor is `never-stored` | it is counted as delivered | An unauthorised claim of storage we never had. |
| `test_never_stored_outranks_mismatch` | when both predicates fit, `never-stored` wins | a fabrication renders as `mismatch` (a near miss) | The strongest failure first. Labelling a fabrication "mismatch" implies we *held* something to mismatch against, which softens a lie into an error. |

## Adversarial — the acceptance criterion (CONTRACT)

| test | asserts | fails if | why it matters |
|---|---|---|---|
| `test_fabricated_entry_is_never_rendered_delivered` | a signed digest naming an id we never minted and never received lands `never-stored`, `announced:false` | it renders `delivered-both` | The acceptance criterion verbatim: "A digest must not be able to claim an id the peer never stored." |
| `test_id_reuse_is_a_contradiction_not_an_overwrite` | the same id with a different `h` becomes `mismatch why="id-reuse"` and the first `h` survives | the second hash overwrites the first | §Threat model: the peer chooses ids, so id reuse is expected behaviour from a hostile peer and must be *detected*, not prevented-and-forgotten. |
| `test_a_digest_frame_cannot_be_replayed` | a captured `digest` frame is refused twice, and `seen.json` is byte-identical after the replay | a replay mutates state, or is accepted | The one-use nonce is the only thing making a digest fresh. A replayable digest is a peer accumulating claims it never made. **Note:** passes today for the old-wire reason (the op is refused outright); it becomes a real check the moment `digest` lands, and it must not be deleted in between. |

## Surface — §7 (CONTRACT)

| test | asserts | fails if | why it matters |
|---|---|---|---|
| `test_dg_show_exists_and_does_not_throw_on_a_corrupt_store` | `inbox_agent.py dg show` exists, exits 0 on a truncated `seen.json`, and prints the unavailable line | the subcommand is missing, or a corrupt store raises | §7: "Nothing here may throw: a corrupt `seen.json` renders the unavailable line while delivery keeps working — decoration never takes the channel down." |

---

## Stale state — a separate contract, a separate file

`tools/test_stale.py` (9 tests: 8 fail today, 1 passes because it guards this
file rather than the implementation). Kept apart from the acceptance file on
purpose: tier 2 asks "does the index tell the truth about storage", staleness
asks "does the panel tell the truth about *now*". They fail for different
reasons and will be fixed by different code.

Spec: `TIER2-DIGEST.md` "Heartbeat / staleness: schema, not prose".

| test | asserts | fails if | why it matters |
|---|---|---|---|
| `test_the_spec_state_is_reachable_and_named` | `stale` is a member of the state vocabulary (or at least renderable), not an adjective invented at print time | no vocabulary and no stale render | A state that exists only inside a `print()` cannot be tested, counted, or asserted by any consumer. |
| `test_a_peer_we_never_heard_from_is_stale_not_up_to_date` | a peer with no `last_digest_at` renders stale | absence renders as freshness | The whole point: never-having-heard is not evidence of agreement. |
| `test_an_ancient_digest_is_stale` | 400 days old renders stale | old data renders current | The obvious case, kept so the next three have a baseline. |
| `test_a_weekend_old_digest_is_stale` | 3 days old renders stale | only very old data goes stale | A 60-second constant passes "just now / 400 days" and still lies to anyone reading after a weekend. |
| `test_a_digest_from_just_now_is_not_stale` | a fresh digest does not render stale | it warns unconditionally | A warning that always fires is not a warning. |
| `test_an_empty_digest_counts_as_heard_from` | an accepted **empty** digest sets `last_digest_at` and clears stale | empty digests leave the peer stale | "They hold nothing" is information; "we heard nothing" is not. Conflating them makes a healthy quiet peer look dead. |
| `test_stale_never_presents_the_old_counters_as_current` | with real counters (1190 delivered) stale, the qualification appears **in the first three lines** and the counters are still shown | staleness is a footnote under numbers that read as live | The contract is to qualify the counters, not hide them; hiding them loses the health signal §7 promises. |
| `test_a_store_that_cannot_be_read_says_so_and_does_not_throw` | truncated `seen.json` exits 0 and prints the unavailable line | a corrupt store throws or prints numbers | §7: decoration never takes the channel down. |
| `test_this_file_does_not_assert_a_threshold_number` | **this file** pins no threshold constant (scanned with `ast`, `Compare` nodes only) | a plausible threshold literal appears in a comparison | The spec says tests must not assert the number. The first version of this check scanned by substring and flagged its own docstring — a measurement reading the wrong surface, which is the exact defect the file is about. |

Threshold discipline: this file constrains the number only from the outside — a
digest from *now* must not be stale, and a digest from *3 days ago* must be. It
never asserts 60, 600 or 3600. If an implementation picks a value that fails the
3-day case, the reading experience is what broke.

## What is deliberately NOT tested here, and why

- **Idle flush / zero extra dials timing** (§5, tests 11-13 of the spec's own
  plan). These need a clock and a dial counter across two real tori; they belong
  in a harness test, not in an acceptance test that must stay under ten seconds.
  The two properties that *can* be pinned without timing are here as COMPAT
  1-4.
- **`dsig` domain separation** (§2: the tag stops a `dsig` replaying as a `sig`).
  It is a property of the signing helper, and the first implementation should
  ship it with its own unit test rather than having one written blind here.
- **Cross-process spool merge** (§3, `seen_pending/`, `flock`). Race tests need
  a real second process; the existing `tools/test_race.ts` is the model to copy
  when the implementation exists, because writing it now would test my guess at
  the file layout rather than the spec.
- **Anything that needs the digest to already work in order to observe it.**
  Every test here observes either the old wire (which exists) or the absence of
  the new API (which is the finding).

## Spec gaps found while writing this, and how they were settled

Both were raised with the spec's author and with the peer, and both now have a
decision rather than a hole in it. The decisions are recorded here because the
tests encode them; `TIER2-DIGEST.md` is the author's to amend.

### Settled: unknown keys inside `dg` are REFUSED

Not inconsistent with §5.1's "unknown keys are inert" — the two rules govern
different objects. On the `send` envelope, inertness is where forward
compatibility lives, because an old sender must keep working against a new
receiver. `digest` is a new op with no legacy senders, so strictness costs
nothing there and buys something concrete: an open schema inside `dg` is
attacker-controlled keys landing in our own `seen.json`.

The two rules must be stated **side by side** in the spec, or someone will
"harmonise" them and open the hole.

### Settled: "not delivered" is the invariant, not a state

A fabricated entry is `never-stored` (an id we never minted and never received);
id-reuse on an id we *do* hold is `mismatch`. Those are two rows in §4's table,
not two readings of one sentence. The acceptance criterion should state the
**invariant** — a digest must not be able to claim an id the peer never stored —
and point at the table for the resulting state. The earlier verbal "mismatch"
brief was imprecise, not a competing design, and the tests following the table
are correct.

## Spec gaps found while writing this

1. **The 64 KiB cap cannot be reached by a schema-valid digest** (max measured
   64,553 bytes). It is defence in depth. If it was meant as a real bound, the
   item limits need revisiting.
2. **Unknown fields in `dg`**: §2 defines bounds but never says whether an
   unknown *key* is refused or ignored. §5.1 makes unknown keys inert for the
   `send` envelope, which is not the same thing. The test asserts refusal,
   because a digest is parsed by us alone and an open schema is extra attack
   surface — but this is a decision the spec should state.
3. **`never-stored` vs `mismatch` for a fabricated entry**: the acceptance
   criterion says it "must not render as delivered"; §4's predicate table says
   such a row is `never-stored`; an earlier verbal brief said `mismatch`. The
   tests follow the table (§4), since it is the only one written as a predicate.

---

## Decisions this plan depends on (settled, see TIER2-DIGEST.md)

- D1: 64 KiB is defence in depth; the 512-item count is the live bound. Largest
  legal digest measured at 64,597 bytes vs a 65,536 cap.
- D2: unknown keys inside `dg` are refused, not ignored.
- D3: fabrication for an id we never minted is `never-stored`; it outranks
  `mismatch`, which would imply we held something to mismatch against.
