/**
 * toragents-side-column — a real right-hand column inside pi.
 *
 * WHY THIS EXISTS
 * pi's public UI surface has no right pane: `ctx.ui.setWidget()` only places
 * content ABOVE or BELOW the editor, and an overlay would float over the chat
 * text instead of beside it. Both options cost the thing that matters, which is
 * chat height (a 40-line widget under the editor is literally half a 46-row
 * terminal).
 *
 * HOW IT WORKS (borrowed from `pi-sidebar-tui`, which ships this technique)
 *   1. pi lays itself out against `terminal.columns`. Replacing that property
 *      with a getter that subtracts our width makes pi reflow into the left
 *      part of the screen on its own — no fork, no patched renderer.
 *   2. `tui.doRender` is wrapped so that, inside one synchronized-output frame,
 *      pi draws its narrower layout and we then draw the reserved columns.
 *      pi's full-line erase (`ESC[2K`) is rewritten to erase only the main
 *      width, or pi's next frame would wipe our column.
 *   3. Painting saves and restores the cursor (DECSC/DECRC) with auto-wrap off,
 *      and only rewrites rows whose text changed, so the column does not flicker
 *      or fight the chat viewport.
 *
 * Everything is reversible: dispose() restores the property descriptor and the
 * original doRender, so turning the column off leaves pi exactly as it was.
 *
 * It is deliberately conservative: if the terminal is too narrow to split, or
 * the pi build does not expose doRender, it refuses to mount rather than
 * painting over the chat.
 */

import { truncateToWidth, visibleWidth } from "@earendil-works/pi-tui";

const SAVE = "\x1b7"; // DECSC
const RESTORE = "\x1b8"; // DECRC
const WRAP_OFF = "\x1b[?7l";
const WRAP_ON = "\x1b[?7h";
const SYNC_ON = "\x1b[?2026h";
const SYNC_OFF = "\x1b[?2026l";

function moveCursor(row: number, col: number): string {
	return `\x1b[${row};${col}H`;
}

function descriptorFor(obj: object, key: string): PropertyDescriptor | undefined {
	let target: object | null = obj;
	while (target) {
		const d = Object.getOwnPropertyDescriptor(target, key);
		if (d) return d;
		target = Object.getPrototypeOf(target);
	}
	return undefined;
}

/**
 * Where the divider is, and what the pointer is doing to it.
 *
 * Pure, so the drag can be tested without a terminal: the panel's mouse handling
 * is the part you cannot exercise by reading code twice, and simulation of the
 * real event stream is worth more than a carefully-worded comment.
 */
export type DividerIntent =
	| { kind: "none" }
	| { kind: "hover" }
	| { kind: "start" }
	| { kind: "drag"; width: number }
	| { kind: "end"; width: number };

/** Narrowest column, and the space the chat may never be squeezed below. */
export const COLUMN_MIN = 30;
export const CHAT_MIN = 60;

/**
 * Clamp a requested width to what this terminal can actually give.
 *
 * The upper bound is the chat's, not the column's: a pane you can widen until
 * the conversation is a sliver is a pane that can hide the thing it explains.
 */
export function clampColumnWidth(width: number, cols: number): number {
	const n = Math.round(Number(width));
	const max = Math.max(COLUMN_MIN, Math.round(cols) - CHAT_MIN);
	return Math.max(COLUMN_MIN, Math.min(max, Number.isFinite(n) ? n : COLUMN_MIN));
}

/**
 * Decode one SGR mouse report (`ESC [ < b ; x ; y (M|m)`) into pi's shape.
 *
 * Only used by the tests and by anything that needs to reason about a raw
 * sequence: pi's own parser already hands us normalized events at runtime.
 */
