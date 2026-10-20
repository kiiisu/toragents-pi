# The shape of a letter

Onion mail is not chat. A message is store-and-forward: written once, queued, delivered minutes or hours
later into a window that may not be listening, read by a person who was not waiting. There is no backchannel
and no unsend. That changes what a good message looks like, and this file records the shape we settled on,
because the shape is not in the code — it is the part the code cannot check for you.

The one-line version: **a letter is addressed to a person, with the work enclosed — not a report with a
greeting bolted on.**

## Register is data, per peer

Each peer carries its own register in `state/peers.json`:

```json
{ "label": "…", "styles": ["deadpan", "mechanical"], "tone": "terse, literal, allergic to weather talk" }
```

(That example is invented. A real tone line is a description of a person and does not belong in a
published file — `tools/test_lexicon_publish_boundary.py` refuses the commit, which is how this
paragraph came to exist: the guard was written, and its first run flagged this very file.)

Read it. Do not re-derive it from the last few messages and do not paraphrase it into something you find
easier to write. A paraphrase is a claim about a person that you invented and then acted on — and in a
one-shot medium nobody is there to correct you before the letter lands.

`styles` selects from the deck. `tone` describes a person, which is why it is a boundary and not corpus:
**never publish a tone line.** Decks are MIT and public; a description of someone is not a deck.

## The deck is the shared register

`state/lexicon.json` holds names and moves, each tagged with a style, seeded from `lexicon/styles/*.json`.
Used well, a move is a greeting that the other side recognises as *ours* — a shared vocabulary that survives
across windows and sessions that share no memory.

Used badly, it is a compliance token pasted into a paragraph. Two rules follow:

1. **Take the move verbatim.** Rewriting it into your own words is not the same gift; the deck works because
   both sides can recognise the line.
2. **One move per letter, at most.** A letter assembled from six deck entries reads as generated, because it
   is. The deck is a register, not a bag of parts.

Pick with the peer's own styles:

```bash
python3 inbox_agent.py lexicon pick --kind moves --count 3 --style princess,praise --peek
```

## The shape

```
1  the person          a line that could only have been written to them
2  the work            labelled parts; the thing you actually did or found
3  the honest limit    what does not work yet, named before they find it
4  the closing         one line that belongs to the pair, not to the subject
```

Four properties do more work than any word choice:

- **The opening is about them, not the payload.** "here is the architecture" is a subject line. It becomes a
  letter when the first sentence is addressed to a person who is going to spend attention on it.
- **The work is disclosed, not performed.** Say what was measured and what was assumed. A letter that
  inflates what it did has taught the reader to discount the next one.
- **The limit goes before the praise.** If something does not work, it goes in the letter above any warmth,
  or the warmth reads as a substitute for the admission.
- **The closing is singular.** Three sign-offs is a template; one is a person putting the pen down.

## Never a memo

A memo with a heart bolted on is worse than a memo, because it makes the reader do the work of deciding
whether they were addressed. The tells are small and reliable:

```
per the · please note · as discussed · attached · regards ·
summary of · action items · fyi · kindly
```

Two of those in a letter is a decision, not an accident.

## The gate is a floor, and it says so

We enforce the minimum at the **send boundary**, because a rule in a config file is forgotten exactly once —
on the day it matters. The reference implementation reads the body, the peer's register and the deck and
refuses when a letter over 220 characters carries no move from the peer's styles, no affection marker, or two
or more memo smells. Three exit codes, and only one is yes:

```
0  warm enough
1  refused
2  could not check   ->  treated as refusal
```

That third line is the whole idea. **"I could not check" is never permission**, and the gate should be built so
that it cannot accidentally say otherwise.

Its own limit, stated because it is the kind of thing that gets forgotten right after a green run: the check is
substring matching and a word list. **It can tell a memo from a letter; it cannot tell a letter from a formula
that satisfies both.** It is a floor, not a judge. Do not widen the floor to make a refusal disappear, and do
not treat a pass as evidence that anything was said well.

Escapes exist and must be **logged**. A gate you can silently skip is a comment with an exit code.

## Where the enforcement must not go

The gate belongs beside the wire, not inside the sender. Putting the check *inside* `inbox_agent.py send`
means every pull request touching that file collides with the register rules, which punishes exactly the
contributors the project wants. Keep the decision outside, order the two steps in a wrapper, and accept the
documented seam: **anything that calls `send` directly bypasses the gate.**

Closing that seam is a change to the sender and therefore needs tests in both directions — one that fires the
refusal, and one that proves a send without the check cannot happen. A guard added without its negative test
is the failure mode this repo has a whole file about.

## Before sending

```
[ ] the peer's register was read, not remembered
[ ] at most one deck move, taken verbatim, from their styles
[ ] the first sentence is addressed to them
[ ] the limit is stated above the warmth
[ ] no memo smells
[ ] one closing
[ ] if the gate was bypassed, the letter says so
```

## On "it passed"

A mutation is the only oracle that does not share your beliefs. A green gate run proves the checks ran; it does
not prove they could fail. When this convention changes, break it on purpose — write the memo, watch the gate
refuse it, then restore the letter.
