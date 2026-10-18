/**
 * The transcript must show what was SAID and nothing else: one line per
 * direction, newest last, and no entry for anything that never crossed the wire.
 */
import { mkdtempSync, writeFileSync, readFileSync } from "node:fs";
import { tmpdir } from "node:os";
import { join } from "node:path";

const HOME = mkdtempSync(join(tmpdir(), "toragents-chat-"));
process.env.TORAGENTS_HOME = HOME;
process.env.TORAGENTS_STATE = join(HOME, "state");
process.env.PI_SESSION_ID = "cccccccc-1111-2222-3333-444444444444";
const STATE = process.env.TORAGENTS_STATE;
import { mkdirSync } from "node:fs";
mkdirSync(STATE, { recursive: true });

const t0 = 1792340000000;
const rows = [
	{ at: t0, dir: "out", peer: "maestro-pc", id: "a1", body: "first thing sent" },
	{ at: t0 + 60000, dir: "in", peer: "maestro-pc", id: "b2", body: "their answer" },
	{ at: t0 + 120000, dir: "out", peer: "maestro-pc", id: "c3", body: "second thing sent" },
];
writeFileSync(join(STATE, "chat.jsonl"), rows.map((r) => JSON.stringify(r)).join("\n") + "\n");

const mod: any = await import(join(process.cwd(), "toragents_watch.ts"));
const lines = mod.renderChat(56) as string[];
const text = lines.join("\n");

const checks: Array<[string, boolean]> = [
	["shows outgoing with an arrow out", text.includes("→ maestro-pc")],
	["shows incoming with an arrow in", text.includes("← maestro-pc")],
	["shows the words, not the ids", text.includes("first thing sent") && !text.includes("a1 ")],
	["order preserved, newest last",
		text.indexOf("first thing sent") < text.indexOf("their answer") &&
		text.indexOf("their answer") < text.indexOf("second thing sent")],
	["no thinking or tool chatter in the transcript",
		!/tool|thinking|bash/i.test(text)],
	["empty transcript says so instead of rendering nothing",
		(() => { writeFileSync(join(STATE, "chat.jsonl"), ""); return (mod.renderChat() as string[]).join("").includes("no messages yet"); })()],
];
const bad = checks.filter(([, ok]) => !ok);
for (const [n, ok] of checks) console.log(`  ${n.padEnd(52)} ${ok ? "PASS" : "FAIL"}`);
console.log(`\n  ${checks.length - bad.length} passed, ${bad.length} failed`);
process.exit(bad.length ? 1 : 0);
