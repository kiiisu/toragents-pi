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
}

export class SideColumn {
	private tui: any;
	private terminal: any;
	private opts: SideColumnOptions;
	private originalColumnsOwn: PropertyDescriptor | undefined;
	private originalColumnsAny: PropertyDescriptor | undefined;
	private originalDoRender: ((...args: any[]) => any) | null = null;
	private originalRouteWheel: ((event: any, delta: number) => any) | null = null;
	private originalWrite: ((data: string) => void) | null = null;
	private disposed = false;
	private installed = false;

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
			buf += moveCursor(row, sepCol) + "\x1b[90m│\x1b[39m";
			buf += moveCursor(row, sepCol + 1) + missing(formatted[i]);
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
