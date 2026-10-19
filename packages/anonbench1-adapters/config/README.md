# Anonbench1 runtime settings

These native Harbor templates record the merged configuration source at
`d15fe9a942f292967e6272234850336b29e30f54`. They are staged for integration.
The current adapter wheel and worker cannot run all of these settings yet.

| Harness | Version | Node |
| --- | --- | --- |
| Pi | 1.3.0 | 26.11.1 |
| OpenClaw | 2026.10.4, with managed Codex 0.162.0 | 24.19.0 |
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

The source requires Harbor `11a0fe2d3305e144a5e8af625d50a7394f293f75`
from `osolmaz/harbor`, recorded in each `harbor.json`. The current Harbor-HF
worker and launch check require upstream Harbor
`3c82380859d187957cfd5cd64802b076d9779550`.
The new OpenClaw adapter imports cleanup and artifact APIs that are absent
from the worker's required commit. It also requires a prepared runtime and
source inventory; changing its version argument alone is insufficient.

Before these templates can become active presets, the operator must align
the Harbor-HF worker and launch check with a reviewed Harbor revision, port
the complete selected adapters and helpers, and qualify the exact source,
runtime and provider combinations remotely. Existing smoke receipts apply
only to their original frozen inputs. This snapshot does not qualify the
renamed copies or publish a worker image.

The Pi, OpenClaw and Hermes install commands also pin LiteLLM 1.104.1.
The standalone Codex and Claude Code source commands omit that override;
their separate Harbor metadata preserves that distinction.

The current wheel, parent Dockerfile and published presets retain their
existing pins. The separate verifier-isolation candidate is outside this
snapshot.

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
