# Harbor-HF customization

This repository holds the reviewed customizations that the Harbor-HF service
needs but must not contain: externally packaged agent harnesses, their pinned
runtimes, and the integration code those harnesses require. Harness behavior
stays here so the control service carries no harness-specific rule.

The repository was named `harbor-custom-harnesses` until 2026-09-24. GitHub
redirects the previous name, so an existing pinned release URL still resolves.

## Pi direct and Code Mode

The Pi harness has two modes. Direct mode uses Pi's built-in tools. Code Mode
loads the Code Mode extension and uses its `codex` tool set. Pi owns the model
conversation and tool execution in both modes. The adapter connects Pi's public
RPC interface to ACP and forwards Pi's token and cost statistics. Harbor owns
task execution, verification, concurrency, retries, and results.

The runtime requires Linux x64 with glibc 2.36 or later and Python 3.12+.
Its wheel contains pinned Node, Pi, and Code Mode binaries. No Rust compiler,
Node installation, or package build is required in a benchmark task.

Select an explicit Hugging Face model and inference provider. The harness uses
Pi's model metadata and reviewed bundled entries for models that Pi does not yet
list. DeepSeek V4.1 Flash has such an entry. The harness always uses the selected
provider's live status, prices, and context limit. It uses normal JSON tool
input, high reasoning, and no automatic model or provider substitution. Supply
the inference credential through
`OPENAI_API_KEY`; no credential value is written to configuration files.
Pi calculates cost from provider token counts and HF's quoted rates. Cached
input uses the full input rate because the public metadata does not quote cache
discounts. This is a conservative estimate, not a provider invoice.

Each directory under `harnesses/` contains native agent manifests, a
`pyproject.toml`, and a `uv.lock`. Pin the repository to a full commit and select
that directory through Harbor's `agents[].kwargs.source.source_dir`. A hosted
service must approve the exact source before it can receive credentials.

### Launch configuration

In a launch form, choose an ACP source agent. Enter this repository's Git URL,
a full commit, and `harnesses/pi-code-mode` as the source directory. Use
`harbor-agent-direct.json` for direct Pi or `harbor-agent-code.json` for Code
Mode. Select the benchmark separately, then set an explicit HF model and
provider. The corresponding native agent fragment is:

```json
{
  "name": "acp",
  "model_name": "openai/<model-namespace>/<model>:<provider>",
  "kwargs": {
    "source": {
      "repo_url": "https://github.com/osolmaz/harbor-hf-customization.git",
      "ref": "<full-40-character-commit>",
      "source_dir": "harnesses/pi-code-mode",
      "manifest_path": "harbor-agent-direct.json"
    }
  }
}
```

Harbor fetches the pinned source and uses its native `python-uv` installer to
install the locked wheel inside the task environment. The wheel starts Pi and
Code Mode, while the ACP adapter carries messages, tool events, usage, and cost
back to Harbor. No harness package needs to be added to the Harbor-HF image.

For a small integration test, select `harbor-agent-direct-canary.json` or
`harbor-agent-code-canary.json`. Each allows at most four provider requests per
process. Use `tests/canary/pi-direct` for direct Pi and
`tests/canary/code-mode` for Code Mode. Select Linux x64 CPU hardware; GPU
hardware is not needed because inference uses the remote HF router. The
canary verifies a file written by Pi. Also inspect the agent logs for the tool
call and confirm that every parent and child Job has stopped.

See [the build instructions](docs/build.md) to build and verify a runtime wheel.
Do not treat an unverified build as a benchmark result.

## Pi through localpi

