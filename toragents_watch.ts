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
	existsSync,
	mkdirSync,
	readFileSync,
	readdirSync,
	renameSync,
	statSync,
	unlinkSync,
	writeFileSync,
} from "node:fs";
import { homedir } from "node:os";
import { join } from "node:path";
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
// A claim with no announced entry is an orphan: the process died between the
// two writes (or was killed at shutdown). Older than this, it is safe to take.
const ORPHAN_MS = 5 * 60 * 1000;

/** Who this session is, for the claim record and for doctor. */
function sessionLabel(): string {
	const id = (process.env.PI_SESSION_ID || "").replace(/-/g, "");
	return id ? `pi-${id.slice(0, 8)}` : "pi-unknown";
}

function failedIds(): Set<string> {
	try {
		return new Set(readdirSync(FAILED_DIR).filter((n) => n && !n.startsWith(".")));
	} catch {
		return new Set();
	}
}

function markFailed(id: string, error: string, from: string): void {
	try {
		mkdirSync(FAILED_DIR, { recursive: true });
		writeFileSync(
			join(FAILED_DIR, id),
			JSON.stringify({ at: Date.now(), error: error.slice(0, 300), from, session: sessionLabel() }),
		);
	} catch {
		/* if even this fails, the unclaim below still returns it to PENDING */
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
/** This session's name, for addressing only (TORAGENTS_SESSION). */
function autoSession(): string {
	const explicit = (process.env.TORAGENTS_SESSION || "").trim();
	if (explicit) return explicit.slice(0, 64);
	const pairs: Array<[string, string]> = [
		["CLAUDE_CODE_SESSION_ID", "claude"],
		["PI_SESSION_ID", "pi"],
		["CODEX_SESSION_ID", "codex"],
		["CODEX_THREAD_ID", "codex"],
	];
	for (const [key, prefix] of pairs) {
		const id = (process.env[key] || "").trim();
		if (id) return `${prefix}-${id.replace(/-/g, "").slice(0, 8)}`;
	}
	return "";
}

/**
 * Which session we are, derived from what the harness already tells its
 * children. Nobody types a name; two sessions on one machine still differ.
 */
const MY_SESSION = autoSession();
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
	let running = false;
	let seedNotice = "";

	const deliver = async (ctx: ExtensionContext, msgs: RemoteMessage[]) => {
		const shown = msgs.slice(0, MAX_BATCH);
		let text = shown.map(render).join("\n\n― ― ―\n\n");
		if (msgs.length > shown.length) {
			const rest = msgs.slice(shown.length);
			text +=
				`\n\n― ― ―\n\n${rest.length} more message(s) waiting, newest from ` +
				`${rest[rest.length - 1].from_label || "peer"}. ` +
				`Run: inbox_agent.py inbox`;
		}
		try {
			// followUp when busy: never interrupt a turn or wedge the session
			await pi.sendUserMessage(text, ctx.isIdle() ? undefined : { deliverAs: "followUp" });
			for (const m of msgs) attempts.delete(m.id);
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
					unclaim(m.id);
					forget(announced, m.id);
					markFailed(m.id, why, m.from_label || m.from_fingerprint || "peer");
					try {
						ctx.ui.notify(
							`toragents: could not deliver ${m.id} after 3 tries (${why}). ` +
								`It is NOT delivered; run /toragents-status, then /toragents-retry`,
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

	const poll = async (ctx: ExtensionContext) => {
		if (running) return; // a slow delivery must not overlap the next tick
		running = true;
		try {
			// Muted: say nothing and mark nothing -- a muted session must not
			// consume mail the peer meant for it, so it stays unread.
			if (isMuted()) return;
			const { messages } = scanInbox();
			// RECEIVE=targeted keeps a busy box quiet: only mail that names this
			// session is delivered. Default "all" is right for two people.
			const wanted =
				RECEIVE === "targeted"
					? messages.filter((m) => (m.to_session || "").trim() !== "" && addressedToMe(m))
					: messages;
			const failed = failedIds();
			const fresh = wanted.filter(
				(m) => !announced.has(m.id) && !failed.has(m.id) && addressedToMe(m),
			);
			// Nothing new. Deliberately no pruning: dropping ids whose files are
			// gone would re-deliver them if an archive is ever restored.
			if (fresh.length === 0) return;
			// Ownership is decided here, not by who writes last.
			const mine = fresh.filter((m) => claim(m.id));
			if (mine.length === 0) return; // another session on this box took them
			for (const m of mine) announced.add(m.id);
			writeAnnounced(announced);
			await deliver(ctx, mine);
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

	pi.on("session_shutdown", async () => {
		if (timer) clearInterval(timer);
		timer = null;
		running = false;
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
			const ids = [...failedIds()];
			let cleared = 0;
			for (const id of ids) {
				try {
					unlinkSync(join(FAILED_DIR, id));
					cleared++;
				} catch {
					/* already gone */
				}
			}
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

	pi.registerCommand("toragents-status", {
		description: "Show Tor inbox watching state",
		handler: async (_args, ctx) => {
			const { messages } = scanInbox();
			const failed = failedIds();
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
