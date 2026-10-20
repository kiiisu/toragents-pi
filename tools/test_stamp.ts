/**
 * The side pane shows the last few messages and never ages them out, so a
 * timestamp with no date is a stamp that lies once a day. These checks fail if
 * the pane goes back to bare HH:MM for everything.
 */
import { mkdtempSync, writeFileSync, mkdirSync } from "node:fs";
import { tmpdir } from "node:os";
import { join } from "node:path";

const HOME = mkdtempSync(join(tmpdir(), "toragents-stamp-"));
process.env.TORAGENTS_HOME = HOME;
process.env.TORAGENTS_STATE = join(HOME, "state");
process.env.PI_SESSION_ID = "dddddddd-1111-2222-3333-444444444444";
const STATE = process.env.TORAGENTS_STATE;
mkdirSync(STATE, { recursive: true });

const mod: any = await import(join(process.cwd(), "toragents_watch.ts"));

// A fixed "now": 2026-10-19 12:55 local. Everything is asserted against it so
// the test does not change meaning depending on when it runs.
const now = new Date(2026, 9, 19, 12, 55, 0).getTime();
const at = (y: number, mo: number, d: number, h: number, mi: number) =>
	new Date(y, mo, d, h, mi, 0).getTime();

const stamp = (t: number) => String(mod.stampFor(t, now));
function startOfToday(): number {
	const d = new Date();
	d.setHours(0, 0, 0, 0);
	return d.getTime();
}

const checks: Array<[string, boolean]> = [
	["same day is bare HH:MM", stamp(at(2026, 9, 19, 9, 5)) === "09:05"],
	["last night says yesterday, not a bare time",
		stamp(at(2026, 9, 18, 20, 10)) === "yesterday 20:10"],
	["two days back carries the date",
		stamp(at(2026, 9, 17, 20, 10)) === "10-17 20:10"],
	["an old message cannot be mistaken for today",
		!/^\d\d:\d\d$/.test(stamp(at(2026, 8, 1, 8, 0)))],
	["a future stamp is dated, not shown as a bare time",
		!/^\d\d:\d\d$/.test(stamp(at(2026, 9, 20, 3, 0)))],
];

// Integration: a stale row must render with a date, a fresh one must not.
writeFileSync(
	join(STATE, "chat.jsonl"),
	[
		// "One minute before today started" -- derived from the real clock because
	// renderChat stamps against Date.now(). A fixture pinned to one calendar day passes
	// on that day and fails forever after; the unit checks above may pin now, the
	// integration check cannot, because it exercises the real path. This instant is
	// always calendar-yesterday AND always under 24h old, at any hour the suite runs.
	{ at: startOfToday() - 60_000, dir: "in", peer: "maestro-pc", id: "old1", body: "OLDROW" },
		{ at: Date.now(), dir: "in", peer: "kisu", id: "new1", body: "NEWROW" },
	]
		.map((r) => JSON.stringify(r))
		.join("\n") + "\n",
);
const text = (mod.renderChat(80) as string[]).join("\n");
checks.push(
	["a message from yesterday renders with a date in the pane",
		/yesterday \d\d:\d\d/.test(text)],
	["the stale row is the one carrying it", text.includes("yesterday")],
);

const bad = checks.filter(([, ok]) => !ok);
for (const [n, ok] of checks) console.log(`  ${n.padEnd(52)} ${ok ? "PASS" : "FAIL"}`);
console.log(`\n  ${checks.length - bad.length} passed, ${bad.length} failed`);
process.exit(bad.length ? 1 : 0);
