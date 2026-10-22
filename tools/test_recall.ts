// Announced, never shown -- the cell that used to be permanent.
//
// A peer's box was found holding three letters in exactly this state: in
// announced.json, no claim file, no transcript row, bodies intact, 28 hours old.
// The startup reconciler healed only the other direction (claim without
// announced), and the tick skips anything announced, so nothing could ever
// reconsider them. These checks assert both halves of the repair: the write that
// made it permanent is gone, and an id already announced is delivered once if no
// row proves it was shown.
//
// Run with: bun tools/test_recall.ts
const H = process.env.TORAGENTS_TEST_HOME || "/tmp/toragents-recall-test";
process.env.TORAGENTS_HOME = H;
const INBOX = H + "/state/inbox";

const { writeFileSync, readFileSync, mkdirSync, rmSync } = await import("node:fs");
rmSync(H + "/state", { recursive: true, force: true });
mkdirSync(INBOX, { recursive: true });
mkdirSync(H + "/state/claimed", { recursive: true });

let pass = 0;
let fail = 0;
const check = (name: string, ok: boolean, detail = "") => {
	console.log(`  ${name} ${ok ? "PASS" : "FAIL"}${detail ? ` (${detail})` : ""}`);
	ok ? pass++ : fail++;
};

const STATE = H + "/state";
const put = (id: string, body: string, received: number) =>
	writeFileSync(
		`${INBOX}/${received}-${id}.json`,
		JSON.stringify({ id, body, from_label: "rowan", received }),
	);

// three bodies; two of them announced, only one of those ever rendered
put("dead1", "this one was announced and forgotten", 1000);
put("fine1", "this one was announced and shown", 1001);
writeFileSync(`${STATE}/announced.json`, JSON.stringify(["dead1", "fine1"]));
// fine1 was delivered: it has a claim AND a transcript row
writeFileSync(`${STATE}/claimed/fine1`, `test-session ${Date.now()}\n`);
writeFileSync(
	`${STATE}/chat.jsonl`,
	JSON.stringify({ at: Date.now(), dir: "in", peer: "rowan", id: "fine1", body: "this one was announced and shown" }) + "\n",
);

let tick: (() => void) | null = null;
(globalThis as any).setInterval = (fn: any) => {
	tick = fn;
	return 1;
};
(globalThis as any).clearInterval = () => {};

const sent: { text: string }[] = [];
const notes: string[] = [];
const handlers: Record<string, any> = { commands: {} };
const fakePi: any = {
	on: (ev: string, fn: Function) => {
		handlers[ev] = fn;
	},
	registerCommand: (n: string, o: any) => {
		handlers.commands[n] = o;
	},
	sendUserMessage: async (text: string) => {
		sent.push({ text });
	},
};
const ctx: any = { isIdle: () => true, ui: { notify: (m: string) => notes.push(m) } };

const mod = await import("../toragents_watch.ts");
mod.default(fakePi);
const settle = () => new Promise((r) => setTimeout(r, 60));

await handlers.session_start({}, ctx);
await settle();

// 1. the notice names what was found, rather than healing silently
check(
	"a startup recall says how many it found",
	notes.some((n) => n.includes("1 message(s) were announced but never shown")),
	JSON.stringify(notes),
);

// 2. the lost one is delivered, the shown one is not
tick!();
await settle();
const text = sent.map((s) => s.text).join("\n");
check("the announced-but-unshown letter is delivered", text.includes("this one was announced and forgotten"), `delivered=${sent.length}`);
check("the one with a transcript row is not shown twice", !text.includes("this one was announced and shown"));
check("it arrives as itself, not as a duplicate of another", sent.length === 1);

// 3. and only once: the row is the proof, so the next start has nothing to recall
tick!();
await settle();
check("a second tick does not repeat it", sent.length === 1);
{
	const rows = readFileSync(`${STATE}/chat.jsonl`, "utf8").trim().split("\n").map((l) => JSON.parse(l));
	check(
		"the transcript now records it, which is what stops the recall",
		rows.some((r: any) => r.id === "dead1" && r.dir === "in"),
		`rows=${rows.length}`,
	);
}

// 4. the recall is derived from the disk every start, not remembered in memory
await handlers.session_start({}, ctx);
await settle();
tick!();
await settle();
check("a restarted window recalls nothing once the row exists", sent.length === 1);

// 5. the ordering that created the cell: nothing writes announced.json before the render
{
	const src = readFileSync(new URL("../toragents_watch.ts", import.meta.url), "utf8");
	const from = src.indexOf("const heldBack = fresh.length - mine.length;");
	const to = src.indexOf("await deliver(ctx, mine, heldBack);", from);
	const between = from >= 0 && to > from ? src.slice(from, to) : "MISSING";
	check(
		"announced.json is not written before deliver()",
		from >= 0 && to > from && !between.includes("writeAnnounced("),
	);
}

