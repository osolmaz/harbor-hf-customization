/**
 * A request bound for small probes, using only Pi's public extension API.
 * @param {{on(event: "before_provider_request", handler: (event: unknown, ctx: {abort(): Promise<void>}) => void): void}} pi
 */
export default function requestLimit(pi) {
	const limit = Number(process.env.HARBOR_PI_MAX_PROVIDER_REQUESTS);
	let requests = 0;
	pi.on("before_provider_request", (_event, ctx) => {
		requests += 1;
		if (requests > limit) {
			// Abort before the provider sends this payload. Do not await completion
			// from inside the request hook: the active loop must first unwind.
			void ctx.abort();
		}
	});
}
