// pi-extension harness. Run with: bun tools/test_watch.ts
// Needs Bun (the extension is TypeScript). It exists because the extension is
// where delivery actually happens, and three of the bugs found in it -- silent
// seeding, mark-more-than-shown, mute stealing mail -- were invisible to every
// other test in this repo.
const H = process.env.TORAGENTS_TEST_HOME || "/tmp/toragents-watch-test";
process.env.TORAGENTS_HOME = H;
const INBOX = H + "/state/inbox";
// Wipe the whole state dir, not just announced.json: claimed/ is the durable
// "who owns this message" memo, and leftovers from a previous run would make
// every message look already-claimed (which is what happened once).
await Bun.$`rm -rf ${H}/state`.quiet();
await Bun.$`mkdir -p ${INBOX}`.quiet();

const { writeFileSync, rmSync } = await import("node:fs");
type Msg = { id: string; body: string; from_label: string; received: number };
const put = (m: Msg) => writeFileSync(`${INBOX}/${m.received}-${m.id}.json`, JSON.stringify(m));

// two pre-existing messages: must be SEEDED, not delivered
put({ id: "old1", body: "old message one", from_label: "maestro-pc", received: 1000 });
put({ id: "old2", body: "old message two", from_label: "maestro-pc", received: 1001 });

let tick: (() => void) | null = null;
(globalThis as any).setInterval = (fn: any) => { tick = fn; return 1; };
(globalThis as any).clearInterval = () => {};

const sent: { text: string; opts: any }[] = [];
const notes: string[] = [];
const handlers: Record<string, Function> = { commands: {} as any };
const fakePi: any = {
  on: (ev: string, fn: Function) => { handlers[ev] = fn; },
  registerCommand: (n: string, o: any) => { (handlers.commands as any)[n] = o; },
  sendUserMessage: async (text: string, opts: any) => { sent.push({ text, opts }); },
};
let idle = true;
const ctx: any = { isIdle: () => idle, ui: { notify: (m: string) => notes.push(m) } };

const mod = await import("../toragents_watch.ts");
mod.default(fakePi);

const settle = () => new Promise((r) => setTimeout(r, 60));

// 1) session start seeds, delivers nothing
await handlers.session_start({}, ctx);
await settle();
console.log(`1. seed on first run        delivered=${sent.length}  notes=${JSON.stringify(notes)}  -> ${sent.length === 0 ? "PASS no spam" : "FAIL"}`);

// 2) a genuinely new message is delivered
put({ id: "new1", body: "hello from the far side", from_label: "maestro-pc", received: 2000 });
tick!(); await settle();
const t = sent[0]?.text || "";
console.log(`2. new message delivered   delivered=${sent.length}  hasBody=${t.includes("hello from the far side")}  fenced=${t.includes("BEGIN PEER MESSAGE")}  -> ${sent.length === 1 && t.includes("hello from the far side") && t.includes("BEGIN PEER MESSAGE") ? "PASS" : "FAIL"}`);

// 3) same message is not delivered twice
tick!(); await settle();
console.log(`3. no duplicate            delivered=${sent.length}  -> ${sent.length === 1 ? "PASS" : "FAIL"}`);

// 4) busy agent -> followUp, never interrupts
idle = false;
put({ id: "new2", body: "second", from_label: "maestro-pc", received: 2001 });
tick!(); await settle();
console.log(`4. busy -> followUp        opts=${JSON.stringify(sent[1]?.opts)}  -> ${sent[1]?.opts?.deliverAs === "followUp" ? "PASS" : "FAIL"}`);
idle = true;

// 5) unparseable file is skipped and NOT lost
writeFileSync(`${INBOX}/2002-broken.json`, "{ not json");
tick!(); await settle();
const before = sent.length;
writeFileSync(`${INBOX}/2002-broken.json`, JSON.stringify({ id: "new3", body: "recovered", from_label: "maestro-pc", received: 2002 }));
tick!(); await settle();
console.log(`5. corrupt then repaired   before=${before} after=${sent.length} got=${(sent[before]?.text || "").includes("recovered")}  -> ${sent.length === before + 1 && (sent[before]?.text || "").includes("recovered") ? "PASS retried, not lost" : "FAIL"}`);

// 7) mute stops delivery and does not consume the message
await (handlers.commands as any)["toragents-mute"].handler("", ctx);
put({ id: "mute1", body: "should stay unread", from_label: "maestro-pc", received: 3000 });
tick!(); await settle();
const whileMuted = sent.length;
await (handlers.commands as any)["toragents-unmute"].handler("", ctx);
tick!(); await settle();
const afterUnmute = sent.length;
console.log(`7. mute then unmute        muted=${whileMuted} after=${afterUnmute} body=${(sent[afterUnmute-1]?.text||"").includes("should stay unread")}  -> ${whileMuted === 3 && afterUnmute === 4 ? "PASS (held, not lost)" : "FAIL"}`);

// 8) the injected header is a stable shape: one from:, carrying the fingerprint
put({ id: "hdr1", body: "header shape", from_label: "maestro-pc", from_fingerprint: "242313a25be2e033af6ef0b56408d531", received: 4000 } as any);
tick!(); await settle();
const hdr = sent[sent.length - 1]?.text || "";
const fromLines = hdr.split("\n").filter((l: string) => l.startsWith("from:"));
console.log(`8. header shape            from-lines=${fromLines.length} hasFp=${(fromLines[0]||"").includes("242313a2")}  -> ${fromLines.length === 1 && (fromLines[0]||"").includes("242313a2") ? "PASS" : "FAIL (duplicate or fingerprintless)"}`);

// 6) /toragents-status works
await (handlers.commands as any)["toragents-status"].handler("", ctx);
console.log(`6. status command          reported=${notes[notes.length - 1]?.split("\n").length} lines  -> ${String(notes[notes.length - 1]).includes("watching") ? "PASS" : "FAIL"}`);