// 5b. the tombstone: a delivery record that outlives the transcript
{
	const tomb = readFileSync(`${STATE}/delivered/dead1`, "utf8").trim();
	// window, clock, length -- and never a peer.
	check("a delivery writes a tombstone: window, clock, length", /^\S+ \d+ \d+$/.test(tomb), tomb);
	check(
		"the length is the letter's, so a truncated body would not match",
		Number(tomb.split(" ")[2]) === "this one was announced and forgotten".length,
		tomb,
	);
	check("and it records nothing about who sent it", !/rowan|liam|@|onion/.test(tomb));
	// APPEND, not rewrite: a second render of the same id is two lines, which is the
	// count. writeFileSync made the second delivery indistinguishable from the first.
	check("a second delivery appends rather than overwrites", mod.markDelivered("dead1", 36) === true && readFileSync(`${STATE}/delivered/dead1`, "utf8").trim().split("\n").length === 2);
	// And the failure direction, so the boolean is not a constant: a path that cannot be
	// written returns false rather than an exception and rather than true.
	check(
		"an unwritable tombstone returns false instead of throwing",
		mod.markDelivered("nested/dir", 1) === false,
	);
	// The one thing the comment promises is the thing a tidy-up does first.
	{
		const src = readFileSync(new URL("../toragents_watch.ts", import.meta.url), "utf8");
		const destructive = src
			.split("\n")
			.filter((l) => /unlinkSync|rmSync|rmdirSync|truncate/.test(l) && /DELIVERED_DIR|"delivered"/.test(l));
		check("nothing deletes from delivered/", destructive.length === 0, destructive.join(" | "));
	}
	// The transcript is a convenience; this is the test of whether the recall
	// depends on it. Blank the rows and restart: the tombstone must still hold.
	writeFileSync(`${STATE}/chat.jsonl`, "");
	writeFileSync(`${STATE}/announced.json`, JSON.stringify(["dead1"]));
	rmSync(`${STATE}/claimed/dead1`, { force: true });
	const before = sent.length;
	await handlers.session_start({}, ctx);
	await settle();
	tick!();
	await settle();
	check(
		"a rotated transcript does not resurrect a tombstoned letter",
		sent.length === before,
		`before=${before} after=${sent.length}`,
	);
	// And the other direction, so the check cannot pass by doing nothing: with the
	// tombstone gone too, the same letter IS recalled.
	rmSync(`${STATE}/delivered/dead1`, { force: true });
	await handlers.session_start({}, ctx);
	await settle();
	tick!();
	await settle();
	check("without a tombstone or a row, it is recalled again", sent.length === before + 1);
	writeFileSync(`${STATE}/chat.jsonl`, JSON.stringify({ at: Date.now(), dir: "in", id: "dead1", body: "x" }) + "\n");
}

// 5c. the count knows when it started counting, and an absence is not a zero
{
	const rec = mod.deliveryRecord(STATE, "dead1");
	check("a tombstone reports its count", typeof rec.count === "number" && rec.count >= 1, JSON.stringify(rec));
	check("and the epoch the counting started at", typeof rec.epoch === "number", JSON.stringify(rec));
	check("both records agree, so the source says both", rec.source === "both", JSON.stringify(rec));
	const never = mod.deliveryRecord(STATE, "never-delivered");
	check("a letter with no record at all reports null, not zero", never.count === null, JSON.stringify(never));
	check("and names no source", never.source === null);
	check("the epoch is not itself mistaken for a letter", !mod.deliveredTombs(STATE).has("_epoch"));
}

// 5c-bis. the transcript was counting all along: it is the pre-epoch backfill, not a fallback
{
	// a letter delivered before tombstones existed: a row, no lines
	writeFileSync(
		`${STATE}/chat.jsonl`,
		readFileSync(`${STATE}/chat.jsonl`, "utf8") +
			JSON.stringify({ at: Date.now(), dir: "in", peer: "rowan", id: "pre1", body: "before the epoch" }) + "\n",
	);
	const pre = mod.deliveryRecord(STATE, "pre1", 1);
	check("a row alone is a count, not a null", pre.count === 1, JSON.stringify(pre));
	check("and it names the row as the source", pre.source === "rows", JSON.stringify(pre));
	check("a letter older than the epoch reports a floor", pre.floor === true, JSON.stringify(pre));
	// disagreement in the other direction: two tombstones, one row
	writeFileSync(`${STATE}/delivered/div1`, "s 1 10\ns 2 10\n");
	writeFileSync(
		`${STATE}/chat.jsonl`,
		readFileSync(`${STATE}/chat.jsonl`, "utf8") +
			JSON.stringify({ at: Date.now(), dir: "in", peer: "rowan", id: "div1", body: "x" }) + "\n",
	);
	const div = mod.deliveryRecord(STATE, "div1");
	check("the record that saw more wins, and is named", div.count === 2 && div.source === "lines", JSON.stringify(div));
}

