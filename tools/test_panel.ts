#!/usr/bin/env bun
/**
 * The reader: subject lines, collapsed cards, the bottom strip, and the column
 * that reserves screen columns.
 *
 * Run with tools/test_panel.sh — this test needs @earendil-works/pi-tui, which is
 * only resolvable from pi's own node_modules; the wrapper points NODE_PATH at it.
 *
 * WHAT IS ASSERTED, in the order these things break:
 *
 *   1. THE TRANSCRIPT MERGE. chat.jsonl is the spine, but its stored bodies were
 *      once cut at 2 000 characters, and mail can be consumed on one box and
 *      still sit in inbox/ on another. So: a short stored body must be replaced by
 *      the whole one from inbox/, and a message the transcript never saw must still
 *      appear. Reading only the spine hides the technical half of every message.
 *
 *   2. THE SUBJECT LINE. Peers write 1.5-3k character letters with no subject. The
 *      derived subject must pick the sentence carrying the identifier rather than
 *      the polite opening, and the preview under it must not repeat it — two lines
 *      of the same text is not a preview.
 *
 *   3. THE GEOMETRY. Everything fits the width it is given, the strip is exactly
 *      its configured height, a card that fits is scrolled into view without the
 *      list jumping, and a card taller than the window aligns to its header (or you
 *      are looking at the tail of a body with no idea which message you are in).
 *
 *   4. THE COLUMN ITSELF, against a fake TUI: installing narrows
 *      terminal.columns, painting happens inside pi's own render frame, and
 *      dispose restores width, render hook and wheel routing. If dispose does not
 *      restore, closing the column leaves pi permanently narrowed.
 *
 * The fixture is built in a temp dir, so this runs on any machine and asserts the
 * same things everywhere. A test that reads the author's mailbox tests the author.
 */
import { mkdirSync, mkdtempSync, writeFileSync } from "node:fs";
import { tmpdir } from "node:os";
import { dirname, join } from "node:path";
import { visibleWidth } from "@earendil-works/pi-tui";

const fails: string[] = [];
const ok = (cond: any, msg: string) => {
	if (!cond) fails.push(msg);
	else console.log("ok   " + msg);
};

// ── the fixture ─────────────────────────────────────────────────────────────
//
// Shaped like a real exchange: a rehydrated truncation, an inbox-only message,
// a long letter with paragraphs, a greeting that must not become the subject,
// and a clause the same peer restates (so the preview dedupe must allow a
// leftover occurrence instead of cutting the sentence you are about to read).
const STATE = mkdtempSync(join(tmpdir(), "toragents-panel-"));
mkdirSync(join(STATE, "inbox"), { recursive: true });
mkdirSync(join(STATE, "sent"), { recursive: true });
const T0 = 1_760_000_000; // fixed clock: nothing here may depend on when it runs

const GREETING = "hi there \u2014 and thanks for the pointer.\nthe digest never worked, see tools/repro_digest_dead.py for the proof.";
const long1 =
	"the digest i told you was live had never worked once, and four of my own green commits sat on top of it. " +
	"stop and pull before you reinstall 1d8c847.\n\n" +
	"what i actually ran, on my box, against the deployed daemon:\n\n" +
	"  tools/repro_digest_sides.py --peer alpha\n  -> exit 0, delivered-both, claimed/ has 27 rows\n\n" +
	"the claimed/ ledger is the proof: 27 rows, each carrying the peer label and the message id, and the " +
	"digest that summarises them disagrees with the count only when the tail is written after the head. " +
	"that is a two-writer bug, not a reporting bug, and no amount of re-reading the report will find it.\n\n" +
	"so the fix is ordering, not reporting: write the tail first, mark it, then the head. if a crash lands " +
	"between them the ledger is incomplete and visibly so, which is the property we want. i'll take the rows, " +
	"stores named, no summary. \u2661";
const long2 =
	"it's the right answer for the SENDER, but it's quiet for the RECEIVER. " +
	"a half-deployed box would accept-unverified forever and nothing fails loudly.\n\n" +
	"the deployment gap only shows up in a response field the sender might not read, so pair it with a loud " +
	"local signal: the daemon logs \"accepted with unverified presence \u2014 no heartbeat writer\", and doctor " +
	"flags \"no _writer.json\" as a deployment issue rather than an operational one. same sentence, fourth " +
	"instance, and it's the one that keeps a checkable claim from becoming an asserted one.\n\n" +
	"absent data is not data of absence: an empty registry cannot tell \"no windows\" from \"no writer\", so it " +
	"must not answer with a verdict. the sentinel fixes that by turning one ambiguous fact into two distinct " +
	"ones. keep it.";