The `--launcher localpi` option starts the same pinned Pi through the bundled
[`localpi`](https://github.com/osolmaz/localpi) 0.6.3 instead of starting Pi
directly. Everything else is unchanged: the same ACP adapter, the same pinned Pi
and Code Mode extension, and the same HF router route. localpi owns Pi's
configuration for this launcher, so the harness writes only a model profile.

- The provider key is written as the `${OPENAI_API_KEY}` reference. The value
  stays in the environment and never reaches a configuration file or a process
  argument.
- `--continue-on-truncation <n>` continues a reply that the output limit cut off,
  up to `n` times. A cut-off reply that already asked for a tool is left alone,
  because Pi runs that tool and continues on its own.
- The default declares no thinking format, which keeps the provider's own default.
  `--thinking-format qwen-chat-template` adds the Qwen chat-template field to the
  pinned model, and `--thinking <off|low|medium|high>` sets Pi's level. Pi then sends
  `enable_thinking`, so `--thinking off` gives the whole reply budget to the answer.
  The default level stays `high`.
- A probe on 2026-09-24 showed that `Qwen/Qwen3.8-27B:novita` honors `enable_thinking`
  inside `chat_template_kwargs`, and ignores `thinking_budget`, `thinking_token_budget`
  and `reasoning.max_tokens`. A smaller thinking budget is therefore not available on
  that route; only thinking on or off.
- The pinned provider is declared without discovery, so localpi uses the exact
  model id from the run, including its provider suffix, and never substitutes
  another provider.
- localpi writes its own diagnostics to stderr, so Pi's stdout stays a clean
  protocol stream.
- `--max-output-tokens <n>` caps the reply length the run declares, instead of
  taking it from the model catalog. A comparison between two harnesses needs the
  same limit on both sides, and a shorter limit is also how a run reproduces a
  reply that the limit cut off. The value never rises above the context window.
- Pi's own settings come from localpi, so this launcher does not set
  `retry.enabled: false`.

Select the `harnesses/localpi` directory with `harbor-agent-localpi.json` for
direct Pi or `harbor-agent-localpi-code.json` for Code Mode. Both pin wheel
`0.1.0rc7`, which bundles localpi 0.6.3 and carries the reply-limit and thinking
options.

The directory also holds four manifests for reply-limit experiments:
`harbor-agent-limit-pi.json` runs plain Pi at 16,384 output tokens, and
`harbor-agent-limit-localpi.json` runs the same limit with the continuation
guard. The pair isolates the guard, because every other setting is the same.
`harbor-agent-think-localpi.json` and `harbor-agent-nothink-localpi.json` repeat
the guarded run with provider thinking on and off, so the pair isolates the cost
of thinking inside the same reply limit.

### Reviewed endpoint thinking cap (not released)

The router manifests above still use the HF router. They do **not** run either
endpoint or set a thinking cap. The endpoint path is opt-in and is not available
in the currently pinned localpi 0.6.3 wheel. A new reviewed wheel must bundle
a localpi release with endpoint-cap support before a hosted run. The harness
checks that support and fails instead of silently running without a cap.

For an endpoint run, select the ACP source agent through a reviewed Harbor-HF
endpoint connection. The connection supplies `OPENAI_BASE_URL` and
`OPENAI_API_KEY` in the task environment. Do not copy the key or replace the
reviewed URL with a router URL. Set these harness arguments in its pinned
manifest:

```text
--launcher localpi --endpoint-engine vllm --thinking high
--endpoint-context-window <verified-window> --max-output-tokens 16384
--thinking-phase-output-cap 8000 --thinking-format qwen-chat-template
```

Use `--endpoint-engine llama-cpp` for a llama.cpp endpoint. Specify the model
as `openai/<exact-id-from-endpoint-/models>`. The harness checks that exact ID
against the endpoint's `/models` list. It does not use router model prices or
metadata. Set the context window from the deployed endpoint, not a catalog
estimate. `--thinking-format qwen-chat-template` is for an endpoint that
supports that option; leave it as `none` for a server that does not.

The cap keeps thinking on and limits the first request's *total* output to
8,000 tokens. When that request ends in thinking only, localpi asks for the
answer with at most 8,384 tokens from the 16,384-token request limit. It is
not an exact thinking-token counter. The endpoint must honor the limit and
report a length stop. Check the real endpoint transcript before using the
result as an eval. Pi reports zero token-price cost for a host billed by time;
this does **not** mean the host is free. Harbor's run ceiling does not cover
that host bill. Do not resume a paid endpoint or submit a hosted Job until a
cumulative host spending limit is verified and approved.

## OpenClaw native runtime

The OpenClaw harness runs the native embedded OpenClaw agent through its stable
`agent exec` command. It pins OpenClaw source commit
`651775ba8d2c7b10f37c5372d3528b41ecfbc804`, Node 24.18.0, the self-contained
OpenClaw package, and its npm dependency lock in one reviewed wheel.

The two manifests use the same runtime and differ only in the explicit
`--code-mode` argument:

- `harbor-agent-direct.json` uses `--code-mode direct`.
- `harbor-agent-code.json` uses `--code-mode code`.

Both modes force `agentRuntime: {"id": "openclaw"}` for the selected model.
They use no fallback model and no ambient OpenClaw configuration or credential
store. Harbor supplies the model and inference credential. The adapter saves
the stable OpenClaw result envelope and source provenance with the agent logs,
reports native token and cost metrics through ACP, and rejects a model, provider,
or Code Mode mismatch.

Use `tests/canary/openclaw-native` before a paid benchmark. A passing task is not
enough by itself. Also confirm that `openclaw-source.json` has the pinned commit
and that `openclaw-envelope.json` reports `codeModeEngaged` as false for the
direct manifest and true for the Code Mode manifest.

## Adding a harness

Add another directory under `harnesses/` with its own native manifest and lock.
The initial runtime lives under `src/harbor_pi_code_mode/`. Existing pins remain reproducible
when another harness changes. Do not add a second task runner, result format,
model API client, or runtime configuration schema.

## License

[MIT](LICENSE)
