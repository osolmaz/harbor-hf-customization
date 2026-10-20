// Selective evidence in the existing Pi session, never raw payloads or headers.
const TYPE = "anonbench1-provider-evidence";
const TOKEN_FIELDS = [
  "input_tokens", "output_tokens", "prompt_tokens", "completion_tokens", "total_tokens",
  "cache_read_input_tokens", "cache_creation_input_tokens",
];

export default function campaignEvidence(pi) {
  let evidence;
  function retain(list, value) {
    if (list.some((entry) => JSON.stringify(entry) === JSON.stringify(value))) return;
    if (list.length < 64) list.push(value);
    else evidence.truncated = true;
  }
  pi.on("turn_start", (_event, ctx) => {
    evidence = {
      schema_version: 1,
      codemode_active: pi.getActiveTools().includes("codemode"),
      thinking_level: pi.getThinkingLevel(),
      model_capabilities: ctx.model ? {
        id: ctx.model.id, api: ctx.model.api, reasoning: ctx.model.reasoning,
        thinking_level_map: ctx.model.thinkingLevelMap ? { ...ctx.model.thinkingLevelMap } : null,
      } : null,
      provider_request_efforts: null, provider_efforts: null,
      models: [], usage: [], reasoning_usage: [], truncated: false,
    };
  });
  pi.on("before_provider_request", (event) => {
    if (!evidence || !event.payload || typeof event.payload !== "object") return;
    evidence.provider_request_efforts ??= [];
    const fields = [
      ["reasoning.effort", event.payload.reasoning?.effort],
      ["reasoning_effort", event.payload.reasoning_effort],
    ].filter(([, value]) => value !== undefined);
    if (!fields.length) fields.push([null, undefined]);
    for (const [field, value] of fields) {
      const valid = typeof value === "string" && value.length <= 64;
      retain(evidence.provider_request_efforts, {
        field, value: valid ? value : null,
        presence: value === undefined ? "absent" : valid ? "present" : "invalid",
        evidence_source: "before_provider_request.payload",
      });
    }
  });
  pi.on("provider_stream_event", (event) => {
    if (!evidence) return;
    const raw = event.data;
    if (!raw || typeof raw !== "object") return;
    for (const chunk of [raw, raw.response, raw.message]) {
      if (!chunk || typeof chunk !== "object") continue;
      // The outer event.model is requested metadata, not provider evidence.
      if (typeof chunk.model === "string" && chunk.model.length > 0) {
        if (chunk.model.length <= 512) {
          retain(evidence.models, { requested_model: event.model, observed_model: chunk.model });
        } else evidence.truncated = true;
      }
      for (const [field, value] of [
        ["reasoning.effort", chunk.reasoning?.effort],
        ["reasoning_effort", chunk.reasoning_effort],
      ]) {
        if (typeof value === "string" && value.length <= 64) {
          evidence.provider_efforts ??= [];
          retain(evidence.provider_efforts, { field, value, evidence_source: "provider_stream_event" });
        }
      }
      if (chunk.usage && typeof chunk.usage === "object") {
        const counts = Object.fromEntries(TOKEN_FIELDS.flatMap((key) => {
          const value = chunk.usage[key];
          return Number.isFinite(value) && value >= 0 ? [[key, value]] : [];
        }));
        const reasoning = chunk.usage.output_tokens_details?.reasoning_tokens
          ?? chunk.usage.completion_tokens_details?.reasoning_tokens;
        if (Number.isSafeInteger(reasoning) && reasoning >= 0) {
          counts.reasoning_tokens = reasoning;
          if (typeof chunk.id === "string" && chunk.id.length > 0 && chunk.id.length <= 512) {
            retain(evidence.reasoning_usage, { response_id: chunk.id, tokens: reasoning });
          }
        }
        if (Object.keys(counts).length) retain(evidence.usage, counts);
      }
    }
  });
  function finish(messageEntryId) {
    if (!evidence) return;
    pi.appendEntry(TYPE, {
      ...evidence,
      message_entry_id: messageEntryId ?? null,
      scope: "turn observations; nested calls and aggregate completeness are not qualified",
    });
    evidence = undefined;
  }
  pi.on("turn_end", (event) => finish(event.messageEntryId));
  pi.on("agent_end", () => finish(null));
}
