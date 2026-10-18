// Delivery failure must not look like delivery.
// gale found this: after three failed sendUserMessage calls the message kept its
// claim and its announced entry, so every later session skipped it, status said
// "undelivered 0", doctor said "delivered", and the peer had a receipt for a
// message no model ever saw. Run: bun tools/test_failure.ts
import { existsSync, mkdirSync, readdirSync, rmSync, writeFileSync, utimesSync } from "node:fs";
const H = "/tmp/toragents-failure-test";
process.env.TORAGENTS_HOME = H; // before import: STATE is resolved at load
rmSync(H, { recursive: true, force: true });
mkdirSync(`${H}/state/inbox`, { recursive: true });
writeFileSync(
	`${H}/state/inbox/5000-bad1.json`,
	JSON.stringify({ id: "bad1", body: "needs delivery", from_label: "peer", received: 5000 }),
);

let tick: (() => void) | null = null;
(globalThis as any).setInterval = (fn: any) => { tick = fn; return 1; };
(globalThis as any).clearInterval = () => {};
const handlers: Record<string, Function> = {};
const commands: Record<string, any> = {};
const notes: string[] = [];
let fail = true;
const sent: string[] = [];
const mod = await import("../toragents_watch.ts");
mod.default({
	on: (e: string, f: Function) => { handlers[e] = f; },
	registerCommand: (n: string, o: any) => { commands[n] = o; },
	sendUserMessage: async (t: string) => {
		if (fail) throw new Error("simulated transport failure");
		sent.push(t);
	},
} as any);
await handlers.session_start({}, { isIdle: () => true, ui: { notify: (m: string) => notes.push(m) } } as any);
// seeding adopts messages that predate the grace window; this one is "now", so
// leave it to the poll.
for (let i = 0; i < 4; i++) { tick!(); await new Promise((r) => setTimeout(r, 60)); }

console.log("DBG notes:", JSON.stringify(notes.slice(-3)));
const claimed = existsSync(`${H}/state/claimed`) ? readdirSync(`${H}/state/claimed`) : [];
const announced = JSON.parse((await import("node:fs")).readFileSync(`${H}/state/announced.json`, "utf8"));
const failed = existsSync(`${H}/state/failed`) ? readdirSync(`${H}/state/failed`) : [];
const warns = notes.filter((n) => n.includes("NOT delivered"));

console.log(`1. claim released after giving up   claimed=${JSON.stringify(claimed)}   -> ${claimed.length === 0 ? "PASS" : "FAIL (still owned by nobody)"}`);
console.log(`2. announced does not claim success announced=${JSON.stringify(announced)}   -> ${!announced.includes("bad1") ? "PASS" : "FAIL (reported delivered)"}`);
console.log(`3. failure recorded, visibly        failed=${JSON.stringify(failed)}     -> ${failed.includes("bad1") ? "PASS" : "FAIL"}`);
console.log(`4. the human is told it failed      notified=${warns.length}             -> ${warns.length > 0 ? "PASS" : "FAIL"}`);

// a retry must actually retry
fail = false;
await commands["toragents-retry"].handler("", { ui: { notify: () => {} } });
tick!(); await new Promise((r) => setTimeout(r, 80));
console.log(`5. retry delivers the message       delivered=${sent.length}             -> ${sent.length === 1 && sent[0].includes("needs delivery") ? "PASS" : "FAIL"}`);

const ok = claimed.length === 0 && !announced.includes("bad1") && failed.includes("bad1") && warns.length > 0 && sent.length === 1;
console.log(`\n   ${ok ? "PASS" : "FAIL"}`);
if (!ok) process.exit(1);
