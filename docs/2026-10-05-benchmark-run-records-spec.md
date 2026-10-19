---
date: 2026-10-05
author: Onur Solmaz
title: Self-contained benchmark run records
tags: [runs, harnesses, harbor, reproducibility, spec]
---

# Self-contained benchmark run records

Status: past runs from 2026-09-11 to 2026-09-26 are recorded under `runs/`. The payload split and the repository check are not implemented yet.

## Problem

A Harbor run pins a harness by repository, full commit, `source_dir`, and manifest. This repository keeps one folder per harness under `harnesses/`, and the adapter code lives in the shared `src/` and `runtimes/` trees. All of these change over time. The pins that past runs used are spread over Harbor-HF presets, scratch files, and a launch file in another repository. To see what code a past run used, you must find its commit and check it out. Nothing lists the runs by date.

## Goal

Each benchmark run gets one dated folder. Inside it, each harness that the run used gets its own folder with everything Harbor needs to install and start it: the manifest, the Python project, its lock, and the adapter source. A reader can see exactly what ran without an older checkout, and Harbor can run the same folder again.

## Layout

```
runs/
  2026-09-25-terminal-bench-2-1-nemotron/
    README.md
    pi-code-mode.yaml
    pi-code-mode/
      harbor-agent.json
      pyproject.toml
      uv.lock
      src/harbor_pi_code_mode/
      build/
    openclaw-native.yaml
    openclaw-native/
      ...
```

### Run folder

- The name is `YYYY-MM-DD-<benchmark>-<subject>`, in lowercase with hyphens. The date is the UTC day of the first launch. `<benchmark>` is the benchmark id that Harbor-HF presets use, such as `terminal-bench-2-1`, `shellbench-structured`, or `aacr-bench`. `<subject>` names what the run compares, such as a model or a harness change.
- One run folder covers one benchmark. A campaign that runs two benchmarks has two run folders.
- `README.md` gives the purpose, the models and providers, and the budget. After the launch, it also gives the Harbor-HF run IDs and their status.

### Harness folder

- The name is the harness name from `harnesses/`, such as `pi-code-mode`, `localpi`, or `openclaw-native`. When a run has more than one folder for the same harness, each name adds a short arm name, such as `localpi-think` and `localpi-nothink`.
- The folder is Harbor's `source_dir`. Harbor uploads all of it into the task environment and runs `uv sync --frozen` in it. The folder therefore holds only what the install needs, plus the small `build/` record.
- It contains:
  - `harbor-agent.json`: the Harbor ACP source manifest. Its entrypoint carries every flag for this run, so the folder has exactly one manifest.
  - `pyproject.toml` and `uv.lock`: the Python project. The project is the adapter itself, built from `src/`. Every other dependency, including the runtime payload, is pinned in `uv.lock` with its hash.
  - `src/<package>/`: the exact adapter source that runs.
  - `build/`: the inputs that produced the runtime payload, which are the Dockerfile, the npm `package.json` and `package-lock.json`, the pinned extension commits, and the sha256 of the payload wheel. With these, the payload can be rebuilt and compared.
- The folder does not contain Node, Pi, the Code Mode binary, or any other built output. These come from the payload wheel that `uv.lock` pins by URL and sha256.

### Job config

- `<harness>.yaml`, next to the harness folder, is the Harbor job config for that harness. It uses Harbor's native `JobConfig` format and adds no schema of its own.
- Its agent entry is `name: acp`, with `kwargs.source` set to this repository, the full commit, `source_dir: runs/<run>/<harness>`, and `manifest_path: harbor-agent.json`. It also sets `manifest_sha256`.
- When several runs used the same harness folder, for example a canary and the full run, the job config is the main run's config, and the README lists the other runs.
- The job config is outside the harness folder for two reasons. A file cannot hold the hash of its own commit. And the harness folder must stay identical to the pinned commit, so nothing may be added to it later.

## Rules

1. Commit the harness folder first. Then write the job config with `ref` set to that commit, commit it, and launch from it.
2. Do not change a harness folder or a job config after a launch. A fix goes in a new harness folder or a new run folder. Only `README.md` gets later additions, such as run IDs and status.
3. Keep credentials, local paths, and private values out. This repository is public. It was made public on 2026-09-08 so that Harbor-HF can fetch harness sources without a credential; this spec does not re-check that need.
   - Put keys and private endpoints in the agent `env` as `${VAR}` references. Harbor resolves these from the launch environment.
   - Harbor does not resolve `${VAR}` in `model_name`. A run whose model name is not yet public is recorded here only after the name becomes public.
   - Name datasets by registry name and version, or by repository and full revision. Never use a local path.
4. Results stay in the Harbor-HF run Bucket. The README gives run IDs and does not copy trial output.
5. Development stays where it is. `src/`, `runtimes/`, `build/`, and `harnesses/` remain the place to change and test the adapters. A run folder receives a copy at launch time.

## Checks

A repository check enforces these rules for every change under `runs/`:

- Each run folder name matches `YYYY-MM-DD-<benchmark>-<subject>`.
- Each harness folder has its manifest, project, lock, source, and `build/` record, and its manifest passes Harbor's `AcpSourceManifest` validation.
- Each job config passes Harbor's `JobConfig` validation and pins a full 40-character commit that exists in this repository.
- When a job config points `source_dir` into `runs/`, the harness folder at the pinned commit is identical to the folder at the current head. This proves that nobody edited it after the launch.
- No file holds an absolute path or a value that looks like a credential.

## Past runs

Past runs are added with the same layout, with two differences:

- The job config keeps the pin that actually ran. That pin points into `harnesses/` at an older commit, not into the run folder.
- The harness folder is a copy of the files at that commit: the manifest and project from `harnesses/<harness>/`, and the adapter source from `src/` or `runtimes/`. At that time the adapter ran from inside the released wheel. The copied source is the source at the pin. The wheels were uploaded by hand, so nothing proves that a wheel was built from that source. To run a past run again, use its original pin. The README marks the folder as a reconstruction and names the source commit.

The pins come from Harbor-HF run records in the run Bucket, from Harbor-HF presets, and from launch files. A past run is added only when its pin can be found. Earlier probes that used other pins are listed in the README without a harness folder.

## Changes this needs

- The current runtime wheels bundle the adapter and the payload together. For a harness folder to run its own `src/`, the payload must become a separate wheel that holds only Node, Pi, the extensions, and their licenses. The adapter then finds the payload through that package instead of through its own package directory.
- `AGENTS.md` says to never publish private run records. It must allow run folders that follow this spec.
- `README.md` gets a short section that points to `runs/` and to this spec.

## Open questions

- Where the payload wheels are hosted. They are GitHub release assets today. This spec needs only an immutable URL and a hash in `uv.lock`.
