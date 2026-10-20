import { createHash } from "node:crypto";
import { writeFile } from "node:fs/promises";
import { isAbsolute } from "node:path";

export default {
  id: "anonbench1-catalog",
  register(api) {
    api.registerCli(
      ({ program, config }) => {
        program
          .command("anonbench1-catalog")
          .description("Capture native projected catalog entries for qualification")
          .requiredOption("--agent-dir <path>", "Isolated prepared agent directory")
          .requiredOption("--workspace-dir <path>", "Isolated prepared workspace")
          .requiredOption("--output <path>", "Fresh private catalog JSON file")
          .option("--thinking-model <provider/model>", "Exact built-in catalog model")
          .option("--thinking-runtime <id>", "Explicit runtime for native policy (openclaw)")
          .option("--thinking-output <path>", "Fresh private native policy JSON file")
          .action(async ({ agentDir, workspaceDir, output, thinkingModel, thinkingRuntime, thinkingOutput }) => {
            if (![agentDir, workspaceDir, output].every(isAbsolute)) {
              throw new Error("Catalog paths must be absolute");
            }
            const thinkingRequested = [thinkingModel, thinkingRuntime, thinkingOutput].some(Boolean);
            if (thinkingRequested && (!thinkingModel || thinkingRuntime !== "openclaw" ||
                !thinkingOutput || !isAbsolute(thinkingOutput) || thinkingOutput === output)) {
              throw new Error("Thinking capture requires an exact model, openclaw runtime and distinct absolute output");
            }
            // The executable CLI owns the active publication and its native cleanup.
            const { loadPreparedModelCatalog } = await import("openclaw/plugin-sdk/agent-runtime");
            const entries = await loadPreparedModelCatalog({
              config,
              agentDir,
              workspaceDir,
              env: process.env,
              readOnly: false,
              refreshFullCatalog: true,
            });
            const bytes = Buffer.from(JSON.stringify(entries) + "\n");
            const catalogSha256 = createHash("sha256").update(bytes).digest("hex");
            let thinkingBytes;
            if (thinkingRequested) {
              const matches = entries.filter((entry) => `${entry.provider}/${entry.id}` === thinkingModel);
              if (matches.length !== 1 ||
                  (matches[0].nativeRuntime != null && matches[0].nativeRuntime !== thinkingRuntime)) {
                throw new Error("Thinking capture requires one exact row owned by the selected runtime");
              }
              const entry = matches[0];
              const policy = api.runtime.agent.resolveThinkingPolicy({
                provider: entry.provider, model: entry.id,
                agentRuntime: thinkingRuntime, catalog: [entry],
              });
              thinkingBytes = Buffer.from(JSON.stringify({
                schema_version: 1, owner: "api.runtime.agent.resolveThinkingPolicy",
                runtime: thinkingRuntime, model_name: thinkingModel,
                catalog_sha256: catalogSha256, catalog_entry: entry, policy,
              }) + "\n");
            }
            await writeFile(output, bytes, { flag: "wx", mode: 0o600 });
            if (thinkingBytes) {
              await writeFile(thinkingOutput, thinkingBytes, { flag: "wx", mode: 0o600 });
            }
            // This precedes CLI finalization; it is deliberately not a settlement receipt.
            process.stdout.write(JSON.stringify({
              schema: 1,
              status: "call_completed",
              catalog: {
                kind: "native_projected_catalog_entries",
                bytes: bytes.length,
                sha256: catalogSha256,
                entries: entries.length,
              },
              ...(thinkingBytes ? { thinking_policy: {
                bytes: thinkingBytes.length,
                sha256: createHash("sha256").update(thinkingBytes).digest("hex"),
              } } : {}),
            }) + "\n");
          });
      },
      {
        descriptors: [{
          name: "anonbench1-catalog",
          description: "Capture native projected catalog entries for qualification",
          hasSubcommands: false,
        }],
      },
    );
  },
};
