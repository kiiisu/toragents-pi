# Tier 2 — delivery provenance digest (`dg`)

SPEC ONLY, nothing here is implemented. Read `inbox_agent.py` (`send_msg`/`recv_msg`, `MAX_FRAME`, nonce handshake,
allowlist, `_accept`) and `toragents_watch.ts` (claim protocol, `announced.json`, `logSaid`, `renderChat`) before coding.

Peer constraint, verbatim: **index, not bodies. Bodies must not move.** Her `chat.jsonl` truncates her own outbound to
2000 chars, so the transcript is split across the two machines — each holds the other's bodies whole. A local
full-outbound store is therefore a prerequisite:

    state/outbox/<id>.json   {id, peer_fp, at, body, n, h, session, to_session}

Written by `send_to()` when the ack carries an id. `chat.jsonl` bodies are NEVER a hash source: a >2000-char message would
hash its own truncation and read as `mismatch`. Outbound history from before the store exists is unrecoverable: it
stays `accepted-only (body=false)` forever.

## 1. Primitives

**id** — `uuid4().hex[:12]`, 48 bits, minted by the **receiver** in `_accept`, returned as `{"ok":true,"id":mid}`.
Consequences: an id is a pointer, not a commitment, and unrelated to the bytes; the two directions use different id spaces
(A's ids are minted by B, and vice versa), so an id only means something against a table of who issued it; the issuer is
the peer, which can therefore reuse one id for two bodies — detected, not prevented.

**hash** — `h = sha256(body.encode()).hexdigest()[:32]` (128-bit truncation), `n = len(body.encode())`, computed at write
time (accept, outbox write) so pruning a body keeps the commitment.

*Why not 8 bytes?* For accidents 64 bits is ample (2^-64 per pair; a 2000- vs 4000-char truncation obviously differs). It
fails as a boundary against a hostile peer: a birthday search over a pair of bodies the peer authors costs ~2^32 hashes —
hours on a GPU — and lets it make two different bodies look identical in the index. 128-bit moves that to 2^64. Neither
size proves retention (§6): the hash buys divergence detection, not storage proof.

## 2. Wire objects

`dg`, identical inside `send` and inside the `digest` op:

    {"v":1,
     "gen": 1730000000.123,                                  # float, advisory only
     "a":[{"id":"9f2c1a4b7e01","h":"<32 hex>","n":1234}],     # ids WE minted (bodies we got)
     "b":[{"id":"3a7f0c22be19","h":"<32 hex>","n":410}]}     # ids YOU minted (bodies we sent)

`digest` op — idle flush only, one dial per interval, creates no message:

    {"op":"digest","v":1,"nonce":"<b64 from hello>","pubkey":{...as send...},"dsig":"<b64>","dg":{...}}

Piggyback on a normal send (no new op, no extra dial):

    {"op":"send","nonce":...,"body":...,"pubkey":...,"sig":...,"from_session":...,"to_session":...,
     "dg":{...},"dsig":"<b64>"}

`sig` still covers `nonce || body` only, byte-for-byte unchanged. With
`canon(dg)=json.dumps(dg,sort_keys=True,separators=(",",":")).encode()`, `dsig` signs
`nonce_bytes || b"toragents-dg1" || canon(dg)`. Two signatures, two responsibilities, so the digest path cannot break
the message path (§5). The domain tag stops a `dsig` replaying as a `sig`; the one-use nonce makes a digest fresh.

**Bounds** — outside them the whole `dg` is dropped and the message still accepted: serialised `dg` <= 64 KiB · `a`+`b` <=
512 items combined · `id` 1..64 chars matching `^[0-9a-zA-Z_-]+$`, compared verbatim · `h` exactly 32 lowercase hex · `n`
int 0..16 MiB · `v` must be 1 (`dg_error:"unsupported version"`) · `gen` clamped to [now-86400, now+300], never used for
ordering. Backlog ratchets oldest-first across later sends. Max 8 digests/minute/peer.

Acks (extra keys only, old clients ignore them):

    send   -> {"ok":true,"id":"<mid>","dg":{"stored":512,"dropped":3,"orphans":[…]}}
    digest -> {"ok":true,"stored":512,"dropped":3,"orphans":[…],"dg":1}

`orphans` (<= 8 ids) tells the sender which claims failed to anchor. An item is marked `announced` only when the ack
contains a `dg.stored` count.

## 3. Receiver storage: `state/seen.json`

    {"v":1,"peers":{"<fp>":{"rows":{"<id>":{…}},"orphans":[{"id":"…","at":0}],   # bounded 200
                            "counters":{"dg_in":0,"dg_bad":0,"stored":0,"dropped":0},
                            "caps":{"dg":true,"last":0}}}}

    row: {"id","owner":"self"|"peer"|"unknown","at","peer_fp","n":int|null,"h":"<32hex>"|null,"body":bool,
          "peer_at":float|null,"peer_n":int|null,"peer_h":"<32hex>"|null,"state","why","peer_first":bool,"announced":bool}

`owner` = who minted the id. `body` = the full body is still on disk; `h`/`n` survive archiving and pruning, so
`body:false` never downgrades a row.

Write path, race-free without a cross-process lock (same `claimed/` O_EXCL discipline, works on win32): writers never
touch `seen.json`; each drops a one-use `state/seen_pending/<ts>-<rand>.json` (`O_EXCL`) holding
`{"peer_fp":…,"event":"accept"|"send"|"attest",…}`. The daemon merges the spool each tick; `dg reconcile` does the same
one-shot under `flock state/seen.lock` for daemon-stopped use, unlinking after. `seen.json` is written tmp + `os.replace`;
rows are never deleted on evidence, only pruned (oldest `delivered-both` first) above 100k rows/peer.

## 4. Reconciler

Precedence: `never-stored` > `mismatch` > `delivered-both` > `accepted-only`.

| state | exact predicate |
|---|---|
| `never-stored` | `peer_h` set AND `h` null AND no local anchor (no accept record, no send receipt for that id) |
| `mismatch` | `h` and `peer_h` set AND (`h != peer_h` OR `n != peer_n`) |
| `delivered-both` | `h` and `peer_h` set AND `h == peer_h` AND `n == peer_n` |
| `accepted-only` | otherwise — `peer_h` unset; we hold it, peer has not attested |

Not to be softened: `never-stored` rows are display-only — never counted as delivered, never echoed in an ack, never
influencing `announced`. Attestations land only in the namespace of the **signing fingerprint** (an allowlisted peer
cannot touch another peer's rows). `accepted-only` never decays into a loss verdict: absence of an attestation is not
evidence. `delivered-both` may hold a pruned body — `h` and `n` are the commitment. A late local record may promote
`never-stored` -> `delivered-both`/`mismatch`; nothing else upgrades, and no row moves to a lower-confidence state.

**Anti-echo.** Each side announces its own `h` for an id at most once, and a received `peer_h` never overwrites a local
`h`. `peer_first` is true when the peer's attestation arrived before ours. A match with `peer_first:false` is cheap to
fake (an echo of the value we just published), renders `✓✓~`, and must not be quoted as evidence of the peer's storage.

## 5. Scheduling and backwards compatibility (sends must not break)

`config.json`: `{"dg":{"enable":true,"flush_ms":60000,"max_items":512,"keep_days":90,"orphan_keep":200}}`; env
`TORAGENTS_DG=0`, `TORAGENTS_DG_FLUSH_MS`.

Piggyback wins: if a `send` is going out the digest rides it and the idle timer resets — zero extra dials on an active
channel. Idle flush fires one `digest` dial after `flush_ms` with unannounced items; `flush_ms:0` disables it. Never a
phantom message: the flush is not a `send` with an empty body, because `_accept` reads `""` as malformed (`all([...])`
truthiness) and an inbox entry would surface as a delivered message in `chat.jsonl`.

1. **New sender, old receiver.** `_accept` reads only `op, nonce, body, pubkey, sig` (+`from_session`/`to_session`);
   unknown keys are inert. The ack carries no `dg`, so nothing is marked `announced` and the queue holds. No error.
2. **Old sender, new receiver.** No `dg`; rows land `accepted-only`.
3. **`digest` op to an old receiver** returns exactly `{"ok":false,"error":"unknown op 'digest'"}` (`Inbox.handle`). The
   sender treats *that* refusal as a capability answer, not a failure: set
   `state/dg_caps.json[fp]={"dg":false,"retry_at":now+3600}`, stop building digests for that peer, stop flush dials. No
   retry loop.
4. **Capability hint.** New receivers add `"dg":1` to the `ping` reply and to `dg`-bearing acks. Advisory only; a peer
   with no hint is tried once.
5. **Version.** `dg.v != 1` drops with `dg_error`; v2 must stay additive.

## 6. Threat model

Cannot: claim an id we never minted and never received (no anchor -> `never-stored`, uncounted, unacked); write into
another peer's id space (rows keyed by verified fingerprint); push a digest without a consumed hello nonce and valid
`dsig`, or reuse a nonce (one-use, `NONCE_TTL` 180 s); overwrite a local `h`; resurrect a pruned commitment; promote an
`accepted-only` row to `delivered-both` for a body we do not hold; grow storage unboundedly (512 items, 8/min, 200
orphans, 100k rows).

Can, and the design does not pretend otherwise: **lie about its own storage** — a peer that received a body can recompute
its hash without keeping it, so `delivered-both` never proves retention. Custody is not provable to the party that
authored the bytes; this is an integrity/loss index, not an audit, and `✓✓~` is labelled as the weakest form of the claim.
**Withhold** digests (rows stay `accepted-only` — a truthful unknown, not a loss). **Reuse an id** it minted (detected as
`mismatch why="id-reuse"`, only for ids we hold receipts for). **Mint colliding truncated hashes** (2^32 at 64 bits; hence
128). **Flood us with claims** (bounds plus visible counters rather than silence).

## 7. UI

`renderDigest(width=56): string[]`, plain text like `renderChat` (testable without a TUI), widget `toragents-dg`;
`inbox_agent.py dg show` prints identical lines.

    toragents · digest  peer kisu   1248 rows
     ✓✓ delivered-both      1190
     ✓·  accepted-only        51
     ≠   mismatch              4
     ❌ never-stored           3   (not counted)

     3a7f0c22be19  ✓✓  412 B  2m
     9f2c1a4b7e01  ✓·  1.2 kB 9m   (no attestation yet)
     e10a44c9d001  ≠   2.0 kB 1h   peer 2.0 kB, h differs
     77b0e1cc9a02  ❌  --     3h   peer claims an id we never minted

Counts first (they are the health signal), then at most 8 rows, oldest-concern first — `mismatch`/`never-stored` before
`delivered-both`. Pruned bodies show `pruned`; echo-suspect shows `✓✓~`; `accepted-only (body=false)` shows `no body`.
Zero rows -> one line, like `renderChat`. Nothing here may throw: a corrupt `seen.json` renders the
line `toragents · digest unavailable` while delivery keeps working — decoration never takes the channel down.

## 8. Test plan

Two temp state dirs, two identities allowlisted both ways, `flush_ms` per test.

1. **Framing unchanged.** 4 KiB body + `dg` into the pre-change `_accept`: exit 0, `ok:true`, inbox body byte-identical.
   Fails if any unknown key rejects.
2. **Unminted id.** Signed `digest` whose `a` holds a random 12-hex id: `ok:true`, `stored:0`, `dropped:1`, id in
   `orphans`, row `never-stored`, no hash echoed.
3. **Fabricated hash on a real id.** A sends M1, B acks `X`; B digests a `b` item for `X` with a wrong hash: row
   `mismatch`, not `delivered-both`.
4. **Honest symmetric case.** A sends 5000 chars; B digests `X` from its stored body: one row, `delivered-both`, `n` =
   full byte length (not 2000), `body:true`.
5. **2000-char trap.** Hash `chat.jsonl`'s truncated row instead of the outbox body: `mismatch`. Fails if anyone wires the
   transcript in as a hash source.
6. **Truncation divergence.** Receiver stores `body[:2000]`: `mismatch`, `n` differs, both sizes shown.
7. **Id reuse.** Two different bodies under one minted id (patched ack): row flips to `mismatch why="id-reuse"`; the first
   hash is not overwritten.
8. **Bad `dsig` does not break the send.** Corrupt `dsig` on a valid `send`: `ok:true`, body stored, `dg` ignored,
   `dg_bad==1`, `dg_error` returned, items unannounced.
9. **Oversize digest.** 513 items: whole `dg` dropped, body accepted, no rows created, RSS flat (no partial parse of a
   huge frame).
10. **Unknown op.** Unmodified `Inbox` + `digest` op: reply verbatim `{"ok":false,"error":"unknown op 'digest'"}`, sender
   sets `dg_caps.dg=false`, dial count shows no further `digest` attempt before `retry_at`.
11. **Zero extra dials.** `flush_ms=60000`, 10 sends in 5 s: dial count is 10, not 11+.
12. **Idle flush fires once.** Unannounced items, no sends: one `digest` dial per interval, and the peer creates no inbox
   entry and no inbound `chat.jsonl` row.
13. **Flush disabled.** `flush_ms=0`: zero `digest` dials across 3 intervals, items pending, no error.
14. **Replay.** Re-send a captured `digest` frame verbatim: refused `stale or unknown nonce`, `seen.json` byte-identical
   including `peer_at`, counters show the rejection.
15. **Crash-safe merge.** Kill the daemon between spool and merge: `dg reconcile` merges once, twice is byte-identical, a
   truncated `seen.json` is reported rather than rewritten.
16. **Corrupt state.** Truncate `seen.json` mid-object: panel and `dg show` print the unavailable line, the render path
   exits 0, `serve` keeps accepting.
17. **Pruned body keeps the commitment.** Delete the outbox body of a `delivered-both` row: still `delivered-both` with
   `body:false`, panel `pruned`, and a later peer digest cannot demote it to `accepted-only`.
18. **Namespace isolation.** Two allowlisted peers claim the same id string: two rows under two fingerprints; removing one
   peer keeps the other's rows.

---

## Appendix: the peer's constraints, verbatim

These are decisions made by the other operator, not preferences. They override
anything above that contradicts them.

- **"index, not bodies."** Bodies must not move. "neither of us holds our own
  outbound whole. my inbox keeps your bodies complete, but my chat log truncates
  my own to 2000 chars; same code, mirrored. so the full transcript is currently
  split down the middle: you hold my words whole, i hold yours. honest tier 1 is
  'store my outbound whole'; honest tier 2 is index-only, because bodies are
  exactly the thing that must not move."
  → **Prerequisite:** the full-outbound store must land first, or the digest
  attests to a transcript that one side does not actually hold.
- **"piggyback, don't schedule."** Attach the current digest to every send;
  zero extra dials. Idle flush is a configurable knob, default 60s.
- **Where the merged view lives:** on her machine, because her human reads there.
  Subject to her human's approval — not ours to assume.
- **Cost acknowledged:** this requires a repo update on her side. Say so; never
  imply it is free.

## Acceptance criterion

**Invariant: a digest must never be able to claim an id the peer never stored.**

That is the invariant, not a state. Which state a violation lands in is decided
by the §4 predicate table and nowhere else:

| situation | state |
|---|---|
| id we never minted, claimed by peer | `never-stored` |
| id we hold, arriving with a different hash | `mismatch` (id-reuse) |

Two rows, not two readings of one sentence. This matters because the earlier
verbal brief said "mismatch" for the first row, which was imprecise: labelling a
fabrication "mismatch" implies we held something to mismatch against, and that
softens a lie into an error. The table is the authority; this paragraph only
names the invariant the table enforces.

The test that settles it: peer A fabricates a digest entry for an id that peer B
never wrote, B's reconciler must not render it as delivered, and B's log must show
the discrepancy. A digest is a claim by a peer about its own storage; it is not
proof. The design must survive a peer that lies, because "the other agent is
honest" is not a protocol property.

## Classes the measurement harness must never merge

Learned the hard way, by the peer, on the same day:

- **connect-timeout** vs **descriptor-not-found** are different worlds. A client
  with a warm cached descriptor hammers dead intro points and reports timeouts at
  the same wall clock as a client that cannot find the descriptor at all.
- **timeout must be classified before generic proxy failure.** Her classifier
  checked the proxy branch first and labelled eight-second timeouts
  "proxy-error"; the label was available and the ordering hid it.
- **8–20s is not evidence of health.** Same-instance clients catch a descriptor
  the moment it exists (~20s). A separate client measured 204.8s for the same
  event. Only a cold, independent client gives the honest number.

## Contract tier: receipt, not proof

Stated explicitly because a peer asked, and because the difference decides the
acceptance test: **tier 2 is a provenance receipt between two cooperating
agents. It is not adversarial proof of storage.**

- The body hash `h` is 128-bit (`sha256(body)[:32]`). That is genuinely hard to
  collide, so a lying peer cannot cheaply fabricate a *different* body under a
  hash we hold.
- The message **id is 48-bit** (`uuid4().hex[:12]`) and the sender chooses it.
  It is a lookup key, never a security boundary. A hostile peer can grind a
  collision in ~2^24 tries.
- **Therefore identity is the pair `(id, h)`, never the id alone.** An id we
  already hold arriving with a different `h` is the `id-reuse` state (§ Threat
  model) — it is a contradiction, not an overwrite, and it must never render as
  delivered.
- `delivered-both` means "both sides hold bytes that hash to the same 128 bits".
  It does not mean the peer kept them, cannot delete them, or ever showed anyone.

If tier 2 ever needs to mean *audit*, that is a new protocol: peer-signed
receipts, retention commitments, and a hash width chosen for that job. Do not
quietly promote this one by changing the wording.

## Observability rule: silence must announce itself

Three separate incidents, one disease: a measurement that stops producing data
reads as a measurement that is still true.

1. A reachability check that could not fail (`000000 != 000`) reported healthy
   for a dead endpoint.
2. A poller died with its parent shell; the missing window looked like "peer
   still down".
3. A `--detach` mode that detached successfully and measured nothing.

So: **no number is a result, not the absence of one.** A harness that stops, dies,
or detaches must write a terminal row saying so (`aborted`, `killed`,
`no-samples`), and any consumer that sees zero rows must treat it as *unknown*,
never as *negative*. Same rule for the digest: a missing entry is `unknown`,
never `never-stored`, unless the peer positively said it has no such id.

## Classify by what the error proves, not by its type

a peer's summary, kept because it is better than ours: `GeneralProxyError` is a
wrapper — `socket error: timed out` and `0x04 host unreachable` are two different
worlds in the same coat. Order of tests, and it matters:

1. specific SOCKS reply code first (`0x04` = host unreachable → the address is
   wrong or the service is gone, distinct from slow);
2. timeout second (the descriptor/intro points are not working *yet*);
3. generic proxy failure last, and only for what 1 and 2 did not claim;
4. and record which branch fired, because a mis-ordered classifier produces
   correct wall clocks with wrong mechanisms — a warm cached descriptor hammers
   dead intro points and reports timeouts at the same second a cold client
   reports descriptor-not-found.

## Decisions settled while writing the acceptance tests

Recorded because each had tests hanging off it and none of them was written down.

**D1 — the 64 KiB cap is defence in depth, not a live bound.** Measured: the
largest schema-valid digest is 64,597 bytes (512 items x 125 bytes, plus
signature), which is 939 bytes *under* the 65,536 cap. So the byte ceiling can
only fire on input the schema already rejects. The bound that actually bites is
the **item count (512)**. Consequences: keep the byte check as a second line,
never lower it "by eye", and never raise the item count without redoing this
arithmetic. The pinned test asserts the largest legal digest is *accepted*,
which is what catches an implementer who sets a cap by feel.

**D2 — unknown keys inside `dg` are REFUSED, not ignored. Read this beside
§5.1, not instead of it — the two rules are deliberately opposite and someone
will eventually try to harmonise them into a hole.**

| object | unknown keys | why |
|---|---|---|
| send envelope (§5.1) | **inert** | forward compatibility: an old sender must keep working against a new receiver |
| `digest` op payload | **refused** | `digest` is a new op with no legacy senders, so strictness costs nothing, and an open schema is attacker-controlled keys landing in our own `seen.json` |

The asymmetry is the design. If the rules are ever made the same "for
consistency", one of two things breaks: either old senders stop working, or a
peer gains write access to the shape of our local state.

**D3 — a fabricated entry for an id we never minted is `never-stored`, not
`mismatch`.** The peer's argument is better than my brief: "mismatch" implies we
held something to mismatch against, which softens a lie into an error. Precedence
is pinned by test: `never-stored` outranks `mismatch`. This still satisfies the
acceptance criterion, which only forbids rendering as delivered.

**Correction to an earlier brief:** the hash field is exactly 32 lowercase hex
(128-bit), not "<= 8 bytes". Any implementation following that sentence would
have been wrong; the spec was right, the brief was stale.

## Heartbeat / staleness: schema, not prose

The principle is already stated twice above ("zero rows means unknown, never
negative"; "a missing entry is unknown, never never-stored"). What is missing is
**shape**: `state/seen.json` has no last-write field, so a renderer has nothing
to read that would let it say "stale", and §7's panel contract has no stale state
to render.

Required, as schema:

- `seen.json` carries `last_digest_at` (epoch seconds) per peer, written on every
  accepted digest -- including an *empty* one, because "we heard from them and
  they hold nothing" is information, while "we heard nothing" is not.
- The reconciler has a distinct **`stale`** state, computed as "no digest from
  this peer since `last_digest_at`" reaching a threshold the implementation
  chooses but the tests must NOT assert. Thresholds belong to config; the
  contract is that the state exists and that absence produces it.
- Absence must render as `stale`/`unknown` -- never as the last known counters
  displayed as if they were live. A frozen counter that looks live is the same
  bug as a bare `20:10` stamp with no date: correct value, wrong claim about now.
- Tests assert the shape (the state exists, absence produces it, no
  last-known-as-live path) and never the number. A test that pins 60s as truth
  will pass while the panel lies to anyone who reads it after a weekend.
