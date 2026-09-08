import assert from "node:assert/strict";
import test from "node:test";
import requestLimit from "../src/harbor_pi_code_mode/request-limit.mjs";

test("allows the bound and aborts every later request without changing payloads", () => {
	process.env.HARBOR_PI_MAX_PROVIDER_REQUESTS = "4";
	let handler;
	requestLimit({
		on(event, callback) {
			assert.equal(event, "before_provider_request");
			handler = callback;
		},
	});
	assert.ok(handler);
	let aborts = 0;
	const context = {
		abort: async () => {
			aborts += 1;
		},
	};
	const event = { payload: { messages: [] } };
	for (let index = 0; index < 4; index += 1) {
		assert.equal(handler(event, context), undefined);
		assert.equal(aborts, 0);
	}
	handler(event, context);
	assert.equal(aborts, 1);
	handler(event, context);
	assert.equal(aborts, 2);
	assert.deepEqual(event.payload, { messages: [] });
	delete process.env.HARBOR_PI_MAX_PROVIDER_REQUESTS;
});
