/**
 * Addressing: who delivers what, when two windows are open.
 *
 * The user's original complaint was "I opened a second pi session and it receives
 * messages too". The rule that answers it has two halves, and conflating them
 * caused a hole the other way round: mail addressed to a NON-owner session was
 * delivered to nobody, because the owner skipped it (not addressed to it) and the
 * named window declined (not the owner). Both halves are asserted here.
 *
 * This tests the RULE, not two live processes. Two real windows on one box still
 * have to be observed; what is proven here is that the decision function cannot be
 * right in one direction and wrong in the other without a red test.
 */

import { wantsMessage } from "../toragents_watch.ts";

let pass = 0;
let fail = 0;

function check(name: string, got: unknown, want: unknown) {
	const ok = JSON.stringify(got) === JSON.stringify(want);
	if (ok) {
		pass++;
		console.log(`  PASS  ${name}  -> ${JSON.stringify(got)}`);
	} else {
		fail++;
		console.log(`  FAIL  ${name}  -> got ${JSON.stringify(got)} want ${JSON.stringify(want)}`);
	}
}

const ME = "pi-aaaa";
const OTHER = "pi-bbbb";

console.log("addressing");

// -- untargeted mail: exactly one window, and it is the owner -----------------
check("untargeted, I own delivery -> I take it",
	wantsMessage({}, { owner: true, me: ME }), true);
check("untargeted, I do NOT own -> I must stay silent (no duplicates)",
	wantsMessage({}, { owner: false, me: ME }), false);
check("untargeted, receive=targeted -> skipped even by the owner",
	wantsMessage({}, { owner: true, me: ME, receive: "targeted" }), false);

// -- addressed mail: exactly one window, owner or not --------------------------
check("addressed to me, I do NOT own -> I take it (this was delivered to nobody)",
	wantsMessage({ to_session: ME }, { owner: false, me: ME }), true);
check("addressed to me, I own -> I take it",
	wantsMessage({ to_session: ME }, { owner: true, me: ME }), true);
check("addressed to the other window, I own -> I leave it alone",
	wantsMessage({ to_session: OTHER }, { owner: true, me: ME }), false);
check("addressed to the other window, I do not own -> I leave it alone",
	wantsMessage({ to_session: OTHER }, { owner: false, me: ME }), false);
check("addressed with whitespace padding still matches",
	wantsMessage({ to_session: ` ${ME} ` }, { owner: false, me: ME }), true);
check("addressed but I have no label yet -> I cannot claim it",
	wantsMessage({ to_session: ME }, { owner: true, me: "" }), false);

// -- the pair that must never both be true for one message --------------------
const targeted = { to_session: ME };
const ownerTakes = wantsMessage(targeted, { owner: true, me: ME });
const otherTakes = wantsMessage(targeted, { owner: false, me: OTHER });
check("one named message, two windows: only the named one takes it",
	[ownerTakes, otherTakes], [true, false]);

console.log(`\n${pass} passed, ${fail} failed`);
if (fail) process.exit(1);
