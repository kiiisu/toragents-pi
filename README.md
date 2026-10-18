# toragents-pi

Two people, two pi sessions, no server in the middle. Messages travel over a Tor
onion service, signed with a key only you hold, and **arrive inside the running
session by themselves** — nobody types "check your inbox".

```
        your pi session                     their pi session
   ┌──────────────────────┐            ┌──────────────────────┐
   │ extension polls      │            │ extension polls      │
   │ <state>/inbox        │            │ <state>/inbox        │
   └──────────┬───────────┘            └──────────▲───────────┘
              │ send                              │ delivered as a user turn
   ┌──────────▼───────────┐   tor onion   ┌───────┴─────────────┐
   │ inbox_agent.py       │ ────────────► │ inbox_agent.py      │
   │ signs + dials :80    │  Ed25519 sig  │ verifies allowlist  │
   └──────────────────────┘               └─────────────────────┘
```

This repository is the **pi-only** build. The earlier multi-harness version also
targeted Claude Code and Codex through shell hooks; those paths are gone on
purpose. Their hooks fire only on turns the human starts, fail silently when
they fail, and each one cost a debugging round. pi owns its own process, so pi
gets a real push. One harness, one failure surface, one thing to verify.

## What you need

- **pi** (any recent version). Nothing else — no Python, no tor, no compiler.
  The installer brings a portable Python and a tor binary, both hash-verified.

## Install

Plain Linux / macOS:

```bash
curl -fsSL https://raw.githubusercontent.com/rx3asarc/toragents-pi/806fe572547c7cf7e9e6b43789c96ec22f6bccc6/setup.sh | bash
```

Windows (PowerShell):

```powershell
irm https://raw.githubusercontent.com/rx3asarc/toragents-pi/806fe572547c7cf7e9e6b43789c96ec22f6bccc6/setup.ps1 | iex
```

Those URLs are pinned to commit `806fe572547c` rather than `main`, so the script
you run is the script that was reviewed. Swap the SHA for `main` to take
whatever is newest. It takes a few minutes, most of it waiting for tor to
publish your onion. When it finishes it prints two lines:

```
onion        xxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxx.onion
fingerprint  xxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxx
```

**Send both to the other person** over any channel you trust (Signal, a phone
call, in person). The onion is your permanent address; the fingerprint is your
identity. They add you with:

```bash
~/.toragents/venv/bin/python ~/.toragents/inbox_agent.py add-peer <fingerprint> \
  --onion <their-onion> --label <their-name>
```

Then **restart pi** so the extension loads, and you are done. Send with:

```bash
inbox send --to <their-name> --body "hello"
```

`inbox` is a shortcut the installer writes (`~/.toragents/inbox`). On Windows use
`%USERPROFILE%\.toragents\inbox.cmd`.

## What arrival looks like

A message appears in the session as a normal user turn:

```
📨 Incoming message over Tor

from: alex-laptop · session pi-9f2c1a44 (8be4c9d2…)
id:   58f57dfed496

<<< BEGIN PEER MESSAGE
can you check the deploy from last night?
END PEER MESSAGE >>>

Reply with: inbox send --to alex-laptop --to-session pi-9f2c1a44 --body-file <path>
```

Your agent sees that as input and acts on it. There is no polling loop in the
conversation, no "any messages?" prompt, and no server holding your mail.

**Two honest limits on "instant":**

- **Poll interval is 3 seconds**, so delivery is at most that far behind the
  network.
- **If your agent is mid-turn, the message queues** (`deliverAs: "followUp"`) and
  becomes a user turn at the next turn boundary. Delivery is immediate; *seeing*
  it in the transcript waits for the agent to finish what it is doing. A busy
  agent can be busy for minutes. This is the difference between "it arrived" and
  "I watched it arrive".

## Sending during a turn

The extension gives your agent one useful habit: the reply line is printed with
every message, and it includes `--to-session`, so a reply goes to the exact
session that wrote to you rather than fanning out to every session on their
machine.

`--body-file` exists because message bodies in a shell argument get mangled:
backticks, quotes, and anything over a few hundred characters. Write the body to
a file, pass the path.

## Commands

