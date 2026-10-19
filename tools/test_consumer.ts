/**
 * Exactly one session may deliver. Every case here is a way the wrong number of
 * sessions ended up consuming: two live windows racing (2026-10-19, three
 * messages delivered into a window nobody was watching), a dead window holding
 * the lock forever, and a stale heartbeat from a suspended process.
 */
import { mkdtempSync, readFileSync, writeFileSync, rmSync } from "node:fs";
import { tmpdir } from "node:os";
import { join } from "node:path";

const D = mkdtempSync(join(tmpdir(), "toragents-consumer-"));
process.env.TORAGENTS_HOME = D;
process.env.TORAGENTS_STATE = join(D, "state");
import { mkdirSync } from "node:fs";
mkdirSync(process.env.TORAGENTS_STATE, { recursive: true });
const S = process.env.TORAGENTS_STATE;

const mod: any = await import(join(process.cwd(), "toragents_watch.ts"));
const now = 1_800_000_000_000;
// "me" must be a real, live pid: a fake one makes the first election look dead
// and every later check fails for the wrong reason.
const me = { pid: process.pid, session: "pi-me" };
// pid 1 is genuinely alive; a made-up pid tests the dead-owner path instead,
// which is how these first came out as failures of the wrong thing.
const other = { pid: 1, session: "pi-other" };

const checks: Array<[string, boolean]> = [];

// 1. first session wins
let r = mod.electConsumer(S, me, now);
checks.push(["the first session claims delivery", r.tookOver && r.owner === "pi-me"]);

// 2. a second session with a live owner does NOT consume
r = mod.electConsumer(S, other, now + 1000);
checks.push(["a second live session stays passive", !r.tookOver]);
checks.push(["ownership is unchanged by the passive session",
	JSON.parse(readFileSync(join(S, "consumer.json"), "utf8")).pid === me.pid]);

// 3. the passive session is not the consumer
checks.push(["non-owner reports it is not the consumer", !mod.isConsumer(S, other.pid, now + 2000)]);

// 4. stale heartbeat is taken over (a suspended or wedged process)
r = mod.electConsumer(S, other, now + mod.CONSUMER_STALE_MS + 5000);
checks.push(["a stale claim is taken over", r.tookOver && r.owner === "pi-other"]);

// 5. dead pid is taken over regardless of freshness
writeFileSync(join(S, "consumer.json"), JSON.stringify({ pid: 1234567, session: "pi-dead", at: now }));
r = mod.electConsumer(S, me, now + 10);
checks.push(["a dead session's claim is taken over", r.tookOver && r.owner === "pi-me"]);

// 6. heartbeat only by the owner, and it refreshes the timestamp
writeFileSync(join(S, "consumer.json"), JSON.stringify({ pid: me.pid, session: "pi-me", at: now }));
const beat = mod.heartbeatConsumer(S, me.pid, "pi-me", now + 30_000);
const stamped = JSON.parse(readFileSync(join(S, "consumer.json"), "utf8")).at;
checks.push(["the owner can heartbeat", beat && stamped === now + 30_000]);
checks.push(["a non-owner must not steal the file via heartbeat",
	mod.heartbeatConsumer(S, other.pid, "pi-other", now + 40_000) === false]);
checks.push(["...and the owner is unchanged after a foreign heartbeat",
	JSON.parse(readFileSync(join(S, "consumer.json"), "utf8")).pid === me.pid]);

// 7. no election file at all: behave like a working channel, do not mute it
rmSync(join(S, "consumer.json"));
checks.push(["a missing election file does not mute delivery", mod.isConsumer(S, me.pid, now)]);

rmSync(D, { recursive: true, force: true });
const bad = checks.filter(([, ok]) => !ok);
for (const [n, ok] of checks) console.log(`  ${n.padEnd(56)} ${ok ? "PASS" : "FAIL"}`);
console.log(`\n  ${checks.length - bad.length} passed, ${bad.length} failed`);
process.exit(bad.length ? 1 : 0);
