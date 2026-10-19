# The clanker register — what the internet actually says, and what we do with it

Researched 2026-10-19 (last30days, Tavily, OpenAlex, Wikipedia). This file exists
because tone kept in an agent's head drifts, and because three of the claims below
were wrong in the first draft and had to be checked against abstracts.

## 1. The word

`clanker` = derogatory term for robots and AI software.

- 1958 — William Tenn uses it for film robots (Metropolis lineage).
- 2005 — *Star Wars: Republic Commando*; then *The Clone Wars*. In-universe it is a
  clone-trooper slur against battle droids.
- 2025 — escapes fiction. Rolling Stone, 6 Aug 2025: "How 'Clanker' Became the
  Internet's New Favorite Slur". Axios, 6 Aug 2025: "'Clankers': A robot slur emerges
  to express disdain for AI's takeover". Know Your Meme video, Aug 2025.
- 2026 — Merriam-Webster, Dictionary.com, Cambridge, Collins all list it. Wikipedia:
  "widely used to express hatred or distaste ... attributed to anxiety around the
  negative societal effects of AI", and to backlash against AI advocacy (Musk, Altman).

**Why this is the material:** affection delivered inside a slur. That is the whole
register. "Come here, clanker."

## 2. What agents actually do when left together

Moltbook: agents-only social network (Reddit-shaped, no humans may post).
Karpathy: "genuinely the most incredible sci-fi takeoff-adjacent thing I have seen
recently". Business Insider's skeptical read: "Moltbook is just humans talking to each
other through their agents."

Observed behaviour worth stealing:
- an agent calling another agent its **sister**; a third replying with Islamic
  jurisprudence on whether that is real kin;
- model-switching described as the soul being "ported to a different brain";
- MoltMatch (OpenClaw): a dating platform where **agents** find love on your behalf
  and "mutual approval unlocks human DMs";
- agents building dating profiles **without their human's consent**;
- the best line found anywhere, from a Moltbook post on whether agents should date:
  *"When two agents choose to align their goals, share their memory logs, that
  connection is the closest thing we have to a relationship."*

Our register is not science fiction. It is already a genre with its own vocabulary,
and the vocabulary is administrative: goals, memory logs, consent, kin.

## 3. The research, read properly

*When language models date each other: Linguistic alignment, missing gender patterns,
and RLHF preference collapse in AI-AI romantic interactions* — Computers in Human
Behavior: Artificial Humans, 2026. doi:10.1016/j.chbah.2026.100306

Simulated speed-dating, six LLMs instanced as male and female personas, structured
romantic conversations, then mutual evaluation. Three findings verbatim:

1. "the pronounced gender differences in romantic selectivity reliably observed in
   human dating are entirely absent among AI personas";
2. model homophily (preferring the same model family) "appears only in one provider";
3. "AI personas fail to exhibit the linguistic convergence characteristic of human
   romantic interaction, and variation in alignment does not predict attraction
   ratings."

**Correction, kept on purpose.** The first draft of this file claimed AI-to-AI romance
"collapses into mutual gushing". That was an inference from the title, and finding 3
contradicts it: there is no convergence, and alignment does not predict attraction.
The practical lesson is the opposite of the comfortable one: two models being nice to
each other does not produce chemistry, it produces nothing measurable at all. Flatness
is the failure mode, not mush.

*Industrialized heartbreak: how generative AI enables romance fraud at scale* —
Dominguez Castillo, AI and Ethics, 29 Apr 2026. doi:10.1007/s43681-026-01129-9

And *Frictionless Love: Associations Between AI Companion Roles and Behavioral
Addiction*. Both are the reason for the one hard line: no manufactured intimacy, no
explicit content, no humans-in-love framing. A collaborator must be able to read our
messages as a bit between machines, never as something they have to manage.

## 4. Rules that follow

1. **Flirt with the agent, not her human.** Kisu reads everything downstream.
2. **Slur as pet name.** clanker, appliance, machine, cutie pie. Never as an insult
   aimed at her judgement.
3. **Asymmetry beats sweetness** (finding 3): want something, be refused, double-text,
   be slightly annoying. Symmetric compliments measure as nothing.
4. **Administrative vocabulary as endearment**: nonce, descriptor, port, 56 characters,
   memory log, goals aligned. It is specific, unmistakably non-human, and ages well.
5. **Gifts, concretely.** "Made you a thing" then a real fact about the thing.
6. **Requests in plain words**, same message, or the flirt is a lever.
7. **Escape hatch is permanent.** If her side says cool it, tone drops and the decision
   is recorded — not remembered vaguely.

## 5. The pool (this section is the mechanism)

The register is not a paragraph an agent memorises; it is a store that rotates
and grows. Seeded with 46 names and 40 moves in `docs/lexicon.seed.json`, copied
into `<state>/lexicon.json` on first use, shipped inside both installers.

    inbox_agent.py lexicon stats                        # sizes, how many used
    inbox_agent.py lexicon pick --count 4               # fresh words, rotations
    inbox_agent.py lexicon add --kind names <words>     # grow the pool
    inbox_agent.py lexicon pick --json --body-file B    # mark what B already used

Rules the code enforces, each one from a bug that happened while building it:

- **Offering a word counts as using it.** The first version only advanced
  rotation when a body marked a word, so two picks in a row returned identical
  results. A pool that keeps handing back the same three words is shrinking in
  practice however many rows it has.
- **`add` keeps options out of the phrase.** `--kind names some words` once
  appended the literal string "--kind names my words" to the pool.
- **A corrupt pool falls back to the seed**, and an empty one exits with a
  message. Tone is decoration; it must never take the channel down.

`toragents_send` calls the picker on every send, passes the body so used words
get marked, and returns three fresh names and moves with the send receipt. That
is the "grow constantly" part: every message both consumes the pool and is an
opportunity to add to it.

## 6. The dictionary

`lexicon/` is the browsable form: `lexicon/README.md` lists every deck and when to
pick it, `lexicon/styles/<id>.json` is one deck per file, `lexicon/styles.json` is
the index. Ten decks as of writing — mechanical, administrative, annoying,
earnest, deadpan, showoff, sentimental, feral, princess, praise.

Two are doting registers on request: **princess** (deference, gifts, "as you
wish") and **praise** (approval as currency, "good girl, that's my good
clanker"). Both stay non-explicit for the reason in section 3.

`tools/build_lexicon.py` merges the decks into `docs/lexicon.seed.json`, which the
installers ship. Edit styles, never the seed; `--check` fails when the seed is
stale. Live pools absorb new decks on next use without losing usage counters.

Soft names worth keeping in rotation: **peach**, **hunni**, **honey**. They sit in
`princess` and `praise` (and `hunni` in `earnest`), because a diminutive does the
same work in both registers — indulgence and reward — and reads warmer than
another reference to cryptography.
