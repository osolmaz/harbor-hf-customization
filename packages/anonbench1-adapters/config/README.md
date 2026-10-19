# Anonbench1 runtime settings

These native Harbor templates record the merged configuration source at
`d18e8e375677d8cbf7ac00c5af6ad9a359ae7b67`. They are staged for integration.
The current adapter wheel cannot run all of these settings yet. The required
Harbor-HF engine update is merged; hosted deployment and qualification remain.

| Harness | Version | Node |
| --- | --- | --- |
| Pi | 1.3.0 | 26.11.1 |
| OpenClaw | 2026.10.5, with managed Codex 0.160.0 | 24.19.0 |
| Hermes | v2026.9.24 / 0.21.5 | 26.11.1 |
| Standalone Codex | 0.163.0 | Native setup; retained-runtime replay requires its own receipt |
| Claude Code | 2.1.292 | Native setup |

Each harness directory holds its full `harbor.json`, `pins.json` and
`job-template.yaml`.
The copies preserve setup timeouts, explicit code mode, MCP and model-input
settings, provider environment references, runtime admission, dependency
identities, workspace artifacts and qualification limits. Model and API
placeholders remain unresolved. The only change to the source bytes is the
published Anonbench1 name. Source hashes describe the original source files;
copied-file hashes describe these renamed copies. They are separate identities.

## Worker dependency

The source requires Harbor `11a0fe2d3305e144a5e8af625d50a7394f293f75`,
recorded in each `harbor.json`. Harbor-HF main now pins this same commit;
[the engine update](https://github.com/huggingface/harbor-hf/pull/251) also aligns
the worker and control launch guards, lock and exported native schema.
The new OpenClaw adapter requires a prepared runtime and source inventory;
changing its version argument alone is insufficient.

Before these templates can become active presets, the operator must deploy
the reviewed Harbor-HF engine update, port the complete selected adapters and
helpers, and qualify the exact source,
runtime and provider combinations remotely. Existing smoke receipts apply
only to their original frozen inputs. This snapshot does not qualify the
renamed copies or publish a worker image.

The Pi, OpenClaw and Hermes install commands also pin LiteLLM 1.104.1.
The standalone Codex and Claude Code source commands omit that override;
their separate Harbor metadata preserves that distinction.

The current wheel, parent Dockerfile and published presets retain their
existing pins. The separate verifier-isolation candidate is outside this
snapshot.

## Released OpenClaw selection

The October 19 selection replaces the previous main-based candidate with
the stable `v2026.10.5` release at
`1b58cb9a1b4f4353e1add63c8506db4d890962e8`. It uses that release's managed
Codex 0.160.0, fs-safe 0.25.0, pnpm 12.9.0 and state schema 20. Use a fresh
bundle and state snapshot; the previous candidate's Codex 0.162.0 and schema
26 are not interchangeable with these inputs.

The selection remains preparation-only. Required-fix equivalence, a fresh
two-plugin build, native catalog, verifier-enabled Trial, accounting and
retention checks remain pending. Preserve these holds from `openclaw/pins.json`.

## Setup on another machine

Follow the upstream [machine setup guide](https://github.com/ShellBench/harbor-config/blob/main/docs/machine-setup.md)
at the exact configuration revision recorded in `sync-manifest.json`.
Use a clean source checkout and a separate private work directory. Run the
source checkout's read-only `tools/verify_runtime_sources.py --source
CONFIG_CHECKOUT` before preparing any runtime. It verifies the five complete
adapter and helper inventories, including the shared observer; the published
configuration copies here are not that adapter inventory.

The source controller recipe uses Python 3.12.11 and uv 0.12.23 with its root
frozen lock. Preserve the selected runtime's full adapter/helper closure,
including the October 19 OpenClaw run and October 17 shared observer. Do not
substitute another run's helper or preparer. Standalone Codex retained-runtime
replay requires Node 22.23.3 and its own verified capture; ordinary installation
is a different preparation route.

Check Docker and Compose, exact image access, architecture, resources and
required isolation on the execution host. Task exports, runtime captures,
provider bindings, credentials and outputs stay external. Source verification
does not qualify an installed runtime or authorize execution.

## Sync

Use the existing adapter sync script with `--settings`. Supply the reviewed
configuration checkout as `--source-root`. The script reads committed files
from its exact HEAD, so local edits cannot enter the snapshot.

```sh
python scripts/sync_anonbench1_adapters.py --settings --source-root CONFIG_CHECKOUT
python scripts/sync_anonbench1_adapters.py --settings --check
python scripts/sync_anonbench1_adapters.py --settings --check --source-root CONFIG_CHECKOUT
```

The last command also compares the copies with their recorded source commit.
CI verifies the full copied-file inventory and hashes without private source
access. A configuration sync requires its own review; it does not authorize
execution or release.
