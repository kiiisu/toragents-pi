/**
 * toragents-policy — who is the front desk, and who gets a full pane.
 *
 * The transport already routes correctly: untargeted mail belongs to whichever
 * session owns delivery, addressed mail belongs to the session it names, and a
 * reply is addressed back to the session that wrote. What was missing was
 * designation — ownership went to whoever claimed first, so "who is the receiver"
 * was decided by start order and could change while you watched it.
 *
 * This module answers exactly one question — WHERE the reading UI goes:
 *   - toragents-panel.ts asks `panelMode()` so only the receiver gets the wide
 *     right-hand column and every other chat gets the four-line strip.
 *
 * The other half of the policy — WHO owns delivery — is NOT here. It lives in the
 * delivery extension (toragents_watch.ts, installed as toragents-inbox.ts), because
 * that file is distributed on its own: a published module may not import from
 * _lib/, and two copies of the ownership rule is precisely the second-registry
 * rot this whole day was spent deleting. policy.json is the contract between the
 * two files; the rule has one home each side.
 *
 * NAME COMPARISON is the one thing that must not drift, so it is deliberately
 * literal here: trimmed, case-sensitive equality, matching the delivery side
 * (`meName === policy.receiver`). A lenient compare on this side would let the
 * panel show a column for a session the desk does not consider the receiver.
 *
 * Policy lives in ~/.toragents/policy.json (or $TORAGENTS_HOME/policy.json), NOT
 * in the state dir: state is churn (chat.jsonl, inbox/, announced.json), config
 * is something a human edited once. Missing or malformed file = defaults, and the
 * defaults are the pre-policy behaviour, so this can land without ceremony.
 */

import { readFileSync, renameSync, statSync, writeFileSync } from "node:fs";
import { homedir } from "node:os";
import { join } from "node:path";

export interface PanelPolicy {
	/** "column" for the wide right pane, "strip" for the bottom lines, "off". */
	default: "column" | "strip" | "off";
	/** Sessions that always get the column, by session name. */
	column: string[];
	/**
	 * Width the user dragged the column to, in cells. 0 means "not chosen yet",
	 * which keeps the proportional default. Written only on drag release, so the
	 * file changes when a decision is made, not while one is being made.
	 */
	width: number;
}

export interface Policy {
	/** The session everyone treats as the front desk. Undefined = old behaviour. */
	receiver?: string;
	panel: PanelPolicy;
	/**
	 * Whether a non-receiver may hold delivery while no live receiver exists.
	 * Default true: a closed front desk should not make the channel mute.
	 */
	fallbackToAnyone: boolean;
}

export const POLICY_DEFAULTS: Policy = {
	panel: { default: "strip", column: [], width: 0 },
	fallbackToAnyone: true,
};

export function policyPath(): string {
	const home = process.env.TORAGENTS_HOME || join(homedir(), ".toragents");
	return join(home, "policy.json");
}

let cache: { at: number; policy: Policy } | null = null;

/**
 * Read the policy, cached on mtime.
 *
 * Called on a poll tick, so it must not re-read the file 76 times a minute; and
 * it must never throw, because a broken config file should degrade to "everyone
 * behaves as before", not break the channel.
 */
export function readPolicy(force = false): Policy {
	const path = policyPath();
	let at = 0;
	try {
		at = statSync(path).mtimeMs;
	} catch {
		return POLICY_DEFAULTS;
	}
	if (!force && cache && cache.at === at) return cache.policy;
	let raw: any = {};
	try {
		raw = JSON.parse(readFileSync(path, "utf8"));
	} catch {
		return POLICY_DEFAULTS;
	}
	const receiver = typeof raw?.receiver === "string" && raw.receiver.trim() ? raw.receiver.trim() : undefined;
	const mode = raw?.panel?.default;
	const policy: Policy = {
		receiver,
		panel: {
			default: mode === "column" || mode === "off" || mode === "strip" ? mode : POLICY_DEFAULTS.panel.default,
			column: Array.isArray(raw?.panel?.column)
				? raw.panel.column.filter((n: unknown): n is string => typeof n === "string" && n.trim() !== "")
				: [],
			width: Number.isFinite(Number(raw?.panel?.width)) ? Math.max(0, Math.round(Number(raw.panel.width))) : 0,
		},
		fallbackToAnyone: raw?.fallbackToAnyone === false ? false : true,
	};
	cache = { at, policy };
	return policy;
}

/**
 * True when this session is the designated front desk.
 *
 * Identity is the session NAME (pi's /name), because that is what the delivery
 * side compares. An id is not a name: a window carrying rowan's id under a
 * different name is not the receiver, and neither side pretends otherwise.
 */
export function isReceiver(name: string | undefined, policy = readPolicy()): boolean {
	if (!policy.receiver) return false;
	return (name || "").trim() === policy.receiver;
}

/**
 * How this session should show the conversation.
 *
 * TORAGENTS_PANEL wins over policy — it is the escape hatch an operator uses
 * while testing ("show me the column in this window"), and an env var is easier
 * to type than an edit plus restart.
 */
export function panelMode(name: string | undefined, policy = readPolicy()): "column" | "strip" | "off" {
	const env = String(process.env.TORAGENTS_PANEL || "").trim().toLowerCase();
	if (env === "0" || env === "off") return "off";
	if (env === "column") return "column";
	if (env === "strip" || env === "widget") return "strip";
	if (name && policy.panel.column.includes(name.trim())) return "column";
	if (isReceiver(name, policy)) return "column";
	return policy.panel.default;
}

/**
 * Remember the width the user chose.
 *
 * Read-modify-write of the whole file, because policy.json is a human-editable
 * document and dropping keys we do not recognise would make this tool the owner
 * of a file a person also edits. Atomic (tmp + rename) so a crash mid-write
 * cannot leave a truncated config, and the cache is updated in place so the next
 * read does not race the file system's mtime granularity.
 *
 * Returns false when the write fails -- a read-only home should lose the
 * preference, not the pane.
 */
export function writePanelWidth(width: number): boolean {
	const path = policyPath();
	const n = Math.max(0, Math.round(Number(width) || 0));
	let raw: any = {};
	try {
		raw = JSON.parse(readFileSync(path, "utf8"));
	} catch {
		raw = {};
	}
	if (!raw || typeof raw !== "object") raw = {};
	if (!raw.panel || typeof raw.panel !== "object") raw.panel = {};
	raw.panel.width = n;
	try {
		const tmp = `${path}.${process.pid}.tmp`;
		writeFileSync(tmp, `${JSON.stringify(raw, null, 2)}\n`, { mode: 0o600 });
		renameSync(tmp, path);
	} catch {
		return false;
	}
	cache = null; // next readPolicy() takes the file's own word for it
	return true;
}
