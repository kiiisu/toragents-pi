/**
 * toragents-panel — the Tor conversation as a right-hand column, read like mail.
 *
 * WHY A COLUMN AND NOT A WIDGET
 * pi's only in-editor surfaces are above/below the editor, so anything placed
 * there costs chat height: a 40-line panel under the editor is half of a 46-row
 * terminal, which is exactly what Maestro rejected. pi has no right pane in its
 * public API, and an overlay would float over chat text rather than sit beside
 * it. `_lib/toragents-side-column.ts` holds the reserved-column machinery
 * (borrowed from pi-sidebar-tui); this file owns the content and the keys.
 *
 * WHERE IT GOES
 * Placement is policy, not preference: `_lib/toragents-policy.ts` reads
 * ~/.toragents/policy.json, and the session it names as receiver gets this wide
 * column while every other chat gets a four-line strip at the bottom of the
 * editor. A 60-column column costs every window on the box; the receiver earns
 * it by being the one window where the conversation is the work. Any session can
 * still open the column with /toragents-panel, which is the escape hatch.
 *
 * THE SHAPE: A LIST OF MESSAGES, NOT A TRANSCRIPT
 * These peers write 1.5-3k character technical letters. Rendered as one wall of
 * wrapped text, a 46-column column shows about one message per screen and you
 * cannot see what arrived without reading it. So each message is a collapsed
 * card with the three things you need to decide "do I read this now":
 *
 *   ▸ ← kisu  14:02  2.5k  ●
 *     digest was dead — pull 1d8c847                <- subject
 *     stop and pull before you reinstall. the dig…  <- preview (2 lines)
 *     … 34 lines · enter opens
 *
 * Subject is derived, not stored: `subject()` scores a message's sentences and
 * picks the one carrying the most technical signal, because a peer's first
 * sentence is often a pleasantry ("conceded, and the concession is the
 * interesting part") while the commit id is in the third. Deterministic, local,
 * no model call — a subject that cost an API round trip per message would also
 * be one that can disagree with itself between renders.
 *
 * FOCUS
 * The column never steals a key the editor needs. `alt+f` gives it focus (border
 * turns accent-coloured); while focused, j/k/enter/space work as arrows and
 * expand. Esc or `alt+f` hands focus back. Everything also has an alt+ form, so
 * nothing here requires focus.
 */

import type { ExtensionAPI, ExtensionContext } from "@earendil-works/pi-coding-agent";
import { Key, matchesKey, truncateToWidth, wrapTextWithAnsi } from "@earendil-works/pi-tui";
import { panelMode, readPolicy } from "./_lib/toragents-policy.ts";
import { SideColumn } from "./_lib/toragents-side-column.ts";
import {
	type Said,
	readThread,
	threadSignature,
	wrapBlock,
	writeSnapshot,
} from "./_lib/toragents-transcript.ts";

const WIDTH_DEFAULT = 46;
const MIN_TERMINAL = 110;
const POLL_MS = 1500;
const SUBJECT_MAX = 60;
const PREVIEW_LINES = 2;

/**
 * How wide the column is.
 *
 * Proportional rather than fixed, because the subject line is the thing being
 * read: a 46-column column clips most subjects mid-clause ("the safety property
 * is right, but it's…"), and a fixed 60 would eat half of a 110-column terminal.
 * TORAGENTS_PANEL_WIDTH overrides everything.
 */
