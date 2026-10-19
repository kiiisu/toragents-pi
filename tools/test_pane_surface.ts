/**
 * The load-bearing guard, tested at the last place that still produces output.
 *
 * Why this file exists: if the pane is handed a component factory in RPC mode,
 * pi emits NOTHING -- no widget, no error, no note. Verified in pi's bundle:
 *
 *   setWidget(key, content, options) {
 *     (content === void 0 || Array.isArray(content)) && output({... widgetLines: content ...})
 *   }
 *
 * A test cannot watch that silence, so it must assert the ARGUMENT instead.
 * That is the observable that cannot go quiet: mode === "tui" => a factory,
 * anything else => an array. "Simplify to factory-only" then fails here, at
 * the boundary we control, before pi's silence is ever in the room.
 *
 * It also pins the two facts we got wrong once and had to be corrected on:
 *   - RPC sends the whole array UNSLICED (the ten-row cap is the TUI array
 *     renderer only, MAX_WIDGET_LINES=10, one branch of one renderer);
 *   - the factory overload is the only one that receives the real width, which
 *     is why the pane was frozen at 56 columns on a wide terminal.
 *
 * This test is expected to PASS today: it pins existing correct behaviour. Its
 * value is entirely in failing later, which is a legitimate job for a pin --
 * unlike a mirror assertion, it can go red.
 */
import { mkdtempSync, writeFileSync, mkdirSync } from "node:fs";
import { tmpdir } from "node:os";
import { join } from "node:path";

const HOME = mkdtempSync(join(tmpdir(), "toragents-pane-"));
process.env.TORAGENTS_HOME = HOME;
process.env.TORAGENTS_STATE = join(HOME, "state");
process.env.TORAGENTS_SOCKS = "1"; // never dialled: nothing here polls
process.env.PI_SESSION_ID = "dddddddd-1111-2222-3333-444444444444";
const STATE = process.env.TORAGENTS_STATE;
mkdirSync(STATE, { recursive: true });

// 14 rows: more than the TUI renderer's 10-row cap, so "unsliced" is visible.
const t0 = 1792340000000;
const rows = Array.from({ length: 14 }, (_, i) => ({
	at: t0 + i * 60000,
	dir: i % 2 === 0 ? "out" : "in",
	peer: "kisu",
	id: `id${String(i).padStart(2, "0")}`,
	body: `row ${i} ` + "x".repeat(200),
}));
writeFileSync(join(STATE, "chat.jsonl"), rows.map((r) => JSON.stringify(r)).join("\n") + "\n");

const mod: any = await import(join(process.cwd(), "toragents_watch.ts"));

const commands: Record<string, any> = {};
const pro = (target: any, make: () => any) =>
	new Proxy(target, { get: (t, k) => (k in t ? t[k] : make()) });
const fakePi = pro(
	{
		on: () => {},
		registerCommand: (name: string, spec: any) => {
			commands[name] = spec;
		},
	},
	() => () => {},
);
if (typeof mod.default !== "function") {
	console.error("FAIL: toragents_watch.ts has no default extension export");
	process.exit(1);
}
await mod.default(fakePi);

const chat = commands["toragents-chat"];
if (!chat || typeof chat.handler !== "function") {
	console.error("FAIL: /toragents-chat is not registered -- the pane cannot be driven");
	process.exit(1);
}

/** Drive the toggle twice: it starts ON, so call 1 hides, call 2 paints. */
async function argumentFor(mode: string): Promise<any> {
	const calls: any[] = [];
	const ctx = {
		mode,
		hasUI: true,
		ui: {
			setWidget: (key: string, content: any) => calls.push({ key, content }),
			notify: () => {},
		},
		isIdle: () => true,
	};
	await chat.handler({}, ctx); // -> hidden (setWidget undefined)
	await chat.handler({}, ctx); // -> painted
	const painted = calls.filter((c) => c.content !== undefined).pop();
	return painted?.content;
}

/** pi's own emit guard, quoted from the bundle. The mechanism under test. */
const piWouldEmit = (content: any) => content === void 0 || Array.isArray(content);

const checks: Array<[string, boolean, string?]> = [];

// --- RPC / json / print: the array branch ---------------------------------
for (const mode of ["rpc", "json", "print"]) {
	const arg = await argumentFor(mode);
	checks.push(
		[`${mode}: pane is given an array, not a factory`, Array.isArray(arg)],
		[
			`${mode}: what we pass survives pi's emit guard`,
			piWouldEmit(arg),
			"a factory here is dropped in silence -- no widget, no error",
		],
		[
			`${mode}: the array is unsliced (14 rows cross intact)`,
			Array.isArray(arg) && arg.length >= 12,
			`got ${Array.isArray(arg) ? arg.length : typeof arg} lines`,
		],
		[
			`${mode}: lines are pre-wrapped at 72, the width we chose`,
			Array.isArray(arg) && arg.every((l: string) => l.length <= 72),
		],
	);
}

// --- TUI: the factory branch ---------------------------------------------
const tui = await argumentFor("tui");
checks.push(
	["tui: pane is given a component factory", typeof tui === "function"],
	["tui: the factory is not an array", !Array.isArray(tui)],
);
if (typeof tui === "function") {
	const component = tui();
	checks.push(
		["tui: the factory returns a component with render()", typeof component?.render === "function"],
	);
	const wide: string[] = component.render(120);
	const narrow: string[] = component.render(60);
	checks.push(
		[
			"tui: render(width) honours the real width (the frozen-56 bug)",
			wide.every((l) => l.length <= 120) && wide.some((l) => l.length > 72),
			"no line exceeded 72 chars, so the width argument was ignored",
		],
		[
			"tui: a narrower render really is narrower",
			narrow.every((l) => l.length <= 60),
		],
		[
			"tui: the factory branch carries no 10-row cap of pi's",
			wide.length > 10,
			`got ${wide.length} rows -- MAX_WIDGET_LINES is the tui ARRAY branch only`,
		],
		[
			"tui: the factory result would be DROPPED by pi's rpc guard",
			!piWouldEmit(component),
			"encodes why the mode branch above must not be simplified away",
		],
	);
}

let bad = 0;
for (const [label, ok, detail] of checks) {
	if (!ok) bad++;
	console.log(`${ok ? "PASS" : "FAIL"}  ${label}${!ok && detail ? `  -- ${detail}` : ""}`);
}
console.log(bad === 0 ? `\nall ${checks.length} checks pass` : `\n${bad} FAILED of ${checks.length}`);
process.exit(bad === 0 ? 0 : 1);
