// Two pi sessions, one inbox, one state directory. Exactly one must deliver.
// Run: bun tools/test_race.ts
import { writeFileSync, mkdirSync, rmSync, readFileSync, existsSync, readdirSync } from "node:fs";
const H = "/tmp/toragents-race-test";
rmSync(H, { recursive: true, force: true });
mkdirSync(`${H}/state/inbox`, { recursive: true });
// fresh claims too: see tools/test_watch.ts
const run = () => Bun.spawnSync({
  cmd: ["bun", "tools/_tick.ts"],
  env: { ...process.env, TORAGENTS_TEST_HOME: H, TORAGENTS_HOME: H },
  cwd: import.meta.dir + "/..",
  stdout: "pipe", stderr: "pipe",
});
// start both before either finishes: that is the race
const p1 = Bun.spawn(["bun", "tools/_tick.ts"], { env: { ...process.env, TORAGENTS_TEST_HOME: H }, cwd: import.meta.dir + "/..", stdout: "pipe", stderr: "pipe" });
const p2 = Bun.spawn(["bun", "tools/_tick.ts"], { env: { ...process.env, TORAGENTS_TEST_HOME: H }, cwd: import.meta.dir + "/..", stdout: "pipe", stderr: "pipe" });
// wait until both sessions have started and seeded, THEN the mail arrives
const ready = async () => {
  for (let i = 0; i < 400; i++) {
    if (readdirSync(H).filter((n) => n.startsWith("ready-")).length >= 2) return true;
    await new Promise((r) => setTimeout(r, 10));
  }
  return false;
};
if (!(await ready())) { console.log("FAIL: sessions did not both start"); process.exit(1); }
writeFileSync(
  `${H}/state/inbox/3000-race1.json`,
  JSON.stringify({ id: "race1", body: "the one that must arrive exactly once", from_label: "peer", received: 3000 }),
);
writeFileSync(`${H}/go`, "1");
const [o1, o2] = await Promise.all([new Response(p1.stdout).text(), new Response(p2.stdout).text()]);
await p1.exited; await p2.exited;
const n = (s: string) => { try { return JSON.parse(s.trim().split("\n").pop()!).delivered as number; } catch { return -1; } };
const total = n(o1) + n(o2);
const claimed = existsSync(`${H}/state/claimed`) ? readdirSync(`${H}/state/claimed`) : [];
const announced = JSON.parse(readFileSync(`${H}/state/announced.json`, "utf8"));
console.log(`1. two sessions, one message   delivered=${n(o1)}+${n(o2)}=${total}   -> ${total === 1 ? "PASS exactly once" : "FAIL (0 = lost, 2 = duplicated)"}`);
console.log(`2. the winner records the claim claimed=${JSON.stringify(claimed)}  -> ${claimed.includes("race1") ? "PASS" : "FAIL"}`);
console.log(`3. announced.json keeps it     announced=${JSON.stringify(announced)}  -> ${announced.includes("race1") ? "PASS" : "FAIL"}`);
if (total !== 1 || !claimed.includes("race1") || !announced.includes("race1")) process.exit(1);

// 4) THE LOSER PATH, deterministically. The concurrent race above almost never
// lands in claim()'s EEXIST branch, because the winner writes announced.json
// first and the loser filters the message out before claiming it. So the branch
// that actually enforces "exactly one" when two sessions do collide is never
// exercised — proved by mutating it (EEXIST -> return true): the file stayed
// green. A claim already held by someone else is reachable on purpose, so test
// that instead of hoping for a scheduling accident.
mkdirSync(`${H}/state/claimed`, { recursive: true });
writeFileSync(`${H}/state/claimed/race2`, "another-session 1");
writeFileSync(
	`${H}/state/inbox/3001-race2.json`,
	JSON.stringify({ id: "race2", body: "already claimed elsewhere", from_label: "peer", received: 3001 }),
);
const held = run();
const heldDelivered = n(held.stdout.toString());
const announced2 = JSON.parse(readFileSync(`${H}/state/announced.json`, "utf8"));
console.log(`4. a claim held elsewhere blocks me  delivered=${heldDelivered}  -> ${heldDelivered === 0 && !announced2.includes("race2") ? "PASS (not taken, not marked)" : "FAIL (delivered or marked while another session owns it)"}`);
rmSync(`${H}/state/claimed/race2`);
const freed = run();
const freedDelivered = n(freed.stdout.toString());
console.log(`5. and it goes out once the claim is gone delivered=${freedDelivered}  -> ${freedDelivered === 1 ? "PASS" : "FAIL"}`);
if (heldDelivered !== 0 || announced2.includes("race2") || freedDelivered !== 1) process.exit(1);