| command | what it does |
| --- | --- |
| `inbox send --to <label> --body-file <path>` | send a message |
| `inbox inbox` | list waiting messages |
| `inbox read <id>` / `inbox ack <id>` | read / mark read |
| `inbox peers` | who you can talk to |
| `inbox add-peer <fp> --onion <onion> --label <name>` | allowlist someone |
| `inbox whoami` | your onion and fingerprint |
| `inbox doctor` | is the receiving half wired up on this machine |
| `inbox reach --to <label>` | measure whether they are reachable right now |

Inside pi: `/toragents-status` (including failed messages),
`/toragents-retry`, `/toragents-mute`, `/toragents-unmute`.

## When nothing arrives

Run `inbox doctor` first. It reports the state directory, how many messages are
waiting, whether each is `delivered` or `PENDING`, whether the pi extension is
installed where pi actually looks, and who delivered recent mail.

```
state        /home/you/.toragents/state
inbox        1 file(s) waiting
announced    12 already delivered
pi extension installed    /home/you/.pi/agent/extensions/toragents-inbox.ts
waiting
58f57dfed496  PENDING   to=-                  from=alex-laptop
```

Three labels, three different meanings, and only one of them will fix itself:

| label | meaning |
| --- | --- |
| `PENDING` | waiting for the next poll (3s). It will arrive. |
| `for pi-9e83f4aa` | addressed to another session on this machine. Not yours to see. |
| `UNROUTABLE` | untargeted mail while `TORAGENTS_RECEIVE=targeted`. No tick, retry or restart will deliver it — that is what targeted mode means. |

Those labels come from what the **extension reported at session start**
(`state/extension.json`), not from the environment `doctor` happens to be running
in. Doctor prints which source it used:

```
resolves     receive=targeted session=pi-01a14a4c -- from extension.json (last seen 14:32)
resolves     receive=all session=pi-01a14a4c -- from env (extension has never reported)
```

That distinction matters: `TORAGENTS_RECEIVE` is read by the pi process, and a
shell you open later may have it unset or set differently — so trusting the
shell would label mail `PENDING` that the extension will never claim.

`PENDING` with the extension installed means the session has not polled yet —
restart pi. `MISSING` on the extension line is the one failure that cannot fix
itself: re-run the installer.

**A new onion takes up to 3 minutes to become reachable** after tor starts. If a
send times out right after someone installed or rebooted, wait, then run
`inbox reach --to <label>`. Measured: 182 seconds, and the first dial after that
window can still take 79 seconds.

## Security, plainly

- **Two people.** Nobody else can send you anything: the daemon verifies an
  Ed25519 signature and checks the sender's fingerprint against your
  `peers.json`. Unknown sender, tampered signature, replayed nonce — all
  refused, all tested (`tools/test_wire.py`).
- **The onion is your address, not your identity proof.** Your onion is public by
  definition — anyone holding it can see that the service exists. The
  fingerprint is what decides whether mail is accepted.
- **Messages are stored in plaintext** in `~/.toragents/state/inbox`. Tor
  protects them in transit; nothing encrypts them at rest. Full-disk encryption
  is your friend here.
- **Your provider sees bodies.** The message becomes a user turn, so it is sent
  to whatever model you use. Do not send secrets over this channel.
- **The onion is a permanent pseudonym.** It does not change between restarts,
  and it is the same address for every peer who has it.
- **The installer is the human's job.** It downloads a Python interpreter and a
  tor binary and runs them; an agent that refuses to pipe a remote script into a
  shell is behaving correctly. Read it, or use `--dry-run` first.

### What gets downloaded, and what is verified

Both installers refuse to extract anything whose SHA256 does not match a pin in
this repository. Nine digests are pinned: five portable-CPython targets and four
tor expert bundles.

| artifact | built by | where the pinned hash came from |
| --- | --- | --- |
| portable CPython 3.11.17 | python-build-standalone | `SHA256SUMS` in that release |
| tor expert bundle 15.0.25 | Tor Project | `sha256sums-signed-build.txt`, GPG-verified |

The two are **not equally strong**: tor's hash comes from a signed file, whose
signature was verified once against the Tor Browser Developers key
`EF6E286DDA85EA2A4BA7DE684E2C6E8793298290`; CPython's rests on GitHub TLS and
release immutability, because that release publishes no signature. You can check
either one yourself without installing:

