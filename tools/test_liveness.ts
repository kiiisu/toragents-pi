#!/usr/bin/env bun
/**
 * Three answers, and the witness that decides the third.
 *
 * The table this replaces had two columns -- "alive" and "not" -- and the second
 * column was doing three jobs: gone, nobody may ask, and the pid was never a pid. A
 * mistyped value that means three things is how a live desk gets declared dead and its
 * mail handed on, which is not a hypothetical here: on any machine where the desk runs
 * as another user, `kill(pid, 0)` answers EPERM for a process that is very much alive.
 *
 * So the probe is asserted against all three answers, the six cells of the decision are
 * asserted one by one, the witness is asserted for age rather than existence (12 of 24
 * heartbeat files on this box were 33 to 40 hours old and nothing reaps them), and the
 * record is asserted to be written once per window rather than once per poll.
 */
import { mkdtempSync, mkdirSync, writeFileSync, readFileSync, existsSync } from "node:fs";
import { tmpdir } from "node:os";
import { join } from "node:path";

const HOME = mkdtempSync(join(tmpdir(), "toragents-liveness-"));
process.env.TORAGENTS_HOME = HOME;
process.env.TORAGENTS_STATE = join(HOME, "state");
const STATE = process.env.TORAGENTS_STATE;
mkdirSync(join(STATE, "live"), { recursive: true });

const mod: any = await import(join(process.cwd(), "toragents_watch.ts"));
const checks: Array<[string, boolean, string?]> = [];
const eq = (name: string, got: any, want: any) =>
	checks.push([name, JSON.stringify(got) === JSON.stringify(want), `got ${JSON.stringify(got)} want ${JSON.stringify(want)}`]);

const errno = (code: string) => Object.assign(new Error(code.toLowerCase()), { code });

// --- the probe: three answers, and only ESRCH is an answer about existence ------
eq("our own pid is alive", mod.probePid(process.pid).signal, "alive");
eq("an impossible pid is gone", mod.probePid(999_999_999).signal, "gone");
eq("EPERM is unknown, not dead", mod.probePid(4242, () => { throw errno("EPERM"); }).signal, "unknown");
eq("EINVAL is unknown too", mod.probePid(4242, () => { throw errno("EINVAL"); }).signal, "unknown");
eq("a thrown non-error is still unknown", mod.probePid(4242, () => { throw new Error("boom"); }).signal, "unknown");
eq("garbage pids never reach the probe", mod.probePid(0, () => { throw new Error("should not run"); }).signal, "gone");
eq("pid 1 is never reported as gone", mod.probePid(1).signal !== "gone", true);
checks.push(["the EPERM reason names the errno", /EPERM/.test(mod.probePid(4242, () => { throw errno("EPERM"); }).why)]);

// --- the six cells --------------------------------------------------------------
const fresh = { found: true, fresh: true };
const stale = { found: true, fresh: false };
const absent = { found: false, fresh: false };
eq("alive + fresh claim holds", mod.decideOwnership("alive", true, stale), "holds");
eq("alive + stale claim hands over", mod.decideOwnership("alive", false, fresh), "handover");
eq("gone + fresh claim hands over", mod.decideOwnership("gone", true, fresh), "handover");
eq("gone + stale claim hands over", mod.decideOwnership("gone", false, stale), "handover");
eq("unknown + fresh heartbeat is MUTED, never a handover", mod.decideOwnership("unknown", true, fresh), "muted");
eq("unknown + no heartbeat is a handover", mod.decideOwnership("unknown", false, absent), "handover");
eq("unknown + stale heartbeat is a handover", mod.decideOwnership("unknown", true, stale), "handover");