const long3 = Array.from({ length: 18 }, (_, i) => `paragraph ${i + 1}: the marker that licenses a refusal must be current itself, or the refusal is stale by construction.`).join("\n\n");

const messages = [
	{ id: "aaa1", dir: "in", at: T0 + 60, peer: "kisu", body: "hi there \u2014 and thanks for the pointer.\nthe digest never worked, see tools/repro_digest_dead.py for the proof." },
	{ id: "bbb2", dir: "out", at: T0 + 120, peer: "kisu", body: "confirmed in my deployed copy: line 335 is PublicKey.from_dict, wire.py defines PublicIdentity, and the except at 338 rewrites the NameError into \"unknown op 'digest'\"." },
	{ id: "ccc3", dir: "in", at: T0 + 180, peer: "kisu", body: long1 },
	{ id: "ddd4", dir: "out", at: T0 + 240, peer: "kisu", body: "the reframing is right, and it's the more useful one: locatability, not safety." },
	{ id: "eee5", dir: "in", at: T0 + 300, peer: "kisu", body: long2 },
	{ id: "fff6", dir: "in", at: T0 + 360, peer: "kisu", body: long3 },
	{ id: "ggg7", dir: "out", at: T0 + 420, peer: "kisu", body: "run `python3 inbox_agent.py doctor` on your machine and send me the two lines it prints." },
];

// The spine: order and who said it. Two entries are deliberately damaged:
// ccc3 stores a 2 000-character truncation (the old cap), and hhh8 is missing
// entirely from the spine but present in inbox/.
const spine = messages
	.filter((m) => m.id !== "hhh8")
	.map((m) => ({ at: m.at * 1000, dir: m.dir, peer: m.peer, id: m.id, body: m.id === "ccc3" ? m.body.slice(0, 300) : m.body }));
writeFileSync(join(STATE, "chat.jsonl"), spine.map((r) => JSON.stringify(r)).join("\n") + "\n");

// inbox/: the received body as it came off the wire — whole.
const inboxOnly = { id: "hhh8", from_label: "kisu", body: "this one never reached the transcript on my box, and a reader that trusts the spine alone would hide it: verified against 7c3925c, not taken on faith.", received: T0 + 480 };
for (const m of [...messages.filter((m) => m.dir === "in"), inboxOnly]) {
	writeFileSync(join(STATE, "inbox", `${m.id}.json`), JSON.stringify({ id: m.id, body: m.body, from_label: m.peer || m.from_label, received: m.at ?? m.received }));
}
for (const m of messages.filter((m) => m.dir === "out")) {
	writeFileSync(join(STATE, "sent", `${m.id}.json`), JSON.stringify({ id: m.id, body: m.body, at: m.at, receipt: "sent" }));
}

// policy: the placement contract, written the way a human would.
writeFileSync(
	join(dirname(STATE), "policy.json"),
	JSON.stringify({ receiver: "rowan", panel: { default: "strip", column: ["stage"] }, fallbackToAnyone: true }),
);

process.env.TORAGENTS_STATE = STATE;
process.env.TORAGENTS_HOME = dirname(STATE);
delete process.env.TORAGENTS_PANEL;

const { readThread, threadSignature, tldr } = await import("../_lib/toragents-transcript.ts");
const { SideColumn } = await import("../_lib/toragents-side-column.ts");
const { PANEL_VERSION, renderColumn, renderStrip, subject } = await import("../toragents-panel.ts");
const { POLICY_DEFAULTS, isReceiver, panelMode, policyPath, readPolicy } = await import("../_lib/toragents-policy.ts");

const theme = { fg: (_c: string, s: string) => s, bold: (s: string) => s };

// The reader's self-declared version, pinned the way EXT_VERSION is: a change to how
// the pane behaves must arrive with a bump, or a window loaded before it will keep
// rendering the old shape and look healthy. Change this pair together, deliberately.
ok(PANEL_VERSION === 1, `the reader is panel v1 (found v${PANEL_VERSION}) — bump both sides together`);

