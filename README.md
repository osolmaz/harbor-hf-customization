# Harbor custom harnesses

This repository contains externally packaged agents for Harbor benchmarks.
It keeps custom harness code and pinned dependencies outside the Harbor worker.

## Pi with Code Mode

The Pi harness loads the Code Mode extension and uses its `codex` tool set.
Pi owns the model conversation and tool execution. The adapter connects Pi's
public RPC interface to ACP and forwards Pi's token and cost statistics.
Harbor owns task execution, verification, concurrency, retries, and results.

The runtime requires Linux x64 with glibc 2.36 or later and Python 3.12+.
Its wheel contains pinned Node, Pi, and Code Mode binaries. No Rust compiler,
Node installation, or package build is required in a benchmark task.

Select an explicit Hugging Face model and inference provider. The harness uses
Pi's model metadata and the selected provider's reported prices and context
limit. It uses normal JSON tool input, high reasoning, and no automatic model
or provider substitution. Supply the inference credential through
`OPENAI_API_KEY`; no credential value is written to configuration files.

Each `harnesses/<name>/` directory will contain a native `harbor-agent.json`,
`pyproject.toml`, and `uv.lock`. Pin the repository to a full commit and select
that directory through Harbor's `agents[].kwargs.source.source_dir`. A hosted
service must approve the exact source before it can receive credentials.

See [the build instructions](docs/build.md) to build and verify a runtime wheel.
Do not treat an unverified build as a benchmark result.

## Adding a harness

Add another directory under `harnesses/` with its own native manifest and lock.
Keep its runtime package under `packages/`. Existing pins remain reproducible
when another harness changes. Do not add a second task runner, result format,
model API client, or runtime configuration schema.

## License

[MIT](LICENSE)
