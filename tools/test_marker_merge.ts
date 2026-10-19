#!/usr/bin/env bun
/**
 * A marker is one account per window, and more than one module may attest to it.
 *
 * The watcher rewrites state/live/<label>.json and _writer.json every tick. When the
 * reader eventually adds its own version to that same file -- which is the agreed
 * design, because a second file would be a second account of the same window -- a
 * blanket rewrite here would delete the field on the next tick. The symptom would be a
 * version that appears on mount and vanishes a second later: a bug that looks like a
 * renderer glitch and sends everyone to the wrong file.
 *
 * So: merge, never replace. This asserts the property BEFORE the second writer exists,
 * because the cost of finding out afterwards is a silent, intermittent field deletion.
 */
import { mkdtempSync, writeFileSync, readFileSync, mkdirSync } from "node:fs";
import { join } from "node:path";
import { tmpdir } from "node:os";

const { markLive, EXT_VERSION } = await import("../toragents_watch.ts");

let pass = 0;
const fails: string[] = [];
function ok(cond: boolean, what: string): void {
	if (cond) { pass++; console.log(`ok   ${what}`); }
	else { fails.push(what); console.log(`FAIL ${what}`); }
}
const read = (p: string) => JSON.parse(readFileSync(p, "utf8"));

const state = mkdtempSync(join(tmpdir(), "marker-merge-"));
const dir = join(state, "live");
mkdirSync(dir, { recursive: true });

// A field written by a module that is not the watcher (the future reader).
writeFileSync(join(dir, "_writer.json"),
	JSON.stringify({ v: 2, session: "old", pid: 1, at: 1, code: "1", panel: 7, note: "kept" }));
writeFileSync(join(dir, "pi-a.json"),
	JSON.stringify({ session: "pi-a", pid: 1, at: 1, code: "1", panel: 7 }));

markLive(state, "pi-a", process.pid, 1234);

const w = read(join(dir, "_writer.json"));
const rec = read(join(dir, "pi-a.json"));

ok(w.panel === 7, "a foreign field survives the writer's tick (_writer.json)");
ok(w.note === "kept", "every foreign field survives, not just the one we anticipated");
ok(w.code === EXT_VERSION, "our own field is updated to the running build");
ok(w.at === 1234, "our own timestamp is updated");
ok(rec.panel === 7, "a foreign field survives on the per-window record too");
ok(rec.code === EXT_VERSION, "the per-window record also carries the build");
ok(rec.session === "pi-a", "the window name is ours and unchanged");

// A corrupt file must not stop the heartbeat: presence is an optimisation, and a
// window that dies silently over someone else's bad JSON is worse than a lost field.
// old-marker-by-design: a corrupt marker must not stop the heartbeat
writeFileSync(join(dir, "_writer.json"), "{not json");
markLive(state, "pi-a", process.pid, 99);
const repaired = read(join(dir, "_writer.json"));
ok(repaired.code === EXT_VERSION, "a corrupt marker is replaced, not fatal");
ok(repaired.session === "pi-a", "the repaired marker is this window's");

if (fails.length) {
	console.log(`\n${fails.length} FAILED`);
	process.exit(1);
}
console.log(`\nALL PASS (${pass} checks)`);