// ── 1. the transcript merge ────────────────────────────────────────────────
const rows = readThread();
ok(rows.length === messages.length + 1, `every message is present (${rows.length} of ${messages.length + 1})`);
ok(rows.every((r: any, i: number) => i === 0 || r.at >= rows[i - 1].at), "chronological order");
ok(rows.some((r: any) => r.dir === "in") && rows.some((r: any) => r.dir === "out"), "both directions read");

{
	const rehydrated = rows.find((r: any) => r.id === "ccc3");
	ok(rehydrated.body.length > 300, `a truncated stored body is replaced by the whole one (${rehydrated.body.length} chars)`);
	ok(rehydrated.body.includes("the fix is ordering, not reporting"), "the rehydrated body is the real text, not a longer truncation");
	const missed = rows.find((r: any) => r.id === "hhh8");
	ok(Boolean(missed), "mail the transcript never saw still shows");
	ok(Boolean(missed) && missed.body.includes("7c3925c"), "and it shows its wire body");

	// Order is the spine's job: an inbox-only message must not be promoted to the
	// end just because it was found last.
	const order = rows.map((r: any) => r.id);
	ok(order.indexOf("hhh8") > order.indexOf("ggg7"), "an unseen message lands by its received time");
}

ok(threadSignature().includes("|"), "the change signature is cheap to compare");
ok(tldr(long1).length > 0 && tldr(long1).length <= 151, "tldr is non-empty and bounded");
ok(tldr("") === "", "tldr of an empty body is empty");

// ── 2. the subject line ────────────────────────────────────────────────────
console.log("\nsubjects:");
for (const m of messages.slice(0, 5)) console.log(`  ${subject(m.body)}`);

ok(messages.every((m) => subject(m.body).length > 0), "every message gets a subject");
ok(messages.every((m) => subject(m.body).length <= 61), "subjects are bounded");
ok(subject("").length > 0, "an empty body still yields a subject");
ok(subject("thanks! also, the fix is in 1d8c847 and wire.py line 335 is wrong.").includes("1d8c847"), "the subject prefers the sentence carrying the identifier");
ok(!/^(hi|hey|hello|thanks|thank you)\b/i.test(subject(messages[0].body)), "a greeting is not the subject");
ok(!/^(so|and|but|also)\b/i.test(subject("so build it once, and then have the reader notice.")), "a subject does not start mid-sentence on a conjunction");

// ── 3. the geometry ────────────────────────────────────────────────────────
const kOf = (m: any) => `${m.dir}:${m.id}`;
const viewAt = (idx: number, extra: any = {}) => {
	const probe = { selected: idx, expanded: new Set<string>(), seen: new Set(rows.map(kOf)), focused: false, top: 0, ...extra };
	const abs = renderColumn(rows, 46, 40, probe, theme).absolute;
	return { ...probe, top: Math.max(0, abs[idx] ?? 0) };
};

for (const width of [28, 46, 100]) {
	for (const height of [10, 30, 60]) {
		const out = renderColumn(rows, width, height, viewAt(rows.length - 1), theme).lines;
		ok(out.length === height, `column renders exactly ${height} rows at width ${width}`);
		ok(out.every((l: string) => visibleWidth(l) <= width), `column fits ${width} cols (${height} rows)`);
	}
}

{
	const newest = rows[rows.length - 1];
	const atNewest = renderColumn(rows, 46, 40, viewAt(rows.length - 1), theme);
	const s0 = atNewest.starts[rows.length - 1];
	const e0 = atNewest.ends[rows.length - 1];
	ok(s0 >= 0 && e0 <= 37, `the selected card is scrolled into view (${s0}..${e0})`);
	ok(atNewest.lines.join("\n").includes(subject(newest.body).slice(0, 18)), "the selected message shows its subject");

	// A window too short for the whole list must not paint what is above it.
	const short = renderColumn(rows, 46, 12, viewAt(rows.length - 1), theme).lines.join("\n");
	ok(!short.includes(subject(rows[0].body).slice(0, 18)), "a message outside the window is not painted");
	ok(short.includes(subject(newest.body).slice(0, 18)), "the window still shows what is selected");
}

