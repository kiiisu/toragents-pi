// A first install must not silently swallow the message that proves it works.
// gale-fork-2 found this the hard way: fresh state + one waiting message =
// adopted as backlog, 0 injections, only a transient notice. Mail that arrived
// while the daemon was running and nobody was watching is exactly the case a
// person tests with, so it has to arrive.
// Run: bun tools/test_firstrun.ts
import { existsSync, writeFileSync, mkdirSync, rmSync, readdirSync, utimesSync } from "node:fs";
const H = "/tmp/toragents-firstrun-test";
// MUST be set before the module is imported: STATE is resolved at load time.
// Without this the test reads and claims against the real ~/.toragents.
process.env.TORAGENTS_HOME = H;
const claimed = () => (existsSync(`${H}/state/claimed`) ? readdirSync(`${H}/state/claimed`) : []);
rmSync(H, { recursive: true, force: true });
mkdirSync(`${H}/state/inbox`, { recursive: true });

const put = (id: string, body: string, ageMs: number) => {
  const f = `${H}/state/inbox/${id}.json`;
  writeFileSync(f, JSON.stringify({ id, body, from_label: "peer", received: 1 }));
  const t = new Date(Date.now() - ageMs);
  utimesSync(f, t, t);
};
put("stale1", "history from two hours ago", 2 * 60 * 60 * 1000);
put("fresh1", "arrived two minutes ago, before pi started", 2 * 60 * 1000);

const sent: string[] = [];
let tick: (() => void) | null = null;
(globalThis as any).setInterval = (fn: any) => { tick = fn; return 1; };
(globalThis as any).clearInterval = () => {};
const handlers: Record<string, Function> = {};
const mod = await import("../toragents_watch.ts");
mod.default({
  on: (e: string, f: Function) => { handlers[e] = f; },
  registerCommand: () => {},
  sendUserMessage: async (t: string) => { sent.push(t); },
} as any);
await handlers.session_start({}, { isIdle: () => true, ui: { notify: () => {} } } as any);
tick!(); // the first poll: this is where a recently-arrived message must land
await new Promise((r) => setTimeout(r, 120));

console.log(`1. history is adopted           delivered=${sent.length} ids=${claimed().join(",")}`);
console.log(`2. the recent message arrives   delivered=${sent.length} hasBody=${(sent[0] || "").includes("arrived two minutes ago")}`);

// Adopted mail IS claimed (that is the memo); what matters is that it was
// not turned into a turn.
const okAdopt = !sent.some((t) => t.includes("history from two hours ago"));
const okFresh = sent.length === 1 && sent[0].includes("arrived two minutes ago");
console.log(`   verdict: old mail quiet=${okAdopt}  new mail delivered=${okFresh}  -> ${okAdopt && okFresh ? "PASS" : "FAIL"}`);
if (!(okAdopt && okFresh)) process.exit(1);
