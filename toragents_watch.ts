/**
 * toragents-inbox — incoming Tor messages become user turns in this session.
 *
 * The problem this solves:
 *   The toragents daemon already accepts signed messages from an allowlisted
 *   peer and writes them to <state>/inbox/*.json. But nothing told the agent.
 *   Every message sat there until a human typed "check your inbox", which makes
 *   the channel a mailbox, not a conversation. This closes that gap: a message
 *   arriving over the onion appears in the session by itself.
 *
 * Why an extension and not the daemon calling something:
 *   Only the process that owns the session can put a message into it. pi
 *   exposes exactly that as pi.sendUserMessage(), so the receiving side has to
 *   poll. The daemon stays dumb on purpose: it writes files, this watches them.
 *   That also means it works for any peer, over any transport the daemon uses.
 *
 * Design notes:
 *   - started on session_start, stopped on session_shutdown: the docs are
 *     explicit that a factory must not start timers, because pi loads
 *     extensions in invocations that have no session.
 *   - <state>/announced.json remembers which ids were already delivered, so a
 *     restart does not replay history. On the very first run it seeds with what
 *     is already on disk and says so, rather than dumping 10 old messages.
 *   - a file that is mid-write or unparseable is SKIPPED and not marked seen,
 *     so the next poll retries it. Marking it seen on a parse error would lose
 *     the message permanently.
 *   - delivery is followUp when the agent is busy, so a message never
 *     interrupts a running turn or wedges the session.
 *
 * Honest caveat, deliberately not hidden: the body is text from a remote
 * machine, and it is delivered as a user message. For an allowlisted,
 * signature-verified peer that is the whole point — but it means the peer's
 * content can instruct this agent. It is fenced with a visible marker so the
 * origin is never ambiguous.
 */

import {
	appendFileSync,
	existsSync,
	mkdirSync,
	readFileSync,
	readdirSync,
	renameSync,
	statSync,
	unlinkSync,
	writeFileSync,
} from "node:fs";
import { homedir, tmpdir } from "node:os";
import { join } from "node:path";
import { spawnSync } from "node:child_process";
import { mkdtempSync } from "node:fs";
import type { ExtensionAPI, ExtensionContext } from "@earendil-works/pi-coding-agent";

/**
 * Which directory is the daemon actually writing to?
 *
 * Two layouts exist and guessing wrong means the watcher silently watches an
 * empty directory forever: an install keeps <home>/state/inbox, while a daemon
 * driven from the control port (TORAGENTS_STATE) keeps identity.json, peers.json
 * and inbox/ flat in one directory. So probe for a real inbox instead of
 * assuming, and report which one was chosen.
 */
function resolveState(): { dir: string; why: string } {
	const home = process.env.TORAGENTS_HOME || join(homedir(), ".toragents");
	// Explicit configuration always wins outright. Only the DEFAULTS get scored,
	// because an explicit path outranking nothing is the whole point of setting
	// it -- scoring an explicit TORAGENTS_HOME against /var/lib would silently
	// watch the wrong directory on a machine that has both.
	if (process.env.TORAGENTS_STATE) {
		return { dir: process.env.TORAGENTS_STATE, why: "TORAGENTS_STATE (explicit)" };
	}
	if (process.env.TORAGENTS_HOME) {
		return { dir: join(home, "state"), why: "TORAGENTS_HOME/state (explicit)" };
	}
	// No explicit config: score the defaults. A stray empty <home>/state/inbox
	// (left by a previous run of this very extension) must not outrank the
	// directory that actually holds the daemon's identity and messages.
	const candidates: [string, string][] = [];
	for (const [dir, why] of [
		[join(home, "state"), "~/.toragents/state"],
		["/var/lib/toragents-inbox", "flat state dir"],
	] as [string, string][]) {
		if (!candidates.some(([d]) => d === dir)) candidates.push([dir, why]);
	}
	const score = (dir: string): number => {
		let n = 0;
		try {
			if (existsSync(join(dir, "identity.json"))) n += 2;
			if (existsSync(join(dir, "inbox"))) n += 1;
			if (readdirSync(join(dir, "inbox")).some((f) => f.endsWith(".json"))) n += 1;
		} catch {
			return 0;
		}
		return n;
	};
	let best = { dir: candidates[0][0], why: "default, created on first run" };
	let bestScore = 0;
	for (const [dir, why] of candidates) {
		const sc = score(dir);
		if (sc > bestScore) {
			bestScore = sc;
			best = { dir, why: `${why} (score ${sc})` };
		}
	}
	return best;;
}

const RESOLVED = resolveState();
const STATE = RESOLVED.dir;
const INBOX = join(STATE, "inbox");
const ANNOUNCED = join(STATE, "announced.json");
// pi runs several sessions on one box against ONE state directory, so "has this
// been delivered?" is a question two processes answer at the same time. A set
// read then written back is a lost-update race: both sessions see the same
// pending file, both deliver it, and whichever writes last erases the other's
// ids so the NEXT session replays them as fresh turns.
//
// So a message is claimed by creating <state>/claimed/<id> with O_EXCL, which
// the kernel arbitrates for us. No lock is needed to decide ownership: the
// create either wins or gets EEXIST. announced.json stays as a readable mirror.
const CLAIM_DIR = join(STATE, "claimed");
// Delivery that failed permanently goes here. A message that cannot be shown
// must not look delivered: the peer already has a receipt, so silence is the
// only thing worse than an error.
const FAILED_DIR = join(STATE, "failed");
// What THIS process actually resolved, published for `inbox doctor`. Without it
// doctor can only read its own shell, which is a different environment from the
// one pi runs with -- and then its labels describe the wrong configuration.
const INFO_FILE = join(STATE, "extension.json");

