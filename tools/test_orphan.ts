// A crash between claiming a message and recording it used to strand it forever:
// every session saw EEXIST, adoption skipped it, and no command could clear it.
// Run: bun tools/test_orphan.ts
import { existsSync, mkdirSync, readdirSync, rmSync, utimesSync, writeFileSync } from "node:fs";
const H = "/tmp/toragents-orphan-test";
process.env.TORAGENTS_HOME = H;
rmSync(H, { recursive: true, force: true });
mkdirSync(`${H}/state/inbox`, { recursive: true });
mkdirSync(`${H}/state/claimed`, { recursive: true });

// the message arrived recently (so it is not backlog) but the claim is old and
// announced.json has never heard of it: exactly the kill -9 window.
writeFileSync(`${H}/state/inbox/6000-orph1.json`,
  JSON.stringify({ id: "orph1", body: "stranded by a crash", from_label: "peer", received: 6000 }));
writeFileSync(`${H}/state/claimed/orph1`, "pi-deadbeef 1");
const old = new Date(Date.now() - 30 * 60 * 1000);
utimesSync(`${H}/state/claimed/orph1`, old, old);
writeFileSync(`${H}/state/announced.json`, "[]");

let tick: (() => void) | null = null;
(globalThis as any).setInterval = (fn: any) => { tick = fn; return 1; };
(globalThis as any).clearInterval = () => {};
const sent: string[] = [];
const notes: string[] = [];
const handlers: Record<string, Function> = {};
const mod = await import("../toragents_watch.ts");
mod.default({
  on: (e: string, f: Function) => { handlers[e] = f; },
  registerCommand: () => {},
  sendUserMessage: async (t: string) => { sent.push(t); },
} as any);
await handlers.session_start({}, { isIdle: () => true, ui: { notify: (m: string) => notes.push(m) } } as any);
tick!();
await new Promise((r) => setTimeout(r, 100));

const delivered = sent.some((t) => t.includes("stranded by a crash"));
const released = notes.some((n) => n.includes("stale claim"));
console.log(`1. stale claim detected        notified=${released}            -> ${released ? "PASS" : "FAIL"}`);
console.log(`2. and the message is delivered delivered=${delivered}            -> ${delivered ? "PASS" : "FAIL"}`);
const ok = delivered && released;
console.log(`\n   ${ok ? "PASS" : "FAIL"}`);
if (!ok) process.exit(1);
