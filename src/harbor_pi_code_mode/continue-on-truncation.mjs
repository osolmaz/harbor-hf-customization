/**
 * Continue a turn that the provider cut off at the output limit, using only Pi's
 * public extension API.
 *
 * A truncated turn that carries tool calls already keeps Pi running. A truncated
 * turn that carries only reasoning does not: the agent loop ends and the run
 * stops before any deliverable is written. This extension closes that gap by
 * queueing one follow-up instruction per truncation, up to a declared bound.
 *
 * Environment:
 * - `HARBOR_PI_CONTINUE_LIMIT`: follow-ups allowed per session. Default 2. Zero
 *   disables the extension.
 * - `HARBOR_PI_CONTINUE_THINKING`: optional Pi thinking level to set before each
 *   follow-up, such as `low`. Unset leaves the level unchanged.
 */
const NUDGE =
	"You hit the output limit before finishing. Continue from where you stopped. " +
	"Do not restate earlier reasoning. Take the next tool call or write the answer.";

/** @returns {number} Follow-ups allowed in one session. */
function continuationLimit() {
	const raw = process.env.HARBOR_PI_CONTINUE_LIMIT ?? "2";
	if (!/^\d+$/.test(raw)) {
		throw new Error("The continuation limit must be a nonnegative integer");
	}
	return Number(raw);
}

/**
 * Queue a follow-up instruction after a truncated assistant turn.
 *
 * @param {{
 *   on: (event: string, handler: (event: {message?: {role?: string, stopReason?: string}}) => void) => void;
 *   setThinkingLevel: (level: string) => void;
 *   sendUserMessage: (text: string, options: {deliverAs: "followUp"}) => void;
 * }} pi Pi's public extension API surface used here.
 */
export default function continueOnTruncation(pi) {
	const limit = continuationLimit();
	const thinking = process.env.HARBOR_PI_CONTINUE_THINKING || undefined;
	if (limit === 0) {
		return;
	}
	let used = 0;
	pi.on("session_start", () => {
		used = 0;
	});
	pi.on("turn_end", (event) => {
		const message = event?.message;
		if (message?.role !== "assistant" || message.stopReason !== "length") {
			return;
		}
		if (used >= limit) {
			return;
		}
		used += 1;
		if (thinking) {
			pi.setThinkingLevel(thinking);
		}
		pi.sendUserMessage(NUDGE, { deliverAs: "followUp" });
	});
}