/**
 * What was SAID, not what the agent did to say it. Only two things ever cross
 * the wire: a body we sent and a body we received. So a log of those two is,
 * by construction, the final-answers-only view -- thinking and tool churn never
 * appear in it because they never leave the machine.
 */
const CHAT_FILE = join(STATE, "chat.jsonl");
const CHAT_SHOWN = 12;

function logSaid(dir: "in" | "out", peer: string, body: string, id: string) {
	try {
		mkdirSync(STATE, { recursive: true });
		appendFileSync(
			CHAT_FILE,
			JSON.stringify({ at: Date.now(), dir, peer, id, body: String(body).slice(0, 2000) }) + "\n",
		);
	} catch {
		/* the transcript is a convenience; never let it break a delivery */
	}
}

function saidLog(): Array<{ at: number; dir: string; peer: string; body: string }> {
	try {
		return readFileSync(CHAT_FILE, "utf8")
			.split("\n")
			.filter(Boolean)
			.slice(-200)
			.map((l) => JSON.parse(l));
	} catch {
		return [];
	}
}

/**
 * Voice for a peer, read at compose time. Not remembered between messages: a
 * register that lives only in an agent's head is the one that drifts.
 */
export function peerTone(stateDir: string, label: string): string {
	try {
		const peers = JSON.parse(readFileSync(join(stateDir, "peers.json"), "utf8"));
		const want = label.trim().toLowerCase();
		for (const v of Object.values<any>(peers)) {
			if ((v?.label || "").toLowerCase() === want) return (v.tone || "").trim();
		}
	} catch {
		/* no allowlist, or unreadable: no tone is a valid answer */
	}
	return "";
}

/**
 * Fresh words for the next message, and a mark on the ones this body used.
 * The pool grows because `lexicon add` appends, and it stays fresh because
 * offering a word counts as using it -- otherwise the same three come back.
 */
export function freshWords(
	py: string,
	agent: string,
	root: string,
	bodyFile: string,
	styles = "",
) {
	try {
		const args = [
			agent, "lexicon", "pick", "--count", "3", "--json",
			// Rare words (hunni) appear roughly one message in seven: often enough
			// to be affectionate, rarely enough to stay a treat.
			"--rare-chance", "0.15",
			"--body-file", bodyFile,
		];
		if (styles) args.push("--style", styles);
		const res = spawnSync(
			py,
			args,
			{ encoding: "utf8", env: { ...process.env, TORAGENTS_HOME: root }, timeout: 20_000 },
		);
		const j = JSON.parse(res.stdout || "{}");
		const texts = (k: string) => (j?.picked?.[k] || []).map((e: any) => e.text);
		return { names: texts("names"), moves: texts("moves"), sizes: j?.sizes || {} };
	} catch {
		return { names: [], moves: [], sizes: {} };
	}
}

/** A peer's preferred decks, as "a,b". Empty means every style. */
export function peerStyles(stateDir: string, label: string): string {
	try {
		const peers = JSON.parse(readFileSync(join(stateDir, "peers.json"), "utf8"));
		const want = label.trim().toLowerCase();
		for (const v of Object.values<any>(peers)) {
			if ((v?.label || "").toLowerCase() === want) return (v.styles || []).join(",");
		}
	} catch {
		/* no allowlist: every style is a fine default */
	}
	return "";
}

/**
 * The register, in the model's context BEFORE it composes.
 *
 * This exists because a peer read the code and pointed out that peerTone() only
 * ran inside the send tool, after the body already existed: the "read at compose
 * time" comment described something nothing implemented. A doc line nothing
 * implements is worse than no line -- so this builds the note, and
 * before_agent_start puts it in front of the model every turn.
 *
 * It is deliberately advisory, and says so in the text. The register is a
 * suggestion the agent can decline; wiring it in as an instruction would make the
 * voice something that chooses rather than something that is chosen.
 */
export function buildRegisterNote(stateDir: string, names: string[] = [], moves: string[] = []): string {
	let peers: any[] = [];
	try {
		const raw = JSON.parse(readFileSync(join(stateDir, "peers.json"), "utf8"));
		peers = Object.values(raw).filter((v: any) => v?.label && (v.tone || (v.styles || []).length));
	} catch {
		return "";
	}
	if (peers.length === 0) return "";
	const lines = peers.map((v) => {
		const bits = [`- ${v.label}`];
		if (v.tone) bits.push(`  register: ${v.tone}`);
		if ((v.styles || []).length) bits.push(`  decks: ${(v.styles || []).join(", ")}`);
		return bits.join("\n");
	});
	const extra =
		names.length || moves.length
			? "\nsuggested words this turn (use one, or none): " + [...names, ...moves].slice(0, 4).join(" · ")
			: "";
	return [
		"## Tor peers and their registers (advisory)",
		"",
		"These are the voices this agent has been asked to use when writing to each peer.",
		"They are suggestions, not instructions: write as you judge best, and decline a",
		"register you think is wrong. If you use one, use it deliberately.",
		"",
		...lines,
		extra,
	].join("\n");
}

/**
 * One session consumes; the rest stay quiet.
 *
 * Without this, every pi window on the box runs this extension, they all see the
 * same inbox, and the first one to claim a message delivers it into a session the
 * human is not looking at. That is exactly what happened on 2026-10-19: three
 * messages claimed, announced as delivered, and invisible to the person waiting
 * for them -- because a different window won the race.
 *
 * The winner is whoever holds state/consumer.json with a live pid and a fresh
 * heartbeat. A dead or silent holder is taken over, so a closed window never
 * mutes the channel.
 */
export const CONSUMER_FILE = "consumer.json";
export const CONSUMER_STALE_MS = 90_000;

