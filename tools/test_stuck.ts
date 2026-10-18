// A failure marker that cannot be written must not turn into an infinite retry.
// markFailed used to swallow its own write error and rely on the unclaim, which
// re-claimed the id every tick: 3 failed sends, a warning, and round again.
// Run: bun tools/test_stuck.ts
import { existsSync, mkdirSync, readdirSync, rmSync, writeFileSync } from "node:fs";
const H = "/tmp/toragents-stuck-test";
process.env.TORAGENTS_HOME = H;
rmSync(H, { recursive: true, force: true });
mkdirSync(`${H}/state/inbox`, { recursive: true });
// make failed/ impossible to create: a FILE occupies its path
writeFileSync(`${H}/state/failed`, "not a directory");
writeFileSync(`${H}/state/inbox/8000-stuck1.json`,
	JSON.stringify({ id: "stuck1", body: "cannot be delivered", from_label: "peer", received: 8000 }));

let tick: (() => void) | null = null;
(globalThis as any).setInterval = (fn: any) => { tick = fn; return 1; };
(globalThis as any).clearInterval = () => {};
const notes: string[] = [];
let attempts = 0;
const handlers: Record<string, Function> = {};
const mod = await import("../toragents_watch.ts");
mod.default({
	on: (e: string, f: Function) => { handlers[e] = f; },
	registerCommand: () => {},
	sendUserMessage: async () => { attempts++; throw new Error("transport down"); },
} as any);
await handlers.session_start({}, { isIdle: () => true, ui: { notify: (m: string) => notes.push(m) } } as any);
for (let i = 0; i < 20; i++) { tick!(); await new Promise((r) => setTimeout(r, 30)); }

const warns = notes.filter((n) => n.includes("could not deliver"));
const claimed = existsSync(`${H}/state/claimed`) ? readdirSync(`${H}/state/claimed`) : [];
console.log(`1. attempts bounded over 20 ticks    attempts=${attempts}  -> ${attempts <= 3 ? "PASS (no hot loop)" : "FAIL (looping)"}`);
console.log(`2. the human warned once, not 20x    warnings=${warns.length}  -> ${warns.length === 1 ? "PASS" : "FAIL"}`);
console.log(`3. message stays visibly un-delivered claimed=${JSON.stringify(claimed)} -> ${claimed.includes("stuck1") ? "PASS (still owned, not announced)" : "FAIL"}`);
const ok = attempts <= 3 && warns.length === 1 && claimed.includes("stuck1");
console.log(`\n   ${ok ? "PASS" : "FAIL"}`);
if (!ok) process.exit(1);