// --- the witness: age, never existence ------------------------------------------
const now = 1_800_000_000_000;
const beat = (name: string, rec: any) => writeFileSync(join(STATE, "live", name), JSON.stringify(rec));
beat("a.json", { pid: 111, at: now - 4_000 });
beat("b.json", { pid: 222, at: now - 40 * 3600_000 });
beat("c.json", { pid: 333, at: now - 4_000 });          // second entry for the same pid
beat("d.json", { pid: 333, at: now - 30_000 });
beat("_writer.json", { pid: 111, at: now });            // the marker file is not a heartbeat
writeFileSync(join(STATE, "live", "e.json"), "{not json");
eq("a fresh heartbeat for that pid is found", mod.heartbeatWitness(STATE, 111, now).fresh, true);
eq("an old heartbeat is found and stale", mod.heartbeatWitness(STATE, 222, now).fresh, false);
eq("the freshest entry wins when a pid wrote twice", mod.heartbeatWitness(STATE, 333, now).fresh, true);
eq("an unknown pid has no witness", mod.heartbeatWitness(STATE, 999, now).found, false);
eq("a corrupt witness does not throw", mod.heartbeatWitness(STATE, 111, now).found, true);
checks.push(["the witness reports age, not just presence", typeof mod.heartbeatWitness(STATE, 111, now).ageMs === "number"]);

// --- the record: once per window, and it survives --------------------------------
const log = join(STATE, mod.OWNERSHIP_LOG);
eq("first record writes", mod.recordOwnership(STATE, "muted:111", { why: "EPERM" }, now), true);
eq("the same key inside the window does not write again", mod.recordOwnership(STATE, "muted:111", { why: "EPERM" }, now + 1_000), false);
eq("the same key after the window writes again", mod.recordOwnership(STATE, "muted:111", { why: "EPERM" }, now + 91_000), true);
const lines = readFileSync(log, "utf8").trim().split("\n");
eq("two lines, both parseable", lines.length, 2);
checks.push(["each line carries the obstacle", lines.every((l: string) => JSON.parse(l).why === "EPERM")]);

// --- the election end to end ------------------------------------------------
// pid 1 is the honest fixture for "a live process we may not signal": on macOS and on
// Linux as a normal user it answers EPERM, which is the case this whole commit exists
// for. As root it is signalable and answers alive -- so the assertions follow the probe
// rather than pretending the environment is uniform, and the skip is printed loudly
// instead of quietly passing.
const sig1 = mod.probePid(1).signal;

const S2 = join(HOME, "state2");
mkdirSync(join(S2, "live"), { recursive: true });
writeFileSync(join(S2, "consumer.json"), JSON.stringify({ pid: 1, session: "pi-desk", name: "desk", at: now }));
writeFileSync(join(S2, "live", "desk.json"), JSON.stringify({ pid: 1, at: now - 2_000 }));
const muted = mod.electConsumer(S2, { pid: process.pid, session: "pi-me", name: "me" }, now + 1_000);
if (sig1 === "unknown") {
	eq("muted: this session does not take delivery", muted.tookOver, false);
	eq("muted: it is flagged as an escalation", muted.escalate, "muted");
	checks.push(["muted: the reason names the obstacle, not a candidate", /EPERM|could not ask|not signalable/i.test(muted.reason)]);
	checks.push(["muted: a line was recorded", existsSync(join(S2, mod.OWNERSHIP_LOG))]);
} else {
	console.log(`  skipped the muted case: pid 1 answers "${sig1}" here, so the EPERM path cannot be reached`);
}

// the same unsignalable owner with no heartbeat behind it -> gone, and a silent desk is
// the worse bug. true whether the probe says unknown (no witness) or alive (stale claim).
const S3 = join(HOME, "state3");
mkdirSync(join(S3, "live"), { recursive: true });
writeFileSync(join(S3, "consumer.json"), JSON.stringify({ pid: 1, session: "pi-desk", name: "desk", at: now - 200_000 }));
const taken = mod.electConsumer(S3, { pid: process.pid, session: "pi-me", name: "me" }, now);
eq("no witness: the channel is not left muted", taken.tookOver, true);

const bad = checks.filter(([, ok]) => !ok);
for (const [n, ok, why] of checks) console.log(`  ${n.padEnd(58)} ${ok ? "PASS" : "FAIL"}`) && (ok || console.log(`      ${why}`));
console.log(`\n  ${checks.length - bad.length} passed, ${bad.length} failed`);
process.exit(bad.length ? 1 : 0);