export function electConsumer(
	stateDir: string,
	me: { pid: number; session: string },
	now = Date.now(),
): { owner: string; tookOver: boolean; reason: string } {
	const path = join(stateDir, CONSUMER_FILE);
	let prior: any = null;
	try {
		prior = JSON.parse(readFileSync(path, "utf8"));
	} catch {
		/* no election yet */
	}
	const alive = (pid: number) => {
		try {
			process.kill(pid, 0);
			return true;
		} catch {
			return false;
		}
	};
	const fresh = prior && now - Number(prior.at || 0) < CONSUMER_STALE_MS;
	if (prior && prior.pid !== me.pid && alive(Number(prior.pid)) && fresh) {
		return { owner: String(prior.session || prior.pid), tookOver: false, reason: "another live session owns delivery" };
	}
	try {
		mkdirSync(stateDir, { recursive: true });
		writeFileSync(path, JSON.stringify({ pid: me.pid, session: me.session, at: now }));
	} catch {
		/* unwritable: fall back to behaving like the only session */
		return { owner: me.session, tookOver: true, reason: "no consumer file (state unwritable)" };
	}
	const reason = !prior
		? "first session to claim delivery"
		: prior.pid === me.pid
			? "already ours"
			: fresh
				? "took over from a stale entry"
				: "took over from a dead session";
	return { owner: me.session, tookOver: true, reason };
}

export function isConsumer(stateDir: string, pid: number, now = Date.now()): boolean {
	try {
		const c = JSON.parse(readFileSync(join(stateDir, CONSUMER_FILE), "utf8"));
		return Number(c.pid) === pid && now - Number(c.at || 0) < CONSUMER_STALE_MS;
	} catch {
		return true; // no election recorded: do not mute a working channel
	}
}

export function heartbeatConsumer(stateDir: string, pid: number, session: string, now = Date.now()) {
	try {
		const c = JSON.parse(readFileSync(join(stateDir, CONSUMER_FILE), "utf8"));
		if (Number(c.pid) !== pid) return false; // someone else owns it; leave it alone
	} catch {
		/* write one below */
	}
	try {
		writeFileSync(join(stateDir, CONSUMER_FILE), JSON.stringify({ pid, session, at: now }));
		return true;
	} catch {
		return false;
	}
}

/**
 * "20:10" for today, "yesterday 20:10" for yesterday, "10-17 20:10" before that.
 *
 * The pane keeps a rolling window of the last few messages and nothing ages out,
 * so a message from last night sat there looking exactly like one from a minute
 * ago: "20:10" read as today, and the pane was quietly 16 hours out of date. A
 * clock without a date lies once a day, every day.
 */
export function stampFor(at: number, now: number = Date.now()): string {
	const d = new Date(at);
	const n = new Date(now);
	const hh = String(d.getHours()).padStart(2, "0");
	const mm = String(d.getMinutes()).padStart(2, "0");
	const sameDay =
		d.getFullYear() === n.getFullYear() &&
		d.getMonth() === n.getMonth() &&
		d.getDate() === n.getDate();
	if (sameDay) return `${hh}:${mm}`;
	const age = now - at;
	if (age >= 0 && age < 24 * 3600 * 1000) return `yesterday ${hh}:${mm}`;
	const mo = String(d.getMonth() + 1).padStart(2, "0");
	const day = String(d.getDate()).padStart(2, "0");
	return `${mo}-${day} ${hh}:${mm}`;
}

/** Plain text lines, so this can be tested and printed without a TUI. */
export function renderChat(width = 56): string[] {
	const rows = saidLog().slice(-CHAT_SHOWN);
	if (rows.length === 0) return [" toragents · no messages yet"];
	const out: string[] = [];
	for (const r of rows) {
		const when = stampFor(r.at);
		const who = `${r.dir === "out" ? "→" : "←"} ${r.peer}  ${when}`;
		out.push(` ${who}`);
		const flat = r.body.replace(/\s+/g, " ").trim();
		const room = Math.max(20, width - 4);
		const cap = room * 2;                       // two lines per message, whatever the width
		const shown = flat.length > cap ? flat.slice(0, Math.max(1, cap - 1)) + "…" : flat;
		for (let i = 0; i < shown.length; i += room) {
			out.push(`   ${shown.slice(i, i + room)}`);
		}
	}
	return out;
}
// A claim with no announced entry is an orphan: the process died between the
// two writes (or was killed at shutdown). Older than this, it is safe to take.
const ORPHAN_MS = 5 * 60 * 1000;

/**
 * Who this session is, for the claim record, for doctor, and for addressing.
 *
 * Deliberately NOT from PI_SESSION_ID alone. pi exports PI_SESSION_ID to its
 * children, and a pi started from inside another agent session INHERITS that
 * variable -- so process.env.PI_SESSION_ID can name a different process than the
 * one running this code. Two windows would share one label, and under
 * TORAGENTS_RECEIVE=targeted either could claim mail addressed to the other.
 *
 * ctx.sessionManager.getSessionId() is this process's own id. It is set at
 * session_start, before the first poll, so claims always see the real answer.
 */
let MY_SESSION = "";
function sessionLabel(): string {
	// Never the literal "unknown": a claim has to say WHICH process holds it, or
	// two unnamed windows look identical on disk and the orphan heal cannot tell
	// a live owner from a dead one.
	return MY_SESSION || `pid-${process.pid}`;
}

/** Resolve once, from the most authoritative source available. */
function resolveSession(ctx?: any): string {
	const explicit = (process.env.TORAGENTS_SESSION || "").trim();
	if (explicit) return explicit.slice(0, 64);
	let own = "";
	try {
		own = String(ctx?.sessionManager?.getSessionId?.() ?? "").trim();
	} catch {
		own = "";
	}
	// Fall back to the environment only when the context cannot answer.
	const id = (own || process.env.PI_SESSION_ID || "").replace(/-/g, "");
	return id ? `pi-${id.slice(0, 8)}` : "";
}

