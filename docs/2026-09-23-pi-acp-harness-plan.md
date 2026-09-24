# Plan: an ACP harness that runs Pi through pi-acp and localpi

Date: 2026-09-23
Status: selected for implementation

## Goal

Add a second Pi harness identity to this repository that speaks ACP by running
`pi-acp`, with `localpi` as the Pi launcher. Keep the continuation guard active,
so a truncated turn still continues. Publish it as its own pinned runtime and
leave the existing `pi-code-mode` and `pi-qwen27b` pins untouched.

## Why

`pi-acp` is a maintained ACP adapter for Pi (owner `svkozak`, MIT, npm
`pi-acp@0.0.33`, published with provenance). `localpi` (owner `osolmaz`) is the
launcher used for daily local work, and it already declares model limits and
passes thinking-budget flags to a llama-server. Running that pair under Harbor
lets a benchmark measure the setup that is actually used, in place of a
purpose-built adapter.

## Verified facts

These were checked in the source of the pinned revisions, not assumed.

- `pi-acp` spawns Pi as `pi --mode rpc --no-themes`, plus `--session <path>`
  when a session path exists. It does not pass `--no-extensions`, so Pi
  extension auto-discovery stays enabled.
- `pi-acp` reads `PI_ACP_PI_COMMAND` and uses it as the executable name, so
  another launcher can replace `pi`.
- `pi-acp` reads global settings from `$PI_CODING_AGENT_DIR/settings.json` and
  project settings from `<cwd>/.pi/settings.json`, and it runs the child with
  the current environment.
- `localpi` forwards `--mode` to Pi and only rejects it together with `--demo`,
  so `localpi --mode rpc` is a valid replacement command.
- `localpi` has a model profile with `client.context_window` and
  `client.max_tokens`, and a thinking budget that becomes
  `--reasoning-budget` plus `--reasoning-budget-message` on a llama-server it
  starts.
- Pi exposes no ACP mode of its own. ACP comes from an adapter, so the harness
  entrypoint must be the adapter, not Pi.
- The repository convention is one ACP entrypoint per wheel, with
  `runtime.kind: python-uv` and a publish per version (`pi-code-mode-0.1.0rc4`,
  `pi-qwen27b-0.1.0rc5`).

## Design

1. Pin, do not vendor. Add `pi-acp@0.0.33` and a pinned `localpi` release to the
   wheel build as npm dependencies with a lockfile. Do not copy their source
   into this repository.
2. Keep one ACP entrypoint shape. Add a thin Python entrypoint that starts the
   bundled `pi-acp` on stdio, matching the existing runtime kind. Do not add a
   new runtime kind.
3. Default the Pi command to the bundled Pi. Set `PI_ACP_PI_COMMAND` to the
   bundled `localpi` through the harness environment so the launcher is a
   declared value, not an implicit one.
4. Load the continuation guard through a path `pi-acp` preserves. Prefer the
   project `.pi/extensions/` directory or the guard shipped in the pinned
   settings, and prove the guard fires. Do not depend on a `-e` flag that the
   adapter never passes.
5. Keep direct mode only. Code Mode stays with the existing harness.
6. Nudge the model first on a truncated turn, exactly as the current guard does.
   Do not force a lower thinking level unless a launch declares it.
7. Declare the served context and reply limits. Take them from the model profile
   when localpi supplies one, and never invent a smaller reply cap than the
   declared one.

## Deliverables

- `harnesses/pi-acp/` with a direct manifest, a bounded canary manifest, a
  `pyproject.toml`, and a `uv.lock`.
- A new pinned prerelease wheel that bundles `pi-acp` and `localpi` at fixed
  versions.
- An offline test that drives the harness over ACP against a scripted local
  peer: session creation, one prompt, one tool call, and one truncated turn that
  the guard continues. No real model inference.
- A README section and a short document under `docs/` that name the pinned
  versions and the environment variables.
- A CI step that locks and checks the new harness project.

## Acceptance checks

1. `uv lock --check --project harnesses/pi-acp` passes.
2. The ACP test passes against the scripted peer, and the log shows a second
   provider request after the truncated turn.
3. The existing harness projects still pass their checks unchanged.
4. The wheel contains `pi-acp`, `localpi`, the guard, and the bundled Node.
5. A remote canary passes on an authorized endpoint before any service
   integration is claimed.

## Out of scope

- Changes to `harnesses/pi-code-mode` and `harnesses/pi-qwen27b` pins.
- Any code in `harbor-hf` or in Pi itself.
- Vendoring third-party source, or any runtime built from source.
- The thinking-budget flags on the Bonsai Space, which belong to that Space.
- Paid arms. A canary and any arm need a separate spending decision.

## Authority

- Repository: `osolmaz/harbor-custom-harnesses` only.
- Allowed: edit, test, commit, push, open a pull request.
- Not allowed: merge without an explicit instruction, publish a release that
  replaces an existing asset, install or copy credentials, deploy the control
  Space, or push to any other repository.