function columnWidth(terminalWidth = 0): number {
	const raw = Number(process.env.TORAGENTS_PANEL_WIDTH || 0);
	if (Number.isFinite(raw) && raw > 0) return Math.max(28, Math.min(100, Math.round(raw)));
	if (!terminalWidth) return WIDTH_DEFAULT;
	return Math.max(38, Math.min(62, Math.round(terminalWidth * 0.36)));
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

function keyOf(m: Said): string {
	return `${m.dir}:${m.id || m.at}`;
}

// ── subject ──────────────────────────────────────────────────────────────────

/**
 * Technical signal in a sentence: an identifier, a sha, a path, a §ref, a
 * figure. Used to score sentences, never shown.
 */
function signalScore(sentence: string): number {
	let n = 0;
	n += (sentence.match(/`[^`]{2,40}`/g) || []).length * 3;
	n += (sentence.match(/\b[\w./-]{2,60}\.(?:ts|tsx|js|mjs|cjs|py|json|jsonl|md|sh|ya?ml|toml)\b/g) || []).length * 3;
	n += (sentence.match(/\b[0-9a-f]{7,40}\b/g) || []).length * 3;
	n += (sentence.match(/§\s?\d+(?:\.\d+)?/g) || []).length * 2;
	n += (sentence.match(/\b[A-Z][A-Z0-9_]{2,}\b/g) || []).length;
	n += (sentence.match(/\b\d+(?:[.,]\d+)?\s?(?:%|ms|s|x|bits?|bytes?)\b/gi) || []).length;
	return n;
}

function tidy(sentence: string): string {
	return sentence
		.replace(/\s+/g, " ")
		.replace(/^[\s"'(\[—–-]+/, "")
		// A subject that starts with a conjunction reads like a fragment of the
		// previous message, which is exactly what it is: "so build it once ..."
		// only makes sense if you read the message before it.
		.replace(/^(?:so|and|but|also|then|hence|thus|ok|okay|well)\b[,:]?\s*/i, "")
		.replace(/:\s*$/, "")
		.trim();
}

/**
 * One line that says what a message is about.
 *
 * Sentence scoring, not summarisation: the sentence with the most identifiers
 * wins, with a small bonus for early position and a large penalty for being too
 * long or too short to be a subject. Falls back to the first sentence, because a
 * greeting still beats an empty subject line.
 */
export function subject(body: string, max = SUBJECT_MAX): string {
	return claimBody(body, max).subject;
}

/**
 * The subject plus the body with the subject's sentence cut out.
 *
 * Without the cut, a collapsed card reads "the reframing is right, and it's the…"
 * and then the preview begins with the very same sentence — two lines of the same
 * text, which is the opposite of a preview. Removing the claimed sentence makes
 * the preview show what you have NOT already read.
 */
export function claimBody(body: string, max = SUBJECT_MAX): { subject: string; rest: string } {
	const text = String(body || "").replace(/\r/g, "\n").trim();
	if (!text) return { subject: "(empty)", rest: "" };
	const rawSentences = text
		.replace(/\n+/g, " ")
		.split(/(?<=[.!?])\s+|\s+—\s+|:\s+(?=[a-z])/)
		.filter((s) => s.trim().length > 0);
	const sentences = rawSentences.map((s) => tidy(s)).filter((s) => s.length > 0);
	if (sentences.length === 0) return { subject: "(empty)", rest: text };

	let best = "";
	let bestRaw = "";
	let bestScore = -Infinity;
	sentences.forEach((s, i) => {
		let score = signalScore(s) * 4;
		if (i === 0) score += 3;
		if (s.length >= 18 && s.length <= 90) score += 2;
		if (s.length > 120) score -= 4;
		if (/^(hi|hey|hello|thanks|thank you|ok|okay|noted|agreed|yes|no)\b/i.test(s)) score -= 5;
		// A sentence that ends in a colon is a lead-in to the next one, not a topic.
		if (/:$/.test(s)) score -= 2;
		if (score > bestScore) {
			bestScore = score;
			best = s;
			bestRaw = sentences[i] === undefined ? "" : rawSentences[i];
		}
	});

	const clean = best.replace(/[.;,:!?\s]+$/, "");
	const short =
		clean.length <= max
			? clean
			: (() => {
					const cut = clean.slice(0, max - 1);
					const space = cut.lastIndexOf(" ");
					return `${(space > max * 0.5 ? cut.slice(0, space) : cut).trimEnd()}…`;
				})();

	// Cut the claimed sentence out of the body, so the preview is new text.
	let rest = text;
	if (bestRaw) {
		const at = text.indexOf(bestRaw.trim());
		if (at >= 0) {
			rest = (text.slice(0, at) + "\n" + text.slice(at + bestRaw.trim().length)).trim();
			rest = rest.replace(/\n{3,}/g, "\n\n");
		}
	}
	return { subject: short, rest };
}

// ── the column ───────────────────────────────────────────────────────────────

export interface ColumnView {
	/** Index of the message the marker sits on. */
	selected: number;
	/** Message keys whose body is shown in full. */
	expanded: Set<string>;
	/** Keys already read; everything else carries the unread dot. */
	seen: Set<string>;
	/** True while the column owns j/k/enter. */
	focused: boolean;
	/** First visible content line. */
	top: number;
}

interface Block {
	key: string;
	lines: string[];
	start: number; // first content line index (after the header rule)
}

/**
 * Render the list.
 *
 * Returns the painted rows plus the layout the key handler needs (`starts` and
 * `ends` per message) so selection and scrolling agree with what is on screen
 * instead of re-deriving it separately and drifting.
 */
export function renderColumn(
	msgs: Said[],
	width: number,
	rows: number,
	view: ColumnView,
	theme: any,
): { lines: string[]; starts: number[]; ends: number[] } {
	const dim = (s: string) => (theme ? theme.fg("dim", s) : s);
	const accent = (s: string) => (theme ? theme.fg("accent", s) : s);
	const muted = (s: string) => (theme ? theme.fg("muted", s) : s);
	const bold = (s: string) => (theme && theme.bold ? theme.bold(s) : s);
	const inner = Math.max(12, width - 2);

	const blocks: Block[] = [];
	if (msgs.length === 0) {
		blocks.push({ key: "empty", lines: [dim(" no messages yet")], start: 0 });
	}

	msgs.forEach((m, i) => {
		const isSel = i === view.selected;
		const key = keyOf(m);
		const open = view.expanded.has(key);
		const unread = !view.seen.has(key) && m.dir === "in";
		const arrow = m.dir === "out" ? "→" : "←";
		const when = new Date(m.at).toTimeString().slice(0, 5);

		const meta = [
			`${isSel ? accent("›") : " "} ${m.dir === "out" ? muted(arrow) : accent(arrow)} ${m.peer}`,
			dim(when),
			dim(chars(m.body.length)),
			unread ? accent("●") : "",
		]
			.filter(Boolean)
			.join(" ");

		const claimed = claimBody(m.body, inner - 3);
		const lines: string[] = [truncateToWidth(meta, width, "")];
		lines.push(truncateToWidth(`   ${bold(claimed.subject)}`, width, ""));

		// Collapsed, blank lines are wasted rows: the preview is for scanning, so
		// paragraph breaks collapse out. Expanded keeps them — that is where the
		// writing's shape matters.
		const previewText = claimed.rest
			.split(/\n+/)
			.map((l) => l.trim())
			.filter(Boolean)
			.join("\n");
		const bodyLines = wrapBlock(open ? m.body : previewText, inner - 2);
		if (open) {
			for (const l of bodyLines) lines.push(truncateToWidth(`   ${l}`, width, ""));
			lines.push(dim(`   ⌃ collapse`));
		} else {
			for (const l of bodyLines.slice(0, PREVIEW_LINES)) lines.push(truncateToWidth(`   ${dim(l)}`, width, ""));
			const rest = bodyLines.length - Math.min(bodyLines.length, PREVIEW_LINES);
			if (rest > 0) {
				lines.push(
					truncateToWidth(dim(`   … ${rest} more line${rest === 1 ? "" : "s"} · enter opens`), width, ""),
				);
			}
		}
		lines.push(dim(""));
		blocks.push({ key, lines, start: 0 });
	});

	// One header shows position and, when focused, that focus is on this pane.
	const unreadCount = msgs.filter((m) => !view.seen.has(keyOf(m)) && m.dir === "in").length;
	const head =
		` toragents · ${msgs.length}` +
		(unreadCount > 0 ? ` · ${unreadCount} unread` : "") +
		(view.focused ? accent(" · FOCUS") : "");
	const foot = view.focused
		? " j/k · enter open · g/G ends · esc out"
		: " alt+f focus · alt+j/k · alt+e open";

	const contentRows = Math.max(0, rows - 2);
	const flat: string[] = [];
	const starts: number[] = [];
	const ends: number[] = [];
	for (const b of blocks) {
		starts.push(flat.length);
		flat.push(...b.lines);
		ends.push(flat.length - 1);
	}

	const top = Math.max(0, Math.min(view.top, Math.max(0, flat.length - contentRows)));
	const out = new Array<string>(Math.max(1, rows)).fill("");
	out[0] = truncateToWidth(head, width, "");
	if (rows >= 2) out[rows - 1] = dim(truncateToWidth(foot, width, ""));
	for (let i = 0; i < contentRows; i++) out[1 + i] = flat[top + i] ?? "";
	return {
		lines: out,
		starts: starts.map((s) => s - top),
		ends: ends.map((e) => e - top),
		absolute: starts,
	};
}

// ── the strip ────────────────────────────────────────────────────────────────

const STRIP_LINES_DEFAULT = 4;

function stripLines(): number {
	const raw = Number(process.env.TORAGENTS_STRIP_LINES || STRIP_LINES_DEFAULT);
	return Math.max(2, Math.min(8, Math.round(Number.isFinite(raw) ? raw : STRIP_LINES_DEFAULT)));
}

/**
 * What a session that is NOT the receiver gets: the newest message, in a few
 * lines at the bottom, and a way into the wide column when it actually wants to
 * read the thread.
 *
 * The height is the point. A full column costs 60 columns of every window on the
 * box; on a chat you glance at, that is space paid for a conversation you are not
 * having. Lines are capped and the newest message is the only one shown, because
 * an inbox strip is a notification, not a reader.
 */
export function renderStrip(
	msgs: Said[],
	width: number,
	seen: Set<string>,
	total: number,
	theme: any,
): string[] {
	const rows = stripLines();
	const dim = (x: string) => (theme ? theme.fg("dim", x) : x);
	const accent = (x: string) => (theme ? theme.fg("accent", x) : x);
	const bold = (x: string) => (theme && theme.bold ? theme.bold(x) : x);

	const unread = msgs.filter((m) => !seen.has(keyOf(m)) && m.dir === "in").length;
	const newest = msgs[msgs.length - 1];
	const hint = "/toragents-panel";
	const head =
		` toragents · ${total}` + (unread > 0 ? accent(` · ${unread} unread`) : "") + (newest ? "" : dim(" · nothing yet"));
	const pad = Math.max(1, width - plainWidth(head) - hint.length - 1);
	const out: string[] = [truncateToWidth(head + " ".repeat(pad) + dim(hint), width, "")];

	if (!newest) {
		out.push(dim(" no messages on this side yet"));
		while (out.length < rows) out.push("");
		return out.slice(0, rows);
	}

	const claimed = claimBody(newest.body, Math.max(12, width - 4));
	const when = new Date(newest.at).toTimeString().slice(0, 5);
	const arrow = newest.dir === "out" ? "→" : "←";
	out.push(truncateToWidth(` ${accent(arrow)} ${newest.peer} ${dim(when)} ${bold(claimed.subject)}`, width, ""));

	const preview = claimed.rest
		.split(/\n+/)
		.map((l) => l.trim())
		.filter(Boolean)
		.join(" ");
	for (const line of wrapBlock(preview, Math.max(12, width - 2))) {
		if (out.length >= rows) break;
		out.push(truncateToWidth(` ${dim(line)}`, width, ""));
	}
	while (out.length < rows) out.push("");
	return out.slice(0, rows);
}

/** Visible width without ANSI — used to right-align the hint. */
function plainWidth(text: string): number {
	return String(text).replace(/\x1b\[[0-9;]*m/g, "").length;
}

// ── extension ────────────────────────────────────────────────────────────────

/**
 * The live column, kept on globalThis.
 *
 * `/reload` re-evaluates this module: the new instance starts with `column = null`
 * while the previous instance's SideColumn is still patching `terminal.columns`
 * and painting stale rows. Without this handle, reload leaves two overlapping
 * columns and a doubly-narrowed chat. One global, one column.
 */
const LIVE = "__toragentsPanelLive";

export default function (pi: ExtensionAPI) {
	const prev = (globalThis as any)[LIVE];
	if (prev?.dispose) {
		try {
			prev.dispose();
		} catch {}
	}
	(globalThis as any)[LIVE] = undefined;

	let column: SideColumn | null = null;
	let tui: any = null;
	let theme: any = null;
	let msgs: Said[] = [];
	let sig = threadSignature();
	let timer: ReturnType<typeof setInterval> | null = null;
	let lastCtx: ExtensionContext | null = null;
	let unsubscribeInput: (() => void) | null = null;

	const view: ColumnView = {
		selected: 0,
		expanded: new Set<string>(),
		seen: new Set<string>(),
		focused: false,
		top: 0,
	};

	const pageSize = () => Math.max(3, Number(tui?.terminal?.rows || 30) - 4);

	const repaint = () => {
		if (!column?.mounted) return;
		column.invalidate();
		try {
			tui?.requestRender?.();
		} catch {
			/* renderer gone */
		}
	};

	/**
	 * Bring the selected card into view, scrolling as little as possible.
	 *
	 * Two rules, because one does not cover both shapes:
	 *   - a card that FITS scrolls minimally, so the list does not jump when you
	 *     step through it;
	 *   - a card TALLER than the window aligns to its header, because otherwise
	 *     "follow the end" leaves you looking at the tail of a body with no idea
	 *     which message you are in.
	 */
	const revealSelection = () => {
		if (!column?.mounted) return;
		const rows = Number(tui?.terminal?.rows || 30);
		const width = column?.width ?? columnWidth(Number(tui?.terminal?.columns || 0));
		const height = Math.max(3, rows - 2);
		const layout = renderColumn(msgs, width, rows, view, theme);
		const s = layout.starts[view.selected];
		const e = layout.ends[view.selected];
		if (s === undefined || e === undefined) return;
		const cardHeight = e - s + 1;
		if (cardHeight > height) {
			view.top = Math.max(0, layout.absolute[view.selected] ?? 0);
			return;
		}
		if (s < 0) view.top = Math.max(0, view.top + s);
		else if (e > height - 1) view.top = Math.max(0, view.top + (e - (height - 1)));
	};

	const markSeen = (i: number) => {
		const m = msgs[i];
		if (m) view.seen.add(keyOf(m));
	};

	const select = (i: number) => {
		if (msgs.length === 0) return;
		view.selected = Math.max(0, Math.min(msgs.length - 1, i));
		markSeen(view.selected);
		revealSelection();
		repaint();
	};

	const toggle = (i: number) => {
		const m = msgs[i];
		if (!m) return;
		const k = keyOf(m);
		if (view.expanded.has(k)) view.expanded.delete(k);
		else view.expanded.add(k);
		view.seen.add(k);
		revealSelection();
		repaint();
	};

	const scrollLines = (n: number) => {
		view.top = Math.max(0, view.top + n);
		repaint();
	};

	const refresh = () => {
		const next = threadSignature();
		if (next === sig) return false;
		sig = next;
		const before = msgs.length;
		const oldKey = msgs[view.selected] ? keyOf(msgs[view.selected]) : "";
		msgs = readThread();
		writeSnapshot(msgs);
		// Follow the conversation: a new message selects itself unless the reader
		// has deliberately parked the marker on an older one.
		if (msgs.length > before) {
			const parked = view.selected < before - 1;
			if (!parked) {
				// Follow the conversation but leave it unread: the dot is the only
				// thing that tells you mail arrived while you were reading.
				view.selected = msgs.length - 1;
				revealSelection();
			}
		} else if (oldKey) {
			const idx = msgs.findIndex((m) => keyOf(m) === oldKey);
			if (idx >= 0) view.selected = idx;
		}
		repaint();
		return true;
	};

	const mountColumn = (ctx: ExtensionContext): { ok: boolean; why: string } => {
		if (column?.mounted) return { ok: true, why: "already open" };
		if (!ctx?.ui?.setWidget) return { ok: false, why: "this session has no interactive UI" };
		msgs = readThread();
		sig = threadSignature();
		writeSnapshot(msgs);
		view.selected = Math.max(0, msgs.length - 1);
		// Existing mail is history, not a notification: start with everything
		// marked read except the newest message, which is what you came to see.
		view.seen = new Set(msgs.map(keyOf));
		// Collapsed, like a mail list: a 3k-character letter that opens itself
		// fills the whole column and hides every other message.
		view.expanded = new Set<string>();
		view.top = 0;
		let result: { ok: boolean; why: string } = {
			ok: typeof ctx.ui.setWidget === "function",
			why: "mounted",
		};
		ctx.ui.setWidget(
			"toragents-panel-mount",
			(t: any, th: any) => {
				tui = t;
				theme = th;
				if (!column) {
					column = new SideColumn(t, {
						width: columnWidth(Number(t?.terminal?.columns || 0)),
						minTerminalWidth: MIN_TERMINAL,
						render: (w, r) => renderColumn(msgs, w, r, view, theme).lines,
						onWheel: (delta) => {
							scrollLines(delta * 3);
							return true;
						},
					});
				}
				result = column.install();
				if (result.ok) (globalThis as any)[LIVE] = column;
				else {
					try {
						ctx.ui.notify(`toragents: right column not shown — ${result.why}`, "warning");
					} catch {}
				}
				revealSelection();
				column.repaint();
				// pi calls dispose() on every extension widget it tears down (reload,
				// setWidget(undefined), clearExtensionWidgets). Hook it, or the
				// unpainted column keeps patching terminal.columns after teardown.
				return {
					render: () => [] as string[],
					invalidate() {},
					dispose: () => {
						try {
							(globalThis as any)[LIVE] = undefined;
						} catch {}
						try {
							column?.dispose();
						} catch {}
						column = null;
					},
				};
			},
			{ placement: "belowEditor" },
		);
		return result;
	};

	const unmountColumn = (ctx?: ExtensionContext): string => {
		try {
			(globalThis as any)[LIVE] = undefined;
		} catch {}
		try {
			column?.dispose();
		} catch {}
		column = null;
		try {
			(ctx || lastCtx)?.ui?.setWidget("toragents-panel-mount", undefined);
		} catch {}
		try {
			tui?.requestRender?.();
		} catch {}
		view.focused = false;
		return "conversation column closed";
	};

	let stripOn = false;

	/** Bottom strip for sessions that are not the front desk. */
	const mountStrip = (ctx: ExtensionContext): { ok: boolean; why: string } => {
		if (column?.mounted) unmountColumn(ctx);
		if (typeof ctx?.ui?.setWidget !== "function") return { ok: false, why: "this session has no interactive UI" };
		msgs = readThread();
		sig = threadSignature();
		if (view.seen.size === 0) view.seen = new Set(msgs.map(keyOf));
		writeSnapshot(msgs);
		stripOn = true;
		ctx.ui.setWidget(
			"toragents-panel-strip",
			(t: any, th: any) => {
				tui = t;
				theme = th;
				return {
					render: () => renderStrip(msgs, Number(t?.terminal?.columns || 80), view.seen, msgs.length, theme),
					invalidate() {},
					dispose: () => {
						stripOn = false;
					},
				};
			},
			{ placement: "belowEditor" },
		);
		return { ok: true, why: `bottom strip (${stripLines()} lines)` };
	};

	const unmountStrip = (ctx?: ExtensionContext): string => {
		stripOn = false;
		try {
			(ctx || lastCtx)?.ui?.setWidget("toragents-panel-strip", undefined);
		} catch {}
		try {
			tui?.requestRender?.();
		} catch {}
		return "strip closed";
	};

	/** Apply a mode: the wide column, the bottom strip, or nothing. */
	const applyMode = (mode: "column" | "strip" | "off", ctx: ExtensionContext): { ok: boolean; why: string } => {
		if (mode === "off") {
			unmountColumn(ctx);
			unmountStrip(ctx);
			return { ok: true, why: "panel off" };
		}
		if (mode === "strip") return mountStrip(ctx);
		unmountStrip(ctx);
		return mountColumn(ctx);
	};

	/**
	 * Keyboard. Focus is the switch: unfocused, only alt-modified keys are taken,
	 * so typing in the editor is never disturbed. Focused, the column behaves like
	 * a mail list (j/k/enter), and Esc gives the keyboard straight back.
	 */
	pi.on("session_start", async (_event, ctx) => {
		lastCtx = ctx;
		unsubscribeInput?.();
		try {
			unsubscribeInput = ctx.ui.onTerminalInput((data: string) => {
				if (!column?.mounted) return undefined;
				const f = view.focused;
				const taken = () => {
					try {
						tui?.requestRender?.();
					} catch {}
					return { consume: true };
				};

				if (matchesKey(data, Key.alt("f"))) {
					view.focused = !view.focused;
					const m = msgs[view.selected];
					if (m) view.seen.add(keyOf(m));
					repaint();
					return taken();
				}
				if (view.focused && (matchesKey(data, Key.escape) || data === "q")) {
					view.focused = false;
					repaint();
					return taken();
				}

				// movement: alt+j/k always; plain j/k only while focused
				if (matchesKey(data, Key.alt("j")) || matchesKey(data, Key.alt(Key.down)) || (f && (matchesKey(data, Key.down) || data === "j"))) {
					select(view.selected + 1);
					return taken();
				}
				if (matchesKey(data, Key.alt("k")) || matchesKey(data, Key.alt(Key.up)) || (f && (matchesKey(data, Key.up) || data === "k"))) {
					select(view.selected - 1);
					return taken();
				}
				// open / close: alt+e always; enter/space while focused
				if (matchesKey(data, Key.alt("e")) || (f && (matchesKey(data, Key.enter) || data === " "))) {
					toggle(view.selected);
					return taken();
				}
				if (matchesKey(data, Key.alt("a")) || (f && data === "a")) {
					view.expanded = new Set(msgs.map(keyOf));
					repaint();
					return taken();
				}
				if (matchesKey(data, Key.alt("c")) || (f && data === "c")) {
					view.expanded.clear();
					repaint();
					return taken();
				}
				// ends of the list
				if (matchesKey(data, Key.alt("g")) || (f && data === "G")) {
					if (msgs.length > 0) {
						view.selected = msgs.length - 1;
						view.seen.add(keyOf(msgs[view.selected]));
					}
					revealSelection();
					repaint();
					return taken();
				}
				if (matchesKey(data, Key.alt("t")) || (f && data === "g")) {
					if (msgs.length > 0) {
						view.selected = 0;
						view.seen.add(keyOf(msgs[view.selected]));
					}
					revealSelection();
					repaint();
					return taken();
				}
				// page scrolling only while focused (plain pgup/pgdn belong to the
				// chat transcript otherwise)
				if (f && matchesKey(data, Key.pageUp)) {
					scrollLines(-pageSize());
					return taken();
				}
				if (f && matchesKey(data, Key.pageDown)) {
					scrollLines(pageSize());
					return taken();
				}
				return undefined;
			});
		} catch {
			/* no raw input this mode; commands still work */
		}

		// A reload must not stack a second column on top of the old one.
		if ((globalThis as any)[LIVE] === undefined) {
			try {
				ctx.ui.setWidget("toragents-panel-mount", undefined);
			} catch {}
		}
		// Placement follows policy: the designated receiver gets the wide column,
		// every other chat gets a few lines at the bottom. TORAGENTS_PANEL overrides.
		let me: string | undefined;
		try {
			me = pi.getSessionName?.();
		} catch {}
		const mode = panelMode(me);
		const r = applyMode(mode, ctx);
		if (!r.ok) {
			try {
				ctx.ui.notify(`toragents: panel not shown — ${r.why}`, "info");
			} catch {}
		}

		if (timer) clearInterval(timer);
		timer = setInterval(() => {
			try {
				if (stripOn && threadSignature() !== sig) {
					sig = threadSignature();
					const before = new Set(view.seen);
					msgs = readThread();
					// The strip marks nothing read: it is a notification. Opening the
					// column is what counts as reading.
					view.seen = new Set([...before]);
					writeSnapshot(msgs);
					try {
						tui?.requestRender?.();
					} catch {}
					return;
				}
				refresh();
			} catch {
				/* never break the session over a repaint */
			}
		}, POLL_MS);
	});

	pi.on("session_shutdown", async () => {
		if (timer) clearInterval(timer);
		timer = null;
		try {
			unsubscribeInput?.();
		} catch {}
		unsubscribeInput = null;
		try {
			column?.dispose();
		} catch {}
		column = null;
		try {
			lastCtx?.ui?.setWidget("toragents-panel-strip", undefined);
		} catch {}
	});

	pi.registerCommand("toragents-panel", {
		description: "Open the conversation column in this session, or close it back to the strip",
		handler: async (_args: any, ctx: any) => {
			lastCtx = ctx;
			if (column?.mounted) {
				let me: string | undefined;
				try {
					me = pi.getSessionName?.();
				} catch {}
				const back = panelMode(me, { ...readPolicy(), panel: { default: "strip", column: [] } });
				const r = applyMode(back === "off" ? "off" : "strip", ctx);
				try {
					ctx.ui.notify(`toragents: column closed — ${r.why}`, "info");
				} catch {}
				return;
			}
			const r = mountColumn(ctx);
			try {
				ctx.ui.notify(
					`toragents: ${r.why}` + (r.ok ? " — alt+f focuses it; j/k/enter move and open" : ""),
					r.ok ? "info" : "warning",
				);
			} catch {}
		},
	});

	pi.registerCommand("toragents-scroll", {
		description: "Scroll the Tor column: up, down, top, bottom, or a number of lines",
		handler: async (args: any, ctx: any) => {
			lastCtx = ctx;
			const arg = String(args ?? "").trim().toLowerCase();
			if (!column?.mounted) {
				try {
					ctx.ui.notify("toragents: the column is closed — /toragents-panel opens it", "warning");
				} catch {}
				return;
			}
			const n = Number(arg);
			if (arg === "top") {
				view.selected = 0;
				revealSelection();
				repaint();
			} else if (arg === "bottom" || arg === "") {
				if (msgs.length > 0) view.selected = msgs.length - 1;
				revealSelection();
				repaint();
			} else if (Number.isFinite(n) && n !== 0) scrollLines(-n);
			try {
				ctx.ui.notify(`toragents: column scrolled (${arg || "bottom"})`, "info");
			} catch {}
		},
	});
}