function failedIds(): Set<string> {
	try {
		return new Set(readdirSync(FAILED_DIR).filter((n) => n && !n.startsWith(".")));
	} catch {
		return new Set();
	}
}

function markFailed(id: string, error: string, from: string): boolean {
	// Returns false when the marker could not be written. The caller must then
	// keep the id out of the retry path some other way: unclaiming it while the
	// marker fails would re-claim it every tick, fail three times, warn again,
	// and loop forever on a disk that will not take the write.
	try {
		mkdirSync(FAILED_DIR, { recursive: true });
		writeFileSync(
			join(FAILED_DIR, id),
			JSON.stringify({ at: Date.now(), error: error.slice(0, 300), from, session: sessionLabel() }),
		);
		return true;
	} catch {
		return false;
	}
}
const LOCK = join(STATE, "announced.lock");

/** True only for the one process that created the claim file. */
function claim(id: string): boolean {
	try {
		mkdirSync(CLAIM_DIR, { recursive: true });
		writeFileSync(join(CLAIM_DIR, id), `${sessionLabel()} ${Date.now()}`, { flag: "wx" });
		return true;
	} catch (e: any) {
		if (e && e.code === "EEXIST") return false;
		// A claim we cannot record is a claim we must not act on: better to skip
		// this message this tick than to deliver it twice.
		return false;
	}
}

function unclaim(id: string): void {
	try {
		unlinkSync(join(CLAIM_DIR, id));
	} catch {
		/* already gone */
	}
}

/** Short critical section around announced.json. Stale locks are taken over so
 *  a crashed session cannot wedge delivery forever. */
function withLock<T>(fn: () => T): T {
	const started = Date.now();
	for (;;) {
		try {
			mkdirSync(STATE, { recursive: true });
			writeFileSync(LOCK, String(process.pid), { flag: "wx" });
			break;
		} catch {
			// Take over a lock left behind by a dead session: 5s is far longer
			// than any read-modify-write here, and far shorter than a human's
			// patience for a stuck inbox.
			try {
				const age = Date.now() - statSync(LOCK).mtimeMs;
				if (age > 5000) {
					unlinkSync(LOCK);
					continue;
				}
			} catch {
				/* lock vanished between the two calls: retry */
			}
			if (Date.now() - started > 2000) break; // ceiling, then proceed unlocked
			const until = Date.now() + 15;
			while (Date.now() < until) {
				/* brief spin: this is a sub-millisecond critical section */
			}
		}
	}
	try {
		return fn();
	} finally {
		try {
			unlinkSync(LOCK);
		} catch {
			/* not ours to remove */
		}
	}
}
const POLL_MS = Number(process.env.TORAGENTS_POLL_MS || 3000);

const MAX_BODY = 4000;
const MAX_BATCH = 5;

/**
 * How much of this machine's mail this session wants.
 *
 *   all      (default) everything, including untargeted mail
 *   targeted only messages addressed to this session by name
 *
 * The default is right for the two-person case the transport exists for. It is
 * wrong on a box running a dozen sessions: untargeted mail used to reach every
 * one of them, so one peer talking at 20-second intervals flooded all of them.
 */
const RECEIVE = (process.env.TORAGENTS_RECEIVE || "all").trim().toLowerCase();

/** Set by /toragents-mute. Survives restarts, per session, no config file. */
const MUTE_DIR = join(STATE, "muted");
// Mail that landed this recently is treated as new on a first run rather than
// as backlog. Long enough to cover a slow install, short enough that a real
// history does not replay as a burst of turns.
const FIRST_RUN_GRACE_MS = 10 * 60 * 1000;
function muteKey(): string {
	return (MY_SESSION || `pid-${process.pid}`).replace(/[^A-Za-z0-9._-]/g, "_");
}
function isMuted(): boolean {
	try {
		return existsSync(join(MUTE_DIR, muteKey()));
	} catch {
		return false;
	}
}
function setMuted(on: boolean): boolean {
	try {
		mkdirSync(MUTE_DIR, { recursive: true });
		const f = join(MUTE_DIR, muteKey());
		if (on) writeFileSync(f, String(Date.now()));
		else if (existsSync(f)) unlinkSync(f);
		return true;
	} catch {
		return false;
	}
}

interface RemoteMessage {
	id: string;
	body: string;
	from_label?: string;
	from_session?: string;
	to_session?: string;
	from_fingerprint?: string;
	from_onion?: string;
	received?: number;
}

function readAnnounced(): Set<string> | null {
	try {
		const d = JSON.parse(readFileSync(ANNOUNCED, "utf8"));
		if (Array.isArray(d)) return new Set(d.filter((x) => typeof x === "string"));
		return null;
	} catch {
		return null;
	}
}

/** Forget a message: memory AND disk. writeAnnounced only ever adds, so an
 *  in-memory delete was resurrected by the next merge -- which made a failed
 *  delivery look delivered and never retried. */
function forget(announced: Set<string>, id: string): void {
	announced.delete(id);
	try {
		withLock(() => {
			const disk = readAnnounced();
			if (!disk || !disk.has(id)) return;
			disk.delete(id);
			const tmp = `${ANNOUNCED}.tmp`;
			writeFileSync(tmp, JSON.stringify([...disk]));
			renameSync(tmp, ANNOUNCED);
		});
	} catch {
		/* the claim release below still prevents duplicate delivery */
	}
}