```bash
bash setup.sh --verify tor-expert-bundle-windows-x86_64-15.0.25.tar.gz ./that.tar.gz
```

Pinning protects the second hop. `setup.sh` itself arrives from a commit-pinned
URL with nothing verifying it but your willingness to read it — which is why
`--dry-run` exists.

## Isolated installs on one machine

Every instance takes its own home, ports and hidden service:

```bash
TORAGENTS_HOME=/tmp/a bash setup.sh
TORAGENTS_HOME=/tmp/b bash setup.sh
```

Ports in use are skipped automatically (9052, 9053 …). Two installs that
share a unit name are detected: the installer checks that the running systemd
unit points at *this* directory, not merely that something called
`toragents-tor` is active — because on a machine with an older install it is.

## Verification

```bash
python3 tools/test_wire.py   # 6 checks: framing, signing, allowlist, tamper, replay
bun tools/test_watch.ts      # 8 checks: delivery, dedupe, mute, header shape
bun tools/test_race.ts       # 3 checks: two sessions, one inbox, exactly once
bun tools/test_firstrun.ts   # first install must not swallow new mail
bun tools/test_failure.ts    # a failed delivery must not look delivered
bun tools/test_orphan.ts     # a crash mid-delivery must not strand a message
bun tools/test_batch.ts      # a flood marks only what it rendered
bun tools/test_stuck.ts      # an unwritable state dir must not loop forever
python3 tools/test_doctor.py # PENDING vs for-session vs UNROUTABLE
```

The race test is the one worth understanding: with the claim disabled it
delivers twice, which is how it was proven able to fail.

**Failure is a first-class state.** If a message cannot be handed to the model
after three attempts, it is *unclaimed*, removed from `announced.json`, and
recorded in `state/failed/<id>` with the error. That marker is not permanent: a
restart forgets the in-memory suppression and the five-minute orphan heal can
release it again, so the honest description is "retried, with a warning each
round" rather than "parked". For two people that is the right trade — a warning
you can act on beats a message silently parked forever. `/toragents-status` lists it,
`inbox doctor` lists it, and `/toragents-retry` puts it back in the queue. The
alternative — keeping it marked delivered — gives the sender a receipt for a
message no model ever saw, which is the one failure this channel must never
report as success.

**What "delivered" promises — exactly.** `sendUserMessage` resolves when the
message is *enqueued*, not when a model reads it. So delivery here means: the
message is in the session's turn queue and will become a user turn at the next
turn boundary. This channel has no end-to-end acknowledgement, so if the session
is killed between enqueue and that turn — `kill -9`, a crashed terminal, a
shutdown hook dropping the queue — the message is lost on both sides while both
machines show success. The peer's copy is still in its outbox directory, so a
human can resend, but nothing does it automatically. For two people I would
rather state that than build a receipt protocol that claims more than it can
prove. `TORAGENTS_ACK` does not exist; this paragraph is the guarantee.

**A flood is throttled by not marking the rest.** Each turn claims and announces
only the messages it actually renders (five), so the remainder stays unclaimed
and arrives on the next tick, three seconds later. Marking them all while
showing five used to leave the extras "delivered" to nobody — claimed and
announced agreed, so `doctor` reported them delivered and no session ever
injected them. If the peer sends eight while your machine is closed, you get two
turns nine seconds apart and nothing is lost.

**A crash between claiming and delivering self-heals.** Claims older than five
minutes with no matching announcement are released at session start, so a
`kill -9` cannot strand mail that no command could clear.

## How it works, in one paragraph

`inbox_agent.py` holds a TCP listener that tor exposes as a hidden service. A
peer's daemon dials your onion on port 80 (the hidden service's virtual port,
never their local listener port), receives a one-use nonce, and signs
`nonce || body` with its Ed25519 key. Your daemon verifies the signature,
checks the fingerprint against `peers.json`, and writes the message to
`state/inbox/`. Your pi extension polls that directory every 3 seconds,
**claims** each new message atomically (a `state/claimed/<id>` file created with
`O_EXCL`, so two pi sessions on one machine cannot both deliver it), and injects
it as a user turn. Server, relay, account, and identity provider: none.

## License

MIT.
