// A flood must arrive in bounded turns WITHOUT marking what it did not show.
// Before this, poll claimed and announced the whole batch and rendered 5, so the
// rest were "delivered" to nobody: claimed and announced agreed, doctor printed
// delivered, and no later session ever injected them.
// Run: bun tools/test_batch.ts
import { existsSync, mkdirSync, readdirSync, writeFileSync } from "node:fs";
import { readFileSync } from "node:fs";
const H = "/tmp/toragents-batch-test";
process.env.TORAGENTS_HOME = H;
const { rmSync } = await import("node:fs");
rmSync(H, { recursive: true, force: true });
mkdirSync(`${H}/state/inbox`, { recursive: true });
for (let i = 0; i < 8; i++) {
	writeFileSync(`${H}/state/inbox/${7000 + i}-m${i}.json`,
		JSON.stringify({ id: `m${i}`, body: `flood message ${i}`, from_label: "peer", received: 7000 + i }));
}

let tick: (() => void) | null = null;
(globalThis as any).setInterval = (fn: any) => { tick = fn; return 1; };
(globalThis as any).clearInterval = () => {};
const sent: string[] = [];
const handlers: Record<string, Function> = {};
const mod = await import("../toragents_watch.ts");
mod.default({
	on: (e: string, f: Function) => { handlers[e] = f; },
	registerCommand: () => {},
	sendUserMessage: async (t: string) => { sent.push(t); },
} as any);
await handlers.session_start({}, { isIdle: () => true, ui: { notify: () => {} } } as any);
tick!(); await new Promise((r) => setTimeout(r, 120));

const ids = (dir: string) => (existsSync(dir) ? readdirSync(dir).sort() : []);
const announced1 = JSON.parse(readFileSync(`${H}/state/announced.json`, "utf8")).sort();
const claimed1 = ids(`${H}/state/claimed`);
const shown1 = (sent[0].match(/flood message \d/g) || []).length;

console.log(`1. first turn renders ${shown1}, not 8          -> ${shown1 === 5 ? "PASS" : "FAIL"}`);
console.log(`2. and says so honestly              text=${sent[0].includes("3 more message(s) waiting")} -> ${sent[0].includes("3 more message(s) waiting") ? "PASS" : "FAIL"}`);
console.log(`3. marked exactly what was rendered  announced=${announced1.length} claimed=${claimed1.length} -> ${announced1.length === 5 && claimed1.length === 5 ? "PASS" : "FAIL (marked more than shown)"}`);

tick!(); await new Promise((r) => setTimeout(r, 120));
const announced2 = JSON.parse(readFileSync(`${H}/state/announced.json`, "utf8")).sort();
const shown2 = (sent[1] || "").match(/flood message \d/g) || [];
console.log(`4. the held-back ones arrive next    second turn=${shown2.length} -> ${shown2.length === 3 ? "PASS (nothing stranded)" : "FAIL"}`);
console.log(`5. all 8 end up delivered            announced=${announced2.length} -> ${announced2.length === 8 ? "PASS" : "FAIL"}`);

const ok = shown1 === 5 && sent[0].includes("3 more message(s) waiting") && announced1.length === 5 &&
	claimed1.length === 5 && shown2.length === 3 && announced2.length === 8;
console.log(`\n   ${ok ? "PASS" : "FAIL"}`);
if (!ok) process.exit(1);