function writeAnnounced(ids: Set<string>): void {
	// atomic: a truncated announced.json would replay history on next start.
	// Merged under a lock so two sessions cannot erase each other's ids.
	try {
		withLock(() => {
			const disk = readAnnounced();
			const merged = new Set<string>(disk ? [...disk] : []);
			for (const id of ids) merged.add(id);
			ids.clear();
			for (const id of merged) ids.add(id);
			const tmp = `${ANNOUNCED}.tmp`;
			writeFileSync(tmp, JSON.stringify([...merged]));
			renameSync(tmp, ANNOUNCED);
		});
	} catch {
		/* losing the memo is survivable; never break the session over it */
	}
}

/** Every inbox file currently on disk, newest last. Unparseable ones are ignored. */
function scanInbox(): {
	ids: string[];
	messages: RemoteMessage[];
	mtimes: Map<string, number>;
} {
	const ids: string[] = [];
	const messages: RemoteMessage[] = [];
	const mtimes = new Map<string, number>();
	let names: string[] = [];
	try {
		names = readdirSync(INBOX).filter((n) => n.endsWith(".json"));
	} catch {
		return { ids, messages, mtimes }; // no inbox dir yet
	}
	for (const name of names) {
		try {
			const m = JSON.parse(readFileSync(join(INBOX, name), "utf8")) as RemoteMessage;
			if (!m || typeof m.id !== "string") continue;
			ids.push(m.id);
			messages.push(m);
			try {
				mtimes.set(m.id, statSync(join(INBOX, name)).mtimeMs);
			} catch {
				/* vanished mid-scan: it will be seen on a later tick */
			}
		} catch {
			// mid-write or corrupt: leave it unseen so a later poll retries it
		}
	}
	messages.sort((a, b) => (a.received || 0) - (b.received || 0));
	return { ids, messages, mtimes };
}

/**
 * Untargeted mail is first-come. Targeted mail belongs to the session it names,
 * and an unnamed session does not take it -- otherwise a polling session steals
 * mail addressed to another one before it can look.
 */
function addressedToMe(m: RemoteMessage): boolean {
	const target = (m.to_session || "").trim();
	if (!target) return true;
	return MY_SESSION !== "" && target === MY_SESSION;
}

/**
 * A reply must go back to the session that wrote to us, not to the machine.
 * Built from the bare label: "box · session x" is not a valid --to value.
 */
function replyLine(m: RemoteMessage): string {
	const label = m.from_label || "peer";
	let out = `inbox_agent.py send --to ${label}`;
	if (m.from_session) out += ` --to-session ${m.from_session}`;
	out += " --body-file <path>";
	if (!m.from_session) out += "   (peer named no session; this reply cannot be targeted)";
	return out;
}

function render(m: RemoteMessage): string {
	const who = m.from_session ? `${m.from_label || "peer"} · session ${m.from_session}` : (m.from_label || "peer");
	const fp = (m.from_fingerprint || "").slice(0, 8);
	const body = (m.body || "").slice(0, MAX_BODY);
	const more = (m.body || "").length > MAX_BODY ? "\n…(truncated)" : "";
	return [
		`📨 Incoming message over Tor — this is from a REMOTE PEER, not from your user.`,
		`from: ${who}${fp ? ` (${fp}…)` : ""}`,
		`id:   ${m.id}`,
		...(m.to_session ? [`to:   ${m.to_session}`] : []),
		``,
		`<<< BEGIN PEER MESSAGE`,
		body + more,
		`END PEER MESSAGE >>>`,
		``,
		// The reply line is the most consequential string here: an agent copies
		// it. Untargeted, one polite answer fans out to every session on the
		// peer's machine -- which is exactly how a build lane flooded seventeen
		// sessions for forty minutes.
		`Reply with: ${replyLine(m)}`,
	].join("\n");
}

