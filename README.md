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
[`localpi`](https://github.com/osolmaz/localpi) 0.6.2 instead of starting Pi
directly. Everything else is unchanged: the same ACP adapter, the same pinned Pi
and Code Mode extension, and the same HF router route. localpi owns Pi's
configuration for this launcher, so the harness writes only a model profile.

- The provider key is written as the `${OPENAI_API_KEY}` reference. The value
  stays in the environment and never reaches a configuration file or a process
  argument.
- `--continue-on-truncation <n>` continues a reply that the output limit cut off,
  up to `n` times. A cut-off reply that already asked for a tool is left alone,
  because Pi runs that tool and continues on its own.
- The run declares no thinking format. The HF router rejects the vendor thinking
  fields that Pi sends for Qwen and DeepSeek models (`chat_template_kwargs`,
  `thinking`), and a rejected request returns no answer at all.
- Pi's own settings come from localpi, so this launcher does not set
  `retry.enabled: false`.

Select the `harnesses/localpi` directory with `harbor-agent-localpi.json` for
direct Pi or `harbor-agent-localpi-code.json` for Code Mode. Both pin wheel
`0.1.0rc5`, which is the first wheel that bundles localpi.

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