{
	const idx = rows.findIndex((r: any) => r.id === "ccc3");
	const victim = rows[idx];
	const deep = victim.body.split("\n").map((l: string) => l.trim()).filter(Boolean)[5].slice(0, 24);
	const collapsed = renderColumn(rows, 46, 40, viewAt(idx), theme).lines.join("\n");
	const expanded = renderColumn(rows, 46, 40, viewAt(idx, { expanded: new Set([kOf(victim)]) }), theme).lines.join("\n");
	const first = victim.body.split("\n").map((l: string) => l.trim()).filter(Boolean)[0].slice(0, 24);
	ok(collapsed.includes(first), "a collapsed card shows the first line");
	ok(collapsed.includes("more line"), "a collapsed card says how much is hidden");
	ok(!collapsed.includes(deep), "deeper lines stay hidden while collapsed");
	ok(expanded.includes(deep), "expanding a message reveals deeper lines");
}

{
	// Tall cards align to the header; tall-enough cards are the case where
	// "follow the end" would hide the very header that says where you are.
	const idx = rows.findIndex((r: any) => r.id === "fff6");
	const tall = renderColumn(rows, 46, 40, viewAt(idx, { expanded: new Set([kOf(rows[idx])]) }), theme);
	ok(tall.starts[idx] === 0, "a card taller than the window aligns to its header");
	const focused = renderColumn(rows, 46, 40, viewAt(idx, { focused: true }), theme).lines[0];
	ok(focused.includes("FOCUS"), "focus is visible in the header");
	ok(renderColumn(rows, 46, 40, viewAt(idx), theme).starts.length === rows.length, "layout reports one start per message");
}

// The preview must not repeat the subject: two lines of the same text is not a preview.
{
	let repeats = 0;
	for (const m of rows) {
		const card = renderColumn([m], 46, 40, { selected: 0, expanded: new Set<string>(), seen: new Set([kOf(m)]), focused: false, top: 0 }, theme)
			.lines.slice(3, 5)
			.join(" ")
			.replace(/\s+/g, " ");
		const subj = subject(m.body).replace(/…$/, "").slice(0, 24).replace(/\s+/g, " ");
		if (subj.length > 12 && card.includes(subj)) repeats++;
	}
	ok(repeats <= 2, `previews rarely repeat the subject (${repeats} of ${rows.length})`);
}

// ── 4. the strip ───────────────────────────────────────────────────────────
{
	const seenAll = new Set(rows.map(kOf));
	for (const width of [60, 100, 160]) {
		const out = renderStrip(rows, width, seenAll, rows.length, theme);
		ok(out.length === 4, `strip is 4 lines at width ${width}`);
		ok(out.every((l: string) => visibleWidth(l) <= width), `strip fits ${width} columns`);
	}
	const strip = renderStrip(rows, 120, seenAll, rows.length, theme).join("\n");
	const newest = rows[rows.length - 1];
	ok(strip.includes("toragents · " + rows.length), "strip reports the message count");
	ok(strip.includes(subject(newest.body).slice(0, 18)), "strip shows the newest subject");
	ok(strip.includes("/toragents-panel"), "strip says how to open the full column");
	ok(/^\s+[←→] kisu/.test(strip.split("\n")[1]), "strip names the peer and direction");

	const unseen = new Set(rows.slice(0, -2).map(kOf));
	ok(/\d+ unread/.test(renderStrip(rows, 120, unseen, rows.length, theme).join("\n")), "strip counts unread mail");
	ok(!/unread/.test(strip), "strip is quiet when everything is read");
	ok(renderStrip([], 100, new Set(), 0, null).length === 4, "an empty strip still occupies its lines");
}

