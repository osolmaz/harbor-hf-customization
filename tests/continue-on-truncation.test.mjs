import assert from "node:assert/strict";
import test from "node:test";
import continueOnTruncation from "../src/harbor_pi_code_mode/continue-on-truncation.mjs";

/** Collect the handlers an extension registers. */
function fakePi() {
	const handlers = new Map();
	const sent = [];
	const levels = [];
	return {
		handlers,
		sent,
		levels,
		pi: {
			on(event, callback) {
				assert.equal(handlers.has(event), false);
				handlers.set(event, callback);
			},
			setThinkingLevel(level) {
				levels.push(level);
			},
			sendUserMessage(text, options) {
				sent.push({ text, options });
			},
		},
	};
}

function turnEnd(handlers, message) {
	const handler = handlers.get("turn_end");
	assert.ok(handler, "turn_end must be registered");
	return handler({ message });
}

test("continues every truncated assistant turn up to the default bound", () => {
	const { handlers, sent, pi } = fakePi();
	continueOnTruncation(pi);
	assert.deepEqual([...handlers.keys()], ["session_start", "turn_end"]);
	turnEnd(handlers, { role: "assistant", stopReason: "length" });
	turnEnd(handlers, { role: "assistant", stopReason: "length" });
	turnEnd(handlers, { role: "assistant", stopReason: "length" });
	assert.equal(sent.length, 2);
	for (const entry of sent) {
		assert.equal(entry.options.deliverAs, "followUp");
		assert.match(entry.text, /output limit/);
		assert.match(entry.text, /next tool call/);
	}
});

test("honours a declared bound and stays quiet at zero", () => {
	process.env.HARBOR_PI_CONTINUE_LIMIT = "1";
	const one = fakePi();
	continueOnTruncation(one.pi);
	turnEnd(one.handlers, { role: "assistant", stopReason: "length" });
	turnEnd(one.handlers, { role: "assistant", stopReason: "length" });
	assert.equal(one.sent.length, 1);
	process.env.HARBOR_PI_CONTINUE_LIMIT = "0";
	const off = fakePi();
	continueOnTruncation(off.pi);
	assert.equal(off.handlers.size, 0);
	assert.equal(off.sent.length, 0);
	delete process.env.HARBOR_PI_CONTINUE_LIMIT;
});

test("ignores turns that ended for another reason", () => {
	const { handlers, sent, pi } = fakePi();
	continueOnTruncation(pi);
	for (const message of [
		undefined,
		{ role: "user", stopReason: "length" },
		{ role: "assistant", stopReason: "stop" },
		{ role: "assistant", stopReason: "toolUse" },
		{ role: "assistant" },
	]) {
		turnEnd(handlers, message);
	}
	assert.equal(sent.length, 0);
});

test("resets the bound when a new session starts", () => {
	const { handlers, sent, pi } = fakePi();
	continueOnTruncation(pi);
	turnEnd(handlers, { role: "assistant", stopReason: "length" });
	turnEnd(handlers, { role: "assistant", stopReason: "length" });
	assert.equal(sent.length, 2);
	const start = handlers.get("session_start");
	assert.ok(start);
	start({ reason: "reload" });
	turnEnd(handlers, { role: "assistant", stopReason: "length" });
	assert.equal(sent.length, 3);
});

test("changes the thinking level only when it is declared", () => {
	process.env.HARBOR_PI_CONTINUE_THINKING = "low";
	const declared = fakePi();
	continueOnTruncation(declared.pi);
	turnEnd(declared.handlers, { role: "assistant", stopReason: "length" });
	assert.deepEqual(declared.levels, ["low"]);
	assert.equal(declared.sent.length, 1);
	delete process.env.HARBOR_PI_CONTINUE_THINKING;
	const plain = fakePi();
	continueOnTruncation(plain.pi);
	turnEnd(plain.handlers, { role: "assistant", stopReason: "length" });
	assert.deepEqual(plain.levels, []);
	assert.equal(plain.sent.length, 1);
});

test("refuses an unusable bound", () => {
	for (const value of ["-1", "1.5", "two", ""]) {
		process.env.HARBOR_PI_CONTINUE_LIMIT = value;
		assert.throws(
			() => continueOnTruncation(fakePi().pi),
			/nonnegative integer/,
		);
	}
	delete process.env.HARBOR_PI_CONTINUE_LIMIT;
});
