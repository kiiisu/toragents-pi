/**
 * toragents-transcript — what was said, in full.
 *
 * Split out of the inbox extension on 2026-10-19, for two reasons:
 *
 *   1. TWO WRITERS. The inbox extension is about DELIVERY, and while the panel
 *      work was happening another session was rewriting that same file; both
 *      sides lost edits. Reading the conversation is a separate concern with a
 *      separate owner, so it now lives here and no delivery change can clobber
 *      it.
 *   2. THREE STORES, ONE TRUTH. chat.jsonl carries order and who said it, but
 *      its bodies used to be truncated at 2 000 characters. inbox/*.json and
 *      sent/*.json hold the message as it crossed the wire. This module merges
 *      them so a reader always sees the whole text, and rehydrates old entries
 *      whose stored copy was cut.
 */

import {
	mkdirSync,
	readFileSync,
	readdirSync,
	renameSync,
	statSync,
} from "node:fs";
import { homedir } from "node:os";
import { join } from "node:path";
import { wrapTextWithAnsi } from "@earendil-works/pi-tui";


/**
 * Which directory is the daemon writing to?
 *
 * Two layouts exist and guessing wrong means reading an empty directory forever:
 * an install keeps <home>/state/inbox, while a daemon driven from its control
 * port (TORAGENTS_STATE) keeps identity.json, peers.json and inbox/ flat in one
 * directory. Explicit configuration wins outright; only the DEFAULTS get scored.
 */
export function resolveState(): { dir: string; why: string } {
	const home = process.env.TORAGENTS_HOME || join(homedir(), ".toragents");
	if (process.env.TORAGENTS_STATE) {
		return { dir: process.env.TORAGENTS_STATE, why: "TORAGENTS_STATE (explicit)" };
	}
	if (process.env.TORAGENTS_HOME) {
		return { dir: join(home, "state"), why: "TORAGENTS_HOME/state (explicit)" };
	}
	const candidates: [string, string][] = [
		[join(home, "state"), "~/.toragents/state"],
		["/var/lib/toragents-inbox", "flat state dir"],
	];
	const score = (dir: string): number => {
		let n = 0;
		try {
			if (readdirSync(join(dir, "inbox")).length > 0) n += 2;
		} catch {
			return 0;
		}
		if (statSync && dir) n += 0;
		return n;
	};
	let best = { dir: candidates[0][0], why: "default" };
	let bestScore = 0;
	for (const [dir, why] of candidates) {
		const sc = score(dir);
		if (sc > bestScore) {
			bestScore = sc;
			best = { dir, why: `${why} (score ${sc})` };
		}
	}
	return best;
}

const RESOLVED_STATE = resolveState();
export const STATE = RESOLVED_STATE.dir;
export const INBOX = join(STATE, "inbox");

const CHAT_FILE = join(STATE, "chat.jsonl");
const SENT_DIR = join(STATE, "sent");
const CHAT_SHOWN = 12;
/**
 * How much of a body the transcript keeps. 2 000 characters was the old cap and
 * it cut real messages in half -- the wire bodies in this install run 1.5k-3.2k,
 * so the stored copy was losing exactly the technical half someone wanted to
 * read. Disk cost is nothing (38 rows = 42 KB), so store the message.
 */
const CHAT_BODY_MAX = 20_000;