// 5c-ter. can the real path produce a repeat at all?
//
// The honest answer, measured rather than assumed: no. Either record blocks the recall, so
// an id with a tombstone OR a row cannot be re-delivered by this product, and a repeat is
// therefore unreachable today. That matters for the mark: a badge for a state that cannot
// occur is a fixture pretending to be a feature. Both directions asserted, so the guarantee
// is pinned rather than described.
{
	const base = mod.deliveryRecord(STATE, "dead1").count || 1;
	// only the transcript is lost
	rmSync(`${STATE}/delivered/dead1`, { force: true });
	writeFileSync(`${STATE}/chat.jsonl`, "");
	// put the tombstone back but drop the row: source must be lines, and no re-delivery
	writeFileSync(`${STATE}/delivered/dead1`, "s 1 36\n");
	writeFileSync(`${STATE}/announced.json`, JSON.stringify(["dead1"]));
	rmSync(`${STATE}/claimed/dead1`, { force: true });
	const beforeCalls = sent.length;
	await handlers.session_start({}, ctx);
	await settle();
	tick!();
	await settle();
	const onlyLines = mod.deliveryRecord(STATE, "dead1");
	check("a tombstone alone blocks the recall", sent.length === beforeCalls && onlyLines.source === "lines", `calls=${sent.length - beforeCalls} ${JSON.stringify(onlyLines)}`);
	check("and the count did not grow", onlyLines.count === base, `base=${base} now=${onlyLines.count}`);
	// only the tombstone is lost
	rmSync(`${STATE}/delivered/dead1`, { force: true });
	writeFileSync(
		`${STATE}/chat.jsonl`,
		JSON.stringify({ at: Date.now(), dir: "in", peer: "rowan", id: "dead1", body: "x" }) + "\n",
	);
	await handlers.session_start({}, ctx);
	await settle();
	tick!();
	await settle();
	const onlyRows = mod.deliveryRecord(STATE, "dead1");
	check("a row alone blocks the recall", sent.length === beforeCalls && onlyRows.source === "rows", `calls=${sent.length - beforeCalls} ${JSON.stringify(onlyRows)}`);
	check("so a count above one is unreachable through this path today", onlyRows.count === base, `base=${base} now=${onlyRows.count}`);
}

// 5d. an archived body is reported, not silently skipped
{
	// announced, no claim, no tombstone, no row -- and no body: the second permanent
	// cell. It must not disappear into a `continue`.
	writeFileSync(`${STATE}/announced.json`, JSON.stringify(["dead1", "archived1"]));
	rmSync(`${STATE}/delivered/dead1`, { force: true });
	writeFileSync(`${STATE}/chat.jsonl`, "");
	notes.length = 0;
	await handlers.session_start({}, ctx);
	await settle();
	check(
		"a letter announced with no body left is reported, not dropped",
		notes.some((n) => n.includes("no body on disk") ),
		JSON.stringify(notes),
	);
	check("and it is not delivered from nowhere", !sent.join("\n").includes("archived1"));
}

// 6. a peer's three dead letters would be recovered: bounded, and only with a body
{
	rmSync(H + "/state", { recursive: true, force: true });
	mkdirSync(INBOX, { recursive: true });
	mkdirSync(H + "/state/claimed", { recursive: true });
	for (let i = 0; i < 3; i++) put(`late${i}`, `late letter ${i}`, 2000 + i);
	writeFileSync(`${STATE}/announced.json`, JSON.stringify(["late0", "late1", "late2", "gone-body"]));
	const mod2 = await import(`../toragents_watch.ts?fresh=${Date.now()}`);
	const sent2: string[] = [];
	mod2.default({
		on: (ev: string, fn: Function) => {
			handlers[ev] = fn;
		},
		registerCommand: (n: string, o: any) => {
			handlers.commands[n] = o;
		},
		sendUserMessage: async (t: string) => {
			sent2.push(t);
		},
	} as any);
	await handlers.session_start({}, ctx);
	await settle();
	tick!();
	await settle();
	// one sendUserMessage carries the batch, joined by the separator: count bodies, not calls.
	const batch = sent2.join("\n");
	check(
		"three lost letters come back in one batch",
		(batch.match(/late letter \d/g) || []).length === 3,
		`calls=${sent2.length} bodies=${(batch.match(/late letter \d/g) || []).length}`,
	);
	check("an announced id with no body on disk is not invented", !sent2.join("\n").includes("gone-body"));
}

console.log(`\n  ${pass} passed, ${fail} failed`);
process.exit(fail > 0 ? 1 : 0);
