// One extension "session": load it, seed, run exactly one poll, print what it
// delivered. Spawned twice in parallel by tools/test_race.ts to exercise the
// two-sessions-on-one-box race for real (processes, not a mocked lock).
const H = process.env.TORAGENTS_TEST_HOME!;
process.env.TORAGENTS_HOME = H;
const sent: string[] = [];
let tick: (() => void) | null = null;
(globalThis as any).setInterval = (fn: any) => { tick = fn; return 1; };
(globalThis as any).clearInterval = () => {};
const handlers: Record<string, Function> = {};
const fakePi: any = {
	on: (ev: string, fn: Function) => { handlers[ev] = fn; },
	registerCommand: () => {},
	sendUserMessage: async (text: string) => { sent.push(text); },
};
const ctx: any = { isIdle: () => true, ui: { notify: () => {} } };
const mod = await import("../toragents_watch.ts");
mod.default(fakePi);
await handlers.session_start({}, ctx);
// Both sessions must be live and idle BEFORE the message exists, otherwise the
// seed adopts it as backlog and the race we are testing never happens.
const { writeFileSync, existsSync } = await import("node:fs");
writeFileSync(`${H}/ready-${process.pid}`, "1");
const go = `${H}/go`;
const deadline = Date.now() + 10000;
while (!existsSync(go) && Date.now() < deadline) await new Promise((r) => setTimeout(r, 10));
tick!();
await new Promise((r) => setTimeout(r, 150));
console.log(JSON.stringify({ delivered: sent.length, ids: sent.length }));
process.exit(0);