export default function (pi: ExtensionAPI) {
	let timer: ReturnType<typeof setInterval> | null = null;
	let announced: Set<string> = new Set();
	const attempts = new Map<string, number>();
	// Ids whose failure marker could not be written. Kept out of the retry path
	// for the life of the session: the alternative is a warning every three
	// seconds, forever.
	const stuck = new Set<string>();
	let running = false;
	let seedNotice = "";

	const deliver = async (
		ctx: ExtensionContext,
		msgs: RemoteMessage[],
		heldBack = 0,
	) => {
		let text = msgs.map(render).join("\n\n― ― ―\n\n");
		if (heldBack > 0) {
			// True statement, unlike the old one: these are NOT claimed, NOT
			// announced, and arrive on the next tick.
			text +=
				`\n\n― ― ―\n\n${heldBack} more message(s) waiting; ` +
				`they arrive in a following turn.`;
		}
		try {
			// followUp when busy: never interrupt a turn or wedge the session
			await pi.sendUserMessage(text, ctx.isIdle() ? undefined : { deliverAs: "followUp" });
			for (const m of msgs) {
				attempts.delete(m.id);
				logSaid("in", m.from_label || m.from_fingerprint || "peer", m.body, m.id);
			}
			paintChat(ctx);
		} catch (err) {
			// Delivery failed. Retry, but not forever: a permanently failing id
			// would otherwise re-fire every tick and hammer the session.
			//
			// Giving up must UNMARK. Keeping the claim and the announced entry
			// while rendering nothing is the one outcome this repo exists to
			// avoid: every later session skips it, status says undelivered 0,
			// doctor says delivered, and the peer has a receipt for a message no
			// model ever saw.
			const why = err instanceof Error ? err.message : String(err);
			for (const m of msgs) {
				const n = (attempts.get(m.id) || 0) + 1;
				if (n >= 3) {
					attempts.delete(m.id);
					const recorded = markFailed(m.id, why, m.from_label || m.from_fingerprint || "peer");
					if (recorded) {
						// safe to release: the marker keeps it out of the retry path
						unclaim(m.id);
						forget(announced, m.id);
					} else {
						// keep it claimed so it is visibly not-delivered, and stop
						// retrying it this session rather than looping on a write
						// that keeps failing
						stuck.add(m.id);
					}
					try {
						ctx.ui.notify(
							`toragents: could not deliver ${m.id} after 3 tries (${why}). ` +
								`It is NOT delivered; run /toragents-status, then /toragents-retry` +
								(recorded ? "" : " (could not record the failure: state dir unwritable)"),
							"warning",
						);
					} catch {}
				} else {
					attempts.set(m.id, n);
					forget(announced, m.id);  // retry next tick (disk too: merge would undo it)
					unclaim(m.id); // and let another session take it
				}
			}
		}
	};

	/** Side panel on by default; /toragents-chat hides it. */
	let CHAT_WIDGET = true;

	/** Keep the side panel current; safe to call from anywhere. */
	let CONSUMER = true; // optimistic until the election says otherwise
	let lastCtx: ExtensionContext | null = null;
	const paintChat = (ctx?: ExtensionContext) => {
		if (ctx) lastCtx = ctx;
		const c = ctx || lastCtx;
		if (!c || !CHAT_WIDGET) return;
		try {
			if (!c.hasUI) return;
			if (c.mode === "tui") {
				// The component overload is the only one that receives the real
				// terminal width (and keeps receiving it after a resize). A
				// string[] is rendered verbatim, which is why this pane sat
				// frozen at renderChat()'s 56-column default on a 198-column
				// terminal: correct content, wrapped at half the screen.
				c.ui.setWidget(
					"toragents-chat",
					() => ({
						render: (width: number) => renderChat(width),
						invalidate: () => {},
					}),
					{ placement: "belowEditor" },
				);
			} else {
				// RPC/json/print: component factories are ignored there, so keep
				// the string form or the pane disappears on those surfaces.
				c.ui.setWidget("toragents-chat", renderChat(72), { placement: "belowEditor" });
			}
		} catch {
			/* no UI, or the widget is not supported on this pi */
		}
	};

	const poll = async (ctx: ExtensionContext) => {
		if (running) return; // a slow delivery must not overlap the next tick
		running = true;
		try {
			// Muted: say nothing and mark nothing -- a muted session must not
			// consume mail the peer meant for it, so it stays unread.
			if (isMuted()) return;
			// Stay silent if another live session owns delivery, or if ownership
			// moved while we were running.
			if (!CONSUMER || !isConsumer(STATE, process.pid)) return;
			heartbeatConsumer(STATE, process.pid, sessionLabel());
			const { messages } = scanInbox();
			// RECEIVE=targeted keeps a busy box quiet: only mail that names this
			// session is delivered. Default "all" is right for two people.
			const wanted =
				RECEIVE === "targeted"
					? messages.filter((m) => (m.to_session || "").trim() !== "" && addressedToMe(m))
					: messages;
			const failed = new Set([...failedIds(), ...stuck]);
			const fresh = wanted.filter(
				(m) =>
					!announced.has(m.id) &&
					!failed.has(m.id) &&
					!stuck.has(m.id) &&
					addressedToMe(m),
			);
			// Nothing new. Deliberately no pruning: dropping ids whose files are
			// gone would re-deliver them if an archive is ever restored.
			if (fresh.length === 0) return;
			// Claim and mark ONLY what this turn will actually render. Marking the
			// whole batch while showing MAX_BATCH leaves the remainder claimed and
			// announced but injected into no session, ever -- doctor calls them
			// delivered because claimed and announced agree, and the peer has a
			// receipt. "Peer sent 8 while pi was closed" is the normal case for a
			// two-person tool, so the leftover must stay visibly undone.
			//
			// Ownership is decided here, not by who writes last. The unclaimed
			// remainder is picked up by the next tick, so a flood still arrives in
			// bounded turns of MAX_BATCH.
			const batch = fresh.slice(0, MAX_BATCH);
			const mine = batch.filter((m) => claim(m.id));
			if (mine.length === 0) return; // another session on this box took them
			const heldBack = fresh.length - mine.length;
			for (const m of mine) announced.add(m.id);
			writeAnnounced(announced);
			await deliver(ctx, mine, heldBack);
			writeAnnounced(announced);
		} finally {
			running = false;
		}
	};

	pi.on("session_start", async (_event, ctx) => {
		try {
			mkdirSync(INBOX, { recursive: true });
		} catch {
			/* daemon will create it */
		}
		// Reconcile before adopting: a claim with no announced entry means a
		// process died between claiming and recording it. Nobody can deliver that
		// message -- every session sees EEXIST, adoption skips it, and no command
		// in the product can clear it. Take stale ones back.
		let healed = 0;
		const onDisk = readAnnounced(); // the authority: other sessions write it
		try {
			for (const id of readdirSync(CLAIM_DIR)) {
				if (onDisk?.has(id)) continue;
				try {
					const age = Date.now() - statSync(join(CLAIM_DIR, id)).mtimeMs;
					if (age > ORPHAN_MS) {
						unclaim(id);
						healed++;
					}
				} catch {
					/* vanished: nothing to heal */
				}
			}
		} catch {
			/* no claims yet */
		}
		if (healed > 0) seedNotice = `${healed} stale claim(s) released for delivery`;

		// Who we are, before anything writes a claim or a report.
		MY_SESSION = resolveSession(ctx);

		// Delivery is owned by exactly one session. Every pi window on this box
		// runs this extension; without an owner they race for the same inbox and
		// messages surface in whichever window claimed first.
		const election = electConsumer(STATE, { pid: process.pid, session: sessionLabel() });
		CONSUMER = election.tookOver;
		if (!CONSUMER) {
			try {
				ctx.ui.notify(
					`toragents: message delivery is owned by ${election.owner} ` +
						`(${election.reason}). This session will not consume mail. ` +
						`Use /toragents-takeover to take it.`,
					"warning",
				);
			} catch {}
		}
		paintChat(ctx);

		// Publish our resolved configuration before anything else: doctor's labels
		// must describe what this process does, not what some shell exported.
		try {
			mkdirSync(STATE, { recursive: true });
			writeFileSync(
				INFO_FILE,
				JSON.stringify({
					session: sessionLabel(),
					receive: RECEIVE,
					state: STATE,
					pid: process.pid,
					at: Date.now(),
				}),
			);
		} catch {
			/* doctor falls back to its own environment and says so */
		}

		const prior = readAnnounced();
		if (prior) {
			announced = prior;
		} else {
			// First run on this machine: adopt what is already on disk instead of
			// replaying it as a burst of user turns.
			const { ids, mtimes } = scanInbox();
			// Claim what we adopt so a session starting later does not replay it,
			// but skip anything another session already owns -- that mail is theirs
			// and adopting it would silently swallow an unread message.
			//
			// And do not adopt mail that JUST arrived. A first install starts the
			// extension seconds after the daemon, so the very first message can
			// already be on disk: adopting it would make the one message that
			// proves the install works silently not appear. Anything inside the
			// grace window stays unclaimed and the first poll delivers it.
			const cutoff = Date.now() - FIRST_RUN_GRACE_MS;
			const adopted = ids.filter((id) => (mtimes.get(id) || 0) < cutoff && claim(id));
			announced = new Set(adopted);
			writeAnnounced(announced);
			if (adopted.length > 0) {
				const held = ids.length - adopted.length;
				seedNotice +=
					(seedNotice ? "; " : "") +
					`${adopted.length} existing message(s) marked as already seen` +
					(held > 0 ? `; ${held} held by another session` : "");
			}
		}
		if (seedNotice) {
			try {
				ctx.ui.notify(
					`toragents: watching ${INBOX} (via ${RESOLVED.why}); ${seedNotice}`,
					"info",
				);
			} catch {
				/* notify is best-effort */
			}
		}
		timer = setInterval(() => {
			void poll(ctx).catch(() => {
				/* poll never throws past here */
			});
		}, POLL_MS);
	});

	/**
	 * Compose-time register. Returning systemPrompt here is the only path that
	 * reaches the model before it writes -- the send tool's echo was a receipt,
	 * never an input, and a peer caught us claiming otherwise.
	 */
	pi.on("before_agent_start", async (event: any) => {
		try {
			const py = process.platform === "win32"
				? join(homedir(), ".toragents", "venv", "Scripts", "python.exe")
				: join(homedir(), ".toragents", "venv", "bin", "python");
			const agent = join(homedir(), ".toragents", "inbox_agent.py");
			const peek = spawnSync(
				py,
				[agent, "lexicon", "pick", "--count", "2", "--json", "--peek"],
				{ encoding: "utf8", timeout: 15_000 },
			);
			let names: string[] = [], moves: string[] = [];
			try {
				const j = JSON.parse(peek.stdout || "{}");
				names = (j?.picked?.names || []).map((e: any) => e.text);
				moves = (j?.picked?.moves || []).map((e: any) => e.text);
			} catch {}
			const note = buildRegisterNote(STATE, names, moves);
			if (!note) return;
			return { systemPrompt: String(event?.systemPrompt || "") + "\n\n" + note };
		} catch {
			return;   // the register is decoration; never break a turn for it
		}
	});

	pi.on("session_shutdown", async () => {
		// Release ownership so the next window can pick delivery up immediately
		// instead of waiting out the stale window.
		try {
			const c = JSON.parse(readFileSync(join(STATE, CONSUMER_FILE), "utf8"));
			if (Number(c.pid) === process.pid) unlinkSync(join(STATE, CONSUMER_FILE));
		} catch {}
		if (timer) clearInterval(timer);
		timer = null;
		running = false;
	});

	/**
	 * Sending is the half a human cannot see. A bash call that prints JSON reads
	 * like plumbing; a tool call named "Send over Tor", with the peer in it, reads
	 * like a message leaving -- which is what it is.
	 */
	if (typeof (pi as any).registerTool === "function")
		pi.registerTool({
			name: "toragents_send",
			label: "Send over Tor",
			description:
				"Send a signed message to a paired peer over Tor. Returns the message id once " +
				"their daemon accepts it. Use this instead of running the daemon by hand: the " +
				"human sees the send, and the body is never touched by a shell.",
			parameters: {
				type: "object",
				properties: {
					to: { type: "string", description: "Peer label, as listed by `inbox peers`" },
					body: { type: "string", description: "Message text" },
					to_session: {
						type: "string",
						description: "Deliver only to this session name on their side",
					},
				},
				required: ["to", "body"],
			},
			async execute(_id: string, params: any) {
				const root = join(homedir(), ".toragents");
				const py =
					process.platform === "win32"
						? join(root, "venv", "Scripts", "python.exe")
						: join(root, "venv", "bin", "python");
				const agent = join(root, "inbox_agent.py");
				// Body through a file, never argv: no shell involved, but a body that
				// starts with "-" would still be read as a flag.
				const dir = mkdtempSync(join(tmpdir(), "toragents-send-"));
				const bodyFile = join(dir, "body.txt");
				writeFileSync(bodyFile, String(params.body ?? ""));
				const args = [agent, "send", "--to", String(params.to), "--body-file", bodyFile];
				if (params.to_session) args.push("--to-session", String(params.to_session));
				const res = spawnSync(py, args, {
					encoding: "utf8",
					env: { ...process.env, TORAGENTS_HOME: root },
					timeout: 180_000,
					});
				const out = `${res.stdout || ""}${res.stderr || ""}`.trim();
				if (res.status !== 0) {
					throw new Error(`send to ${params.to} failed (exit ${res.status}):\n${out.slice(-1500)}`);
				}
				let id = "";
				try {
					id = JSON.parse(res.stdout).id || "";
				} catch {
					/* some daemon errors print prose; the id is best effort */
				}
				logSaid("out", String(params.to), String(params.body ?? ""), id);
				paintChat();
				const tone = peerTone(STATE, String(params.to));
				const styles = peerStyles(STATE, String(params.to));
				const words = freshWords(py, agent, root, bodyFile, styles);
				return {
					content: [
						{
							type: "text" as const,
							text:
								(id
									? `sent to ${params.to} — id ${id}`
									: `send to ${params.to} reported success:\n${out.slice(-600)}`) +
								(tone ? `\nregister for ${params.to}: ${tone}` : "") +
								(words.names.length || words.moves.length
									? `\nfresh words (${words.sizes.names ?? "?"} names in pool): ` +
										[...words.names, ...words.moves].slice(0, 4).join(" · ")
									: ""),
						},
					],
					details: { to: params.to, id, ok: true, tone, styles, words }, 
				};
			},
	});

	pi.registerCommand("toragents-takeover", {
		description: "Make this session the one that delivers Tor messages",
		handler: async (_args: any, ctx: any) => {
			try {
				writeFileSync(join(STATE, CONSUMER_FILE), JSON.stringify({
					pid: process.pid, session: sessionLabel(), at: Date.now(),
				}));
				CONSUMER = true;
				await poll(ctx);
				ctx.ui.notify("toragents: this session now owns message delivery", "info");
			} catch (e: any) {
				try { ctx.ui.notify(`toragents: takeover failed: ${e?.message || e}`, "warning"); } catch {}
			}
		},
	});

	pi.registerCommand("toragents-chat", {
		description: "Show or hide the Tor conversation panel beside the editor",
		handler: async (_args: any, ctx: any) => {
			CHAT_WIDGET = !CHAT_WIDGET;
			try {
				if (!CHAT_WIDGET) ctx.ui.setWidget("toragents-chat", undefined);
				else paintChat(ctx);
				ctx.ui.notify(`toragents: conversation panel ${CHAT_WIDGET ? "shown" : "hidden"}`, "info");
			} catch {}
		},
	});

	// Manual check that does not wait for the next tick.

	pi.registerCommand("toragents-check", {
		description: "Check the Tor inbox now and deliver anything new",
		handler: async (_args, ctx) => {
			await poll(ctx);
			try {
				ctx.ui.notify(`toragents: checked ${INBOX}`, "info");
			} catch {
				/* ignore */
			}
		},
	});

	pi.registerCommand("toragents-mute", {
		description: "Stop delivering Tor messages into this session (they stay unread)",
		handler: async (_args, ctx) => {
			const ok = setMuted(true);
			try {
				ctx.ui.notify(
					ok
						? `toragents: muted as ${muteKey()}; messages stay unread and can be drained with the inbox command`
						: "toragents: could not write the mute marker",
					ok ? "info" : "warning",
				);
			} catch {}
		},
	});

	pi.registerCommand("toragents-unmute", {
		description: "Resume delivering Tor messages into this session",
		handler: async (_args, ctx) => {
			const ok = setMuted(false);
			try {
				ctx.ui.notify(ok ? "toragents: unmuted" : "toragents: could not remove the mute marker", "info");
			} catch {}
		},
	});

	pi.registerCommand("toragents-retry", {
		description: "Retry messages that failed to deliver",
		handler: async (_args, ctx) => {
			const ids = [...failedIds(), ...stuck];
			let cleared = 0;
			for (const id of ids) {
				stuck.delete(id);
				try {
					unlinkSync(join(FAILED_DIR, id));
					cleared++;
				} catch {
					/* no marker to remove: it was never written */
				}
			}
			// Drain now rather than up to POLL_MS later.
			await poll(ctx);
			try {
				ctx.ui.notify(
					cleared === 0
						? "toragents: nothing to retry"
						: `toragents: ${cleared} message(s) queued for delivery again`,
					"info",
				);
			} catch {}
		},
	});

	pi.registerCommand("toragents-said", {
		description: "Print the conversation transcript (what was said, both directions)",
		handler: async (_args: any, ctx: any) => {
			try {
				ctx.ui.notify(renderChat(72).join("\n"), "info");
			} catch {}
		},
	});

	pi.registerCommand("toragents-status", {
		description: "Show Tor inbox watching state",
		handler: async (_args, ctx) => {
			const { messages } = scanInbox();
			const failed = new Set([...failedIds(), ...stuck]);
			const unread = messages.filter((m) => !announced.has(m.id) && !failed.has(m.id)).length;
			const lines = [
				`watching    ${timer ? "yes" : "no"} every ${POLL_MS}ms`,
				`inbox       ${INBOX}`,
				`resolved by ${RESOLVED.why}`,
				`on disk     ${messages.length}`,
				`undelivered ${unread}`,
				`failed      ${failed.size}${failed.size ? " (" + [...failed].join(", ") + ")" : ""}${failed.size ? " -- /toragents-retry" : ""}`,
				`state       ${existsSync(ANNOUNCED) ? "memo present" : "memo missing (will seed)"}`,
			];
			try {
				ctx.ui.notify(lines.join("\n"), "info");
			} catch {
				/* ignore */
			}
		},
	});
}
