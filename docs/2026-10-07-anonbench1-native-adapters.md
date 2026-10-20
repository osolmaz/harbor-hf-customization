---
title: Anonbench1 native adapters on Harbor-HF
author: Onur Solmaz
date: 2026-10-20
tags: [harbor-hf, anonbench1, harness, attestation]
---

# Anonbench1 native adapters on Harbor-HF

Package `0.1.0rc8` selects Pi 1.3.0, Codex 0.163.0, and Claude Code 2.1.292.
OpenClaw is held until its selected runtime bundle passes admission. Hermes is
excluded from this release. Neither held harness has an executable package export.

## Runtime contract

The worker supplies Harbor 0.24.0 at commit
`11a0fe2d3305e144a5e8af625d50a7394f293f75` and LiteLLM 1.104.1. The adapter wheel
has no runtime dependencies, so installation cannot replace the worker's engine.
The development lock uses the same versions. LiteLLM is a library dependency;
this package starts no proxy.

The three public entry points are:

| Entry point | CLI version | Runtime |
| --- | --- | --- |
| `anonbench1_adapters:Anonbench1Pi` | 1.3.0 | Node 26.11.1 |
| `anonbench1_adapters:Anonbench1Codex` | 0.163.0 | Native Harbor installer |
| `anonbench1_adapters:Anonbench1ClaudeCode` | 2.1.292 | Native Harbor installer |

Each entry point rejects another CLI version before setup. Setup then checks the
installed version and writes `attestation.json`. A mismatch fails the trial.
Native Harbor owns task execution, network policy, verification, and results.
The copied adapters retain their resource and usage observations.

Claude Code maps the reviewed HF router connection into its native Anthropic
environment variables in memory. All four model slots use the selected explicit
provider route. It calls HF's native Messages endpoint directly; no relay or
service special case is needed.

The Codex wrapper preserves the full HF model ID in the native CLI command. The
pinned Harbor command builder otherwise drops the namespace. A strict command
shape check changes that argument only; the prompt, tools, session handling and
API remain native.

Codex and Claude Code can accept an explicit `price` quote in USD per million
tokens. It supplies their existing LiteLLM cost calculation with the exact model
and provider route. Conflicting quotes fail, and unrecognized returned model IDs
are not assigned guessed aliases. `pricing.json` records the quote; calculated
costs are estimates, not provider invoices. No inference call uses LiteLLM.

## Source and package checks

`scripts/sync_anonbench1_adapters.py --source-root <configuration-checkout>` reads
only the allowlisted files from one committed source revision. Its manifest
records both original and published SHA-256 hashes. The only transformations
are the published benchmark name, package-qualified Python imports, and the
comment separator required by the suppression checker. Runtime behavior is unchanged.

Run `--check --source-root <configuration-checkout>` to compare with that exact
revision. CI can run `--check` without the source checkout. Generated source is
not reformatted. Package coverage measures the local wrappers; generated source
is checked separately through provenance, native construction, and protocol tests.

`config/` retains the selected native settings for all harnesses as source
material. These copies do not install a runtime or prove that it is ready.
In particular, OpenClaw 2026.10.5 requires an admitted retained bundle and
source inventory. A version string cannot satisfy that requirement.

## Deployment

The parent Dockerfile pins the upstream worker by digest and installs the
versioned adapter wheel. Publish a candidate image from reviewed source, then
use its immutable digest for an explicitly authorized remote canary. Confirm
version attestation, native results, accounting, and shutdown before merging a
service integration. The service needs no harness-specific code.

Keep credentials, deployment values, task content, and run records out of this
repository. Select explicit providers in private launch configuration. There is
no automatic provider fallback.