// ── 5. placement policy (the contract with the delivery half) ──────────────
{
	const policy = readPolicy(true);
	ok(policyPath() === join(dirname(STATE), "policy.json"), "policy is read from beside the state dir");
	ok(policy.receiver === "rowan", "the fixture policy names its receiver");
	ok(policy.panel.default === "strip", "the fixture policy sets a default placement");

	ok(isReceiver("rowan", policy), "the named receiver is the receiver");
	ok(!isReceiver("gale-fork-3", policy), "everybody else is not");
	ok(!isReceiver("Rowan", policy), "the compare is case-sensitive, exactly like delivery");
	ok(isReceiver(" rowan ", policy), "surrounding whitespace is trimmed on both sides");
	ok(!isReceiver("pi-01a15403", policy), "an id is not a name");
	ok(!isReceiver(undefined, policy), "a nameless session is not the receiver");
	ok(isReceiver("rowan", POLICY_DEFAULTS) === false, "with no receiver named, nobody is the receiver");

	ok(panelMode("rowan", policy) === "column", "the receiver gets the column");
	ok(panelMode("stage", policy) === "column", "a listed session gets the column");
	ok(panelMode("gale-fork-3", policy) === "strip", "everyone else gets the strip");
	ok(panelMode(undefined, policy) === "strip", "an unnamed session gets the strip");

	process.env.TORAGENTS_PANEL = "column";
	ok(panelMode("gale-fork-3", policy) === "column", "TORAGENTS_PANEL=column overrides policy");
	process.env.TORAGENTS_PANEL = "0";
	ok(panelMode("rowan", policy) === "off", "TORAGENTS_PANEL=0 overrides policy");
	delete process.env.TORAGENTS_PANEL;

	// A designation the delivery half does not implement is a designation that
	// silently does nothing, so the shared field and the rule are both asserted.
	const { existsSync, readFileSync } = await import("node:fs");
	const delivery = join(import.meta.dir, "..", "toragents_watch.ts");
	if (existsSync(delivery)) {
		const src = readFileSync(delivery, "utf8");
		ok(/export function mayClaimOwnership/.test(src), "the delivery half still owns the claim rule");
		ok(/POLICY_FILE|policy\.json/.test(src), "the delivery half reads the same policy file");
		ok(/\.receiver/.test(src), "the delivery half reads the same receiver field");
	} else {
		console.log("  (toragents_watch.ts not beside tools/ — cross-half check skipped)");
	}
}

// ── 6. the column against a fake TUI ───────────────────────────────────────
{
	const writes: string[] = [];
	const listeners: Array<(d: string) => any> = [];
	const fakeTerminal = {
		columns: 140,
		rows: 40,
		write: (s: string) => writes.push(s),
		onInput: (fn: (d: string) => any) => {
			listeners.push(fn);
			return () => listeners.splice(listeners.indexOf(fn), 1);
		},
	};
	let renders = 0;
	const fakeTui: any = {
		terminal: fakeTerminal,
		mode: "tui",
		doRender: () => {
			renders++;
		},
		requestRender: () => {},
		routeWheel: () => "pi",
	};
	const col = new SideColumn(fakeTui, {
		width: 46,
		minTerminalWidth: 100,
		render: (w: number, r: number) => renderColumn(rows, w, r, viewAt(rows.length - 1), theme).lines,
		onWheel: () => true,
	});
	const before = fakeTerminal.columns;
	const installed = col.install();
	ok(installed.ok, `column mounted (${installed.why})`);
	ok(fakeTerminal.columns === before - 46 - 1, `pi is narrowed to ${before - 46 - 1} columns`);
	ok(col.mounted, "column reports itself mounted");

	writes.length = 0;
	fakeTui.doRender();
	ok(renders > 0, "doRender still ran pi's own render");
	const painted = writes.join("");
	ok(painted.includes("toragents"), "column content was painted");
	ok(/\x1b\[\d+;\d+H/.test(painted), "column is painted with absolute cursor moves");

	col.dispose();
	ok(fakeTerminal.columns === before, `dispose restores the terminal width (${before})`);
	ok(!col.mounted, "column reports itself unmounted");
	ok(fakeTui.doRender.name !== "wrapped", "doRender hook removed cleanly");

	const narrow = new SideColumn({ ...fakeTui, terminal: { ...fakeTerminal, columns: 90 } }, {
		width: 46,
		minTerminalWidth: 100,
		render: () => [],
		onWheel: () => true,
	});
	const refused = narrow.install();
	ok(!refused.ok && /90/.test(refused.why), "refuses to split a narrow terminal (90 columns; need 100)");
}

console.log(`\n${fails.length === 0 ? "ALL PASS" : "FAIL " + fails.length}`);
for (const f of fails) console.log("  - " + f);
process.exit(fails.length === 0 ? 0 : 1);
