/**
 * A pi launched from inside another agent session inherits PI_SESSION_ID.
 * If the label trusts that variable, two windows share one name and
 * RECEIVE=targeted can hand one session's mail to the other.
 *
 * Proved against the real module by driving session_start with a fake ctx whose
 * session id deliberately differs from the environment's.
 */
import { mkdtempSync, readFileSync } from "node:fs";
import { tmpdir } from "node:os";
import { join } from "node:path";

const HOME = mkdtempSync(join(tmpdir(), "toragents-label-"));
process.env.TORAGENTS_HOME = HOME;
delete process.env.TORAGENTS_SESSION;
// The inherited, WRONG value:
process.env.PI_SESSION_ID = "aaaaaaaa-1111-2222-3333-444444444444";

let label = "";
let report: any = null;
const handlers: Record<string, any> = {};
const pi = {
	on: (n: string, f: any) => (handlers[n] = f),
	registerCommand: () => {},
};

const mod: any = await import(join(process.cwd(), "toragents_watch.ts"));
mod.default(pi);
const ctx = {
	sessionManager: { getSessionId: () => "bbbbbbbb-9999-8888-7777-666666666666" },
	ui: { notify: () => {} },
};
await handlers["session_start"]({ type: "session_start", reason: "startup" }, ctx);
report = JSON.parse(readFileSync(join(HOME, "state", "extension.json"), "utf8"));

const checks: Array<[string, boolean]> = [
	["report names the ctx session, not the inherited variable",
		report.session === "pi-bbbbbbbb"],
	["not the parent's id", report.session !== "pi-aaaaaaaa"],
	["state is published", report.state === join(HOME, "state")],
];

// An explicit name still wins -- that is the documented escape hatch.
const HOME2 = mkdtempSync(join(tmpdir(), "toragents-label2-"));
process.env.TORAGENTS_HOME = HOME2;
process.env.TORAGENTS_SESSION = "pi-named-by-hand";
const mod2: any = await import(join(process.cwd(), "toragents_watch.ts?v=2"));
mod2.default(pi);
await handlers["session_start"]({ type: "session_start" }, ctx);
const report2 = JSON.parse(readFileSync(join(HOME2, "state", "extension.json"), "utf8"));
checks.push(["TORAGENTS_SESSION still wins", report2.session === "pi-named-by-hand"]);

const bad = checks.filter(([, ok]) => !ok);
for (const [n, ok] of checks) console.log(`  ${n.padEnd(52)} ${ok ? "PASS" : "FAIL"}`);
console.log(`\n  ${checks.length - bad.length} passed, ${bad.length} failed`);
process.exit(bad.length ? 1 : 0);