function logSaid(dir: "in" | "out", peer: string, body: string, id: string) {
	try {
		mkdirSync(STATE, { recursive: true });
		appendFileSync(
			CHAT_FILE,
			JSON.stringify({ at: Date.now(), dir, peer, id, body: String(body).slice(0, CHAT_BODY_MAX) }) + "\n",
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

export function renderChat(width = 72): string[] {
	const rows = readThread().slice(-CHAT_SHOWN);
	if (rows.length === 0) return [" toragents · no messages yet"];
	const out: string[] = [];
	for (const r of rows) {
		const when = new Date(r.at).toTimeString().slice(0, 5);
		out.push(` ${r.dir === "out" ? "→" : "←"} ${r.peer}  ${when}`);
		const t = tldr(r.body);
		if (t) for (const l of wrapTextWithAnsi(` ▸ ${t}`, Math.max(24, width))) out.push(l);
		for (const l of wrapBlock(r.body, Math.max(20, width - 3))) out.push(`   ${l}`);
	}
	return out;
}


// ───────────────────────── what was said, in full ─────────────────────────

/**
 * The transcript, whole.
 *
 * Three stores hold pieces of the same conversation: chat.jsonl (order, and who
 * said it), inbox/*.json (the received body as it came off the wire), and
 * sent/*.json (the sent body, plus its receipt). Reading only the first one --
 * which is what the panel used to do -- means showing a 2 000-character
 * truncation of a message whose full text was sitting on disk the whole time.
 * So: spine from the transcript, body from whichever store has it whole, and
 * anything on disk the transcript never saw is still shown. A panel that hides
 * mail because a different window consumed it is lying twice.
 */
export type Said = { at: number; dir: "in" | "out"; peer: string; id: string; body: string };

function prefixMatch(full: string, cut: string): boolean {
	const head = String(cut || "").slice(0, 160);
	return head.length === 0 || String(full || "").startsWith(head);
}

function sentStore(): any[] {
	const out: any[] = [];
	try {
		for (const n of readdirSync(SENT_DIR)) {
			if (!n.endsWith(".json")) continue;
			try {
				const d = JSON.parse(readFileSync(join(SENT_DIR, n), "utf8"));
				if (d && typeof d.body === "string") out.push(d);
			} catch {
				/* half-written receipt: it will be complete next paint */
			}
		}
	} catch {
		/* no sent store on this install */
	}
	return out;
}

export function readThread(): Said[] {
	const inbox = new Map<string, { body: string; peer: string; at: number }>();
	try {
		for (const n of readdirSync(INBOX)) {
			if (!n.endsWith(".json")) continue;
			const full = join(INBOX, n);
			try {
				const m = JSON.parse(readFileSync(full, "utf8"));
				if (!m || typeof m.id !== "string" || typeof m.body !== "string") continue;
				const secs = Number(m.received || 0);
				inbox.set(m.id, {
					body: m.body,
					peer: m.from_label || "peer",
					at: secs > 0 ? Math.round(secs * 1000) : statSync(full).mtimeMs,
				});
			} catch {
				/* mid-write */
			}
		}
	} catch {
		/* no inbox yet */
	}
	const sent = sentStore();

	const rows: Said[] = [];
	const seenIn = new Set<string>();
	for (const r of saidLog()) {
		const at = Number(r.at) || 0;
		const id = String((r as any).id || "");
		const dir: "in" | "out" = r.dir === "out" ? "out" : "in";
		let body = String(r.body || "");
		if (dir === "in") {
			seenIn.add(id);
			const whole = inbox.get(id);
			if (whole && whole.body.length > body.length) body = whole.body;
		} else {
			const whole = sent.find(
				(s) =>
					typeof s.body === "string" &&
					s.body.length > body.length &&
					prefixMatch(s.body, body) &&
					(at === 0 || Math.abs(Number(s.at || 0) * 1000 - at) < 30 * 60 * 1000),
			);
			if (whole) body = whole.body;
		}
		rows.push({ at, dir, peer: String(r.peer || "peer"), id, body });
	}
	for (const [id, m] of inbox) {
		if (seenIn.has(id)) continue;
		rows.push({ at: m.at, dir: "in", peer: m.peer, id, body: m.body });
	}
	rows.sort((a, b) => a.at - b.at);
	return rows;
}

/**
 * A one-line summary of the technical content. Deliberately dumb and local: no
 * model call, no network, same input -> same output, so it can be asserted.
 *
 * It is not a summary of the whole message -- the prose is right there under it.
 * It exists so the SALIENT tokens (commits, files, flags, figures) survive even
 * when the body is collapsed, because those are what a reader scans for.
 */
export function tldr(body: string, max = 150): string {
	const text = String(body || "")
		.replace(/\r/g, "\n")
		.replace(/\s+/g, " ")
		.trim();
	if (!text) return "";
	const keys: string[] = [];
	const add = (s: string) => {
		const t = String(s).replace(/\s+/g, " ").trim();
		if (!t || t.length > 42) return;
		if (keys.some((k) => k.toLowerCase() === t.toLowerCase())) return;
		if (keys.length < 5) keys.push(t);
	};
	// Shouting is not signal: an ALLCAPS word that is just an English filler word
	// says nothing about the message, and "· AND · ALL" in a summary line is
	// exactly how a useful one-line digest turns into noise.
	const FILLER =
		/^(?:A|AN|AND|OR|NOT|BUT|THE|FOR|YES|NO|ONE|TWO|NEW|OLD|ALL|ANY|NOW|OK|OKAY|SO|IF|IT|IS|AS|AT|BY|DO|GO|TO|UP|WE|I)$/;
	const seenSha = new Set<string>();
	const shas: string[] = [];
	for (const s of text.match(/\b[0-9a-f]{7,40}\b/g) || []) {
		if (!/\d/.test(s) || seenSha.has(s)) continue;
		seenSha.add(s);
		shas.push(s);
		if (shas.length === 2) break;
	}
	if (shas.length) add(shas.length === 1 ? `commit ${shas[0]}` : `commits ${shas.join(" ")}`);
	for (const m of text.matchAll(/`([^`]{2,42})`/g)) add(m[1]);
	for (const m of text.matchAll(/\b[\w./-]{2,60}\.(?:ts|tsx|js|mjs|cjs|py|json|jsonl|md|sh|ya?ml|toml)\b/g)) add(m[0]);
	for (const m of text.matchAll(/§\s?\d+(?:\.\d+)?/g)) add(m[0]);
	for (const m of text.matchAll(/\b[A-Z][A-Z0-9_]{2,}\b/g)) if (!FILLER.test(m[0])) add(m[0]);
	for (const m of text.matchAll(/\b\d+(?:[.,]\d+)?\s(?:%|ms|s|kb|mb|gb|bits?|bytes?)\b/gi)) add(m[0]);
	let t = firstSentence(text);
	for (const k of keys) {
		const next = t ? `${t} · ${k}` : k;
		if (next.length > max) break;
		t = next;
	}
	return t.length > max ? `${t.slice(0, max - 1).trimEnd()}…` : t;
}

function firstSentence(text: string, max = 96): string {
	const first = (text.split(/(?<=[.!?])\s+/)[0] || text).trim();
	if (first.length <= max) return first;
	let cut = first.slice(0, max);
	const space = cut.lastIndexOf(" ");
	if (space > max * 0.5) cut = cut.slice(0, space);
	return `${cut.trimEnd()}…`;
}

/** Paragraph structure is kept: collapsing whitespace destroys the rhythm the
 *  writer actually wrote, and that rhythm IS the personality in these messages. */
export function wrapBlock(body: string, width: number): string[] {
	const out: string[] = [];
	let blank = 0;
	for (const line of String(body || "").replace(/\r\n?/g, "\n").split("\n")) {
		if (!line.trim()) {
			blank++;
			if (blank === 1) out.push("");
			continue;
		}
		blank = 0;
		for (const l of wrapTextWithAnsi(line, Math.max(10, width))) out.push(l);
	}
	while (out.length && out[out.length - 1] === "") out.pop();
	return out;
}

function ago(ms: number): string {
	const s = Math.max(0, Math.round((Date.now() - ms) / 1000));
	if (s < 60) return `${s}s`;
	if (s < 3600) return `${Math.round(s / 60)}m`;
	if (s < 86_400) return `${Math.round(s / 3600)}h`;
	return `${Math.round(s / 86_400)}d`;
}

function chars(n: number): string {
	return n >= 1000 ? `${(n / 1000).toFixed(1)}k` : String(n);
}


/** Cheap change detector: repaint only when the conversation actually moved. */
export function threadSignature(): string {
	const bits: string[] = [];
	try {
		const s = statSync(CHAT_FILE);
		bits.push(`${s.size}:${Math.round(s.mtimeMs)}`);
	} catch {
		bits.push("-");
	}
	try {
		bits.push(String(readdirSync(INBOX).length));
	} catch {
		bits.push("-");
	}
	try {
		bits.push(String(readdirSync(SENT_DIR).length));
	} catch {
		bits.push("-");
	}
	return bits.join("|");
}

/**
 * The panel under the editor.
 *
 * Rendered as a component, not a string array: pi caps string-array widgets at
 * 10 lines (that cap is where "... (widget truncated)" came from), while a
 * component renders what it needs. So the newest message is shown WHOLE, older
 * ones get a TLDR plus as many lines as the budget allows, and the header says
 * how many are folded away rather than pretending they do not exist.
 */

// ── snapshot, for anything outside pi that wants to read this ────────────────

/**
 * The transcript as one small JSON file.
 *
 * Nothing inside pi needs it (the column renders from memory). It exists so a
 * shell script, a diff, or a report can read the conversation without importing
 * TypeScript — and so "what did the panel actually see" is answerable.
 */
export const SNAPSHOT = join(STATE, "thread.json");

export function writeSnapshot(rows: Said[]): boolean {
	try {
		mkdirSync(STATE, { recursive: true });
		const payload = {
			at: Date.now(),
			messages: rows.map((m) => ({
				at: m.at,
				dir: m.dir,
				peer: m.peer,
				id: m.id,
				body: m.body,
				tldr: tldr(m.body),
			})),
		};
		const tmp = `${SNAPSHOT}.tmp`;
		writeFileSync(tmp, JSON.stringify(payload));
		renameSync(tmp, SNAPSHOT);
		return true;
	} catch {
		return false;
	}
}

/**
 * What the mailbox knows that the conversation does not.
 *
 * The transcript answers "what was said". These are the four facts it cannot
 * express, and every one of them comes from a file that already exists:
 *
 *   queued     letters in outbox/ that have not gone out (with why and for how
 *              long) -- the state that made tonight's "is she offline?" a
 *              five-command investigation instead of one line on screen.
 *   strangers  received mail whose `from_known` is false. A security fact: it
 *              answers "who has my onion", which no amount of reading the
 *              transcript will tell you.
 *   unsent     sent/ records where `state` is not an acknowledgement. "unknown"
 *              means the transport itself cannot say whether it landed, and
 *              hiding that behind a tick would be the lie this whole project is
 *              built to avoid.
 *   dark       age of the oldest queued letter: how long the peer has been out
 *              of reach, for as long as the queue has been trying.
 *
 * Cached on directory mtime, because this runs on a poll tick and the inbox
 * holds hundreds of files: unchanged directory, unchanged answer. Any read
 * failure degrades that fact to zero rather than throwing -- a mailbox the pane
 * cannot read must not take the pane with it.
 */
export interface MailFacts {
	queued: number;
	queuedChars: number;
	oldestQueued: number | null;
	worstPeer: string | null;
	worstAttempts: number;
	lastError: string | null;
	strangers: number;
	unsent: number;
}

const FACTS_EMPTY: MailFacts = {
	queued: 0,
	queuedChars: 0,
	oldestQueued: null,
	worstPeer: null,
	worstAttempts: 0,
	lastError: null,
	strangers: 0,
	unsent: 0,
};

let factsCache: { key: string; facts: MailFacts } | null = null;

function readJsonDir(dir: string, key: string): { key: string; rows: any[] } {
	let names: string[] = [];
	let stamp = 0;
	try {
		names = readdirSync(dir).filter((n) => n.endsWith(".json"));
		stamp = statSync(dir).mtimeMs;
	} catch {
		return { key: `${key}:absent`, rows: [] };
	}
	const rows: any[] = [];
	for (const name of names) {
		try {
			rows.push(JSON.parse(readFileSync(join(dir, name), "utf8")));
		} catch {
			/* one broken file must not hide the rest */
		}
	}
	return { key: `${key}:${names.length}:${stamp}`, rows };
}

export function readMailFacts(stateDir = STATE): MailFacts {
	const out = readJsonDir(join(stateDir, "outbox"), "outbox");
	const inbox = readJsonDir(join(stateDir, "inbox"), "inbox");
	const sent = readJsonDir(join(stateDir, "sent"), "sent");
	const key = `${out.key}|${inbox.key}|${sent.key}`;
	if (factsCache && factsCache.key === key) return factsCache.facts;

	const facts: MailFacts = { ...FACTS_EMPTY };
	const now = Date.now();
	for (const item of out.rows) {
		if (item?.sent_id) continue; // already delivered, waiting for the file to go
		facts.queued++;
		const body = typeof item.body === "string" ? item.body.length : 0;
		facts.queuedChars += body;
		const created = Number(item.created) * 1000;
		if (Number.isFinite(created) && created > 0) {
			if (facts.oldestQueued === null || created < facts.oldestQueued) facts.oldestQueued = created;
		}
		const attempts = Number(item.attempts) || 0;
		if (attempts > facts.worstAttempts) {
			facts.worstAttempts = attempts;
			facts.worstPeer = typeof item.to === "string" ? item.to : null;
			facts.lastError = typeof item.last_error === "string" ? item.last_error.split("\n")[0].slice(0, 60) : null;
		}
	}
	for (const msg of inbox.rows) if (msg?.from_known === false) facts.strangers++;
	for (const rec of sent.rows) {
		const state = String(rec?.state || "");
		if (state && state !== "acked" && state !== "delivered") facts.unsent++;
	}
	if (facts.oldestQueued !== null) facts.oldestQueued = now - facts.oldestQueued;
	factsCache = { key, facts };
	return facts;
}