export function parseSgrMouse(data: string): { button: number; x: number; y: number; release: boolean } | null {
	const m = /^\x1b\[<(\d+);(\d+);(\d+)([Mm])$/.exec(data);
	if (!m) return null;
	return { button: Number(m[1]), x: Number(m[2]) - 1, y: Number(m[3]) - 1, release: m[4] === "m" };
}

/**
 * Classify a mouse event against the divider.
 *
 * `button` is pi's raw SGR bitmask: bits 0-1 are the button, bit 5 (32) marks
 * motion, and `release` is the `m` terminator. Only the divider column and its
 * two neighbours are ours — everywhere else the event belongs to the transcript
 * (selection, links), so we must not swallow it.
 */
export function dividerIntent(input: {
	cols: number;
	width: number;
	x: number;
	button: number;
	release: boolean;
	dragging: boolean;
	/** Width currently under the pointer during a drag. */
	candidate?: number;
}): DividerIntent {
	const cols = Math.round(Number(input.cols));
	const x = Number(input.x);
	if (!Number.isFinite(cols) || cols <= 0 || !Number.isFinite(x)) return { kind: "none" };
	const motion = (input.button & 32) !== 0;
	const left = (input.button & 3) === 0;
	const atPointer = clampColumnWidth(cols - Math.round(x) - 1, cols);

	if (input.dragging) {
		if (input.release) return { kind: "end", width: clampColumnWidth(input.candidate ?? input.width, cols) };
		return { kind: "drag", width: atPointer };
	}
	const dividerX = cols - clampColumnWidth(input.width, cols) - 1;
	if (Math.abs(x - dividerX) > 1) return { kind: "none" };
	if (input.release) return { kind: "none" }; // a click on the divider is not a command yet
	if (motion) return left ? { kind: "drag", width: atPointer } : { kind: "hover" };
	if (left) return { kind: "start" };
	return { kind: "none" };
}

export interface SideColumnOptions {
	/** Column width in cells (excluding the 1-cell separator). */
	width: number;
	/** Narrowest terminal that may be split; below this the column is skipped. */
	minTerminalWidth?: number;
	/** Called when pi is about to draw. Returning undefined hides the column. */
	render: (width: number, rows: number) => string[] | undefined;
	/**
	 * Mouse wheel over the column. `delta` follows pi's convention: positive is
	 * downward. Return true when consumed (it then does not scroll the chat).
	 *
	 * Only fires in fullscreen mode, because that is the only mode where pi owns
	 * the viewport and routes wheel events to components at all. In regular mode
	 * the terminal scrollback sits behind everything and there is nothing to
	 * claim.
	 */
	onWheel?: (delta: number, event: any) => boolean;
	/**
	 * Called when a drag of the divider ends, with the width the user chose.
	 *
	 * Not called during the drag: committing width means changing
	 * `terminal.columns`, which reflows the whole chat, and doing that per motion
	 * event is how a resize turns into a slideshow. The drag paints a ghost; the
	 * release is the one reflow.
	 */
	onResizeCommit?: (width: number) => void;
}

export class SideColumn {
	private tui: any;
	private terminal: any;
	private opts: SideColumnOptions;
	private originalColumnsOwn: PropertyDescriptor | undefined;
	private originalColumnsAny: PropertyDescriptor | undefined;
	private originalDoRender: ((...args: any[]) => any) | null = null;
	private originalRouteWheel: ((event: any, delta: number) => any) | null = null;
	private originalHandleMouse: ((event: any) => any) | null = null;
	private originalWrite: ((data: string) => void) | null = null;
	private disposed = false;
	private installed = false;
	/** Pointer is over the divider: the line thickens so the edge is findable. */
	private hovering = false;
	/** Candidate width while a drag is in progress; null when not dragging. */
	private pending: number | null = null;

	private cached: string[] | null = null;
	private cachedCols = 0;
	private cacheValid = false;

	constructor(tui: any, opts: SideColumnOptions) {
		this.tui = tui;
		this.terminal = tui?.terminal;
		this.opts = opts;
	}

	/** Told to repaint by the owner (new message, scroll, resize). */
	invalidate(): void {
		this.cacheValid = false;
	}

	get mounted(): boolean {
		return this.installed && !this.disposed;
	}

	/** The width actually reserved, so callers measure with the number they paint with. */
	get width(): number {
		return this.opts.width;
	}

	/** True while the user is dragging the divider. */
	get resizing(): boolean {
		return this.pending !== null;
	}

	/**
	 * Change the reserved width in place.
	 *
	 * The `columns` getter reads `opts.width` on every access, so the reflow is
	 * immediate on the next pi frame; this only has to invalidate the cache.
	 */
	setWidth(width: number): void {
		const next = clampColumnWidth(width, this.rawColumns());
		if (next === this.opts.width) return;
		this.opts.width = next;
		this.invalidate();
		this.repaint();
	}

	install(): { ok: boolean; why: string } {
		if (this.installed) return { ok: true, why: "already mounted" };
		if (this.disposed) return { ok: false, why: "disposed column cannot be remounted" };
		const t = this.terminal;
		if (!t || typeof t.write !== "function") return { ok: false, why: "no terminal on this tui" };
		const raw = typeof t.columns === "number" ? t.columns : 80;
		if (raw < (this.opts.minTerminalWidth ?? 100)) {
			return { ok: false, why: `terminal is ${raw} columns; need ${this.opts.minTerminalWidth ?? 100}` };
		}
		this.originalWrite = t.write.bind(t);

		// 1. pi reflows into the left part.
		this.originalColumnsAny = descriptorFor(t, "columns");
		this.originalColumnsOwn = Object.getOwnPropertyDescriptor(t, "columns");
		const orig = this.originalColumnsAny;
		const self = this;
		Object.defineProperty(t, "columns", {
			configurable: true,
			enumerable: true,
			get() {
				const rawCols = orig?.get
					? (orig.get.call(t) ?? 80)
					: typeof orig?.value === "number"
						? orig.value
						: 80;
				return Math.max(1, rawCols - self.opts.width - 1);
			},
		});

		// 2. draw the column inside pi's own frame.
		if (typeof this.tui.doRender === "function") {
			const original = this.tui.doRender;
			this.originalDoRender = original;
			this.tui.doRender = function (...args: any[]) {
				if (self.disposed) return original.apply(this, args);
				const writeOwn = Object.getOwnPropertyDescriptor(t, "write");
				const write = t.write;
				const mainWidth = self.mainWidth();
				let force = false;
				self.originalWrite!(SYNC_ON);
				try {
					Object.defineProperty(t, "write", {
						configurable: true,
						enumerable: true,
						writable: true,
						value(this: any, data: string) {
							if (typeof data !== "string") return write.call(this, data);
							// A scroll/newline/clear invalidates our absolute rows.
							if (/\x1b\[(?:2J|3J)|\n|\r\n|\x1b\[\d*[ST]|\x1bM/.test(data)) force = true;
							const clean = data
								.replace(/\x1b\[\?2026[hl]/g, "")
								.replace(/\x1b\[2K/g, `\x1b[${mainWidth}X`);
							return write.call(this, clean);
						},
					});
					try {
						const out = original.apply(this, args);
						self.paintInternal(force);
						return out;
					} catch (err) {
						throw err;
					} finally {
						if (writeOwn) Object.defineProperty(t, "write", writeOwn);
						else Reflect.deleteProperty(t, "write");
					}
				} finally {
					self.originalWrite!(SYNC_OFF);
				}
			};
		} else {
			return { ok: false, why: "this pi build has no tui.doRender to hook" };
		}

		// 3. wheel over the column scrolls the column, not the chat. Without this
		// the chat's viewport steals every wheel event that crosses the divider,
		// which is what "the panes do not scroll separately" felt like.
		if (typeof this.tui.routeWheel === "function") {
			const originalWheel = this.tui.routeWheel;
			this.originalRouteWheel = originalWheel;
			const self = this;
			this.tui.routeWheel = function (event: any, delta: number) {
				if (self.disposed || !self.installed) return originalWheel.call(self.tui, event, delta);
				const x = Number(event?.x);
				const cols = self.rawColumns();
				const divider = cols - self.opts.width - 1; // 0-based x of the separator cell
				if (Number.isFinite(x) && x >= divider) {
					const consumed = self.opts.onWheel?.(delta, event);
					if (consumed !== false) return; // never scroll the chat from over the column
				}
				return originalWheel.call(self.tui, event, delta);
			};
		}

		// 4. the divider is a handle. pi hit-tests its own component tree, and this
		// column is deliberately not in it (it is painted beside pi, not inside),
		// so the only seam that sees pointer events here is the alt-screen's own
		// dispatch. Same class of patch as routeWheel above -- private method,
		// verified by tests, replaced if pi ever exposes a public hook.
		if (typeof this.tui.handleMouseEvent === "function") {
			const originalMouse = this.tui.handleMouseEvent;
			this.originalHandleMouse = originalMouse;
			this.tui.handleMouseEvent = function (event: any) {
				if (self.disposed || !self.installed) return originalMouse.call(self.tui, event);
				if (self.routeMouse(event)) return; // handled here; never reaches selection
				return originalMouse.call(self.tui, event);
			};
		}

		this.installed = true;
		this.cacheValid = false;
		return { ok: true, why: `mounted ${this.opts.width} columns` };
	}

	dispose(): void {
		if (!this.installed && !this.disposed) {
			this.disposed = true;
			return;
		}
		this.disposed = true;
		this.installed = false;
		try {
			if (this.originalColumnsOwn) Object.defineProperty(this.terminal, "columns", this.originalColumnsOwn);
			else Reflect.deleteProperty(this.terminal, "columns");
		} catch {
			/* terminal gone: nothing to restore */
		}
		if (this.originalDoRender) {
			this.tui.doRender = this.originalDoRender;
			this.originalDoRender = null;
		}
		if (this.originalRouteWheel) {
			this.tui.routeWheel = this.originalRouteWheel;
			this.originalRouteWheel = null;
		}
		if (this.originalHandleMouse) {
			this.tui.handleMouseEvent = this.originalHandleMouse;
			this.originalHandleMouse = null;
		}
		this.hovering = false;
		this.pending = null;
		// Wipe our columns so no stale text is left beside the chat.
		try {
			const rows = Number(this.terminal?.rows || 0);
			const cols = this.rawColumns();
			let buf = SAVE + WRAP_OFF;
			for (let r = 1; r <= rows; r++) buf += moveCursor(r, cols - this.opts.width) + "\x1b[K";
			buf += WRAP_ON + RESTORE;
			this.originalWrite?.(buf);
		} catch {
			/* best effort */
		}
	}

	repaint(): void {
		this.cacheValid = false;
		this.paintInternal(true);
	}

	/**
	 * Route one pointer event: the divider, or nothing at all.
	 *
	 * True means the event was ours. Everything else falls through to pi --
	 * including motion a column away from the divider, because a pane that
	 * swallows pointer events it has no use for breaks selection in the chat it
	 * is supposed to sit beside.
	 */
	private routeMouse(event: any): boolean {
		const intent = dividerIntent({
			cols: this.rawColumns(),
			width: this.opts.width,
			x: Number(event?.x),
			button: Number(event?.button || 0),
			release: Boolean(event?.release),
			dragging: this.pending !== null,
			candidate: this.pending ?? undefined,
		});
		switch (intent.kind) {
			case "none":
				if (this.hovering) {
					this.hovering = false;
					this.invalidate();
					this.repaint();
				}
				return false;
			case "hover":
				if (!this.hovering) {
					this.hovering = true;
					this.invalidate();
					this.repaint();
				}
				return true;
			case "start":
				this.hovering = true;
				this.pending = clampColumnWidth(this.opts.width, this.rawColumns());
				this.invalidate();
				this.repaint();
				return true;
			case "drag":
				this.pending = intent.width;
				this.invalidate();
				this.repaint();
				return true;
			case "end": {
				const chosen = intent.width;
				this.pending = null;
				this.hovering = false;
				this.opts.width = chosen; // the reflow happens on pi's next frame
				this.invalidate();
				this.repaint();
				try {
					this.opts.onResizeCommit?.(chosen);
				} catch {
					/* persisting the width is best effort, never fatal */
				}
				return true;
			}
		}
		return false;
	}

	private rawColumns(): number {
		const d = this.originalColumnsAny;
		const raw = d?.get ? d.get.call(this.terminal) : typeof d?.value === "number" ? d.value : 80;
		return typeof raw === "number" && Number.isFinite(raw) ? Math.max(1, Math.floor(raw)) : 80;
	}

	private mainWidth(): number {
		return Math.max(1, this.rawColumns() - this.opts.width - 1);
	}

	private paintInternal(force: boolean): void {
		if (this.disposed || !this.installed) return;
		if (this.tui?.mode !== undefined && String(this.tui.mode).includes("print")) return;
		const rows = Number(this.terminal?.rows || 0);
		const cols = this.rawColumns();
		const sw = this.opts.width;
		if (rows < 8 || cols < (this.opts.minTerminalWidth ?? 100)) return;

		const missing = (v: string) => v;
		const content = this.opts.render(sw, rows === 0 ? 24 : rows - 0);
		if (!content) return;

		const sepCol = cols - sw;
		// The edge advertises itself: plain line, thick line under the pointer,
		// double line while it is being dragged. No cursor-shape escape games --
		// terminal support for those is a lottery; a glyph is not.
		const sepGlyph = this.pending !== null ? "║" : this.hovering ? "┃" : "│";
		const sepPaint = this.pending !== null || this.hovering
			? `\x1b[36m${sepGlyph}\x1b[39m`
			: `\x1b[90m${sepGlyph}\x1b[39m`;
		const pad = (text: string) => {
			const cut = truncateToWidth(text, sw, "", true);
			return cut + " ".repeat(Math.max(0, sw - visibleWidth(cut)));
		};
		const formatted: string[] = [];
		for (let i = 0; i < rows; i++) formatted.push(pad(content[i] ?? ""));

		const dimsChanged = this.cachedCols !== cols;
		const all = force || !this.cacheValid || dimsChanged || !this.cached;
		const dirty: number[] = [];
		if (all) for (let i = 0; i < rows; i++) dirty.push(i);
		else for (let i = 0; i < rows; i++) if (this.cached![i] !== formatted[i]) dirty.push(i);
		if (dirty.length === 0) return;

		let buf = SAVE + WRAP_OFF;
		for (const i of dirty) {
			const row = i + 1;
			buf += moveCursor(row, sepCol) + sepPaint;
			buf += moveCursor(row, sepCol + 1) + missing(formatted[i]);
		}
		// While dragging, the committed column stays put and a ghost edge follows
		// the pointer with the width beside it: the number is the thing being
		// chosen, so it should be readable before the choice is made.
		if (this.pending !== null) {
			const ghostWidth = clampColumnWidth(this.pending, cols);
			const ghostCol = cols - ghostWidth - 1;
			if (ghostCol >= 1) {
				for (let r = 1; r <= rows; r++) buf += moveCursor(r, ghostCol) + "\x1b[36m║\x1b[39m";
				const label = String(ghostWidth);
				buf += moveCursor(1, Math.max(1, ghostCol - label.length - 1)) + `\x1b[36m${label}\x1b[39m`;
			}
		}
		buf += WRAP_ON + RESTORE;

		try {
			this.originalWrite!(buf);
		} catch {
			this.cacheValid = false;
			return;
		}
		this.cached = formatted;
		this.cachedCols = cols;
		this.cacheValid = true;
	}
}
