# Harbor Pi agent bugs

These bugs are in Harbor's built-in Pi agent, `src/harbor/agents/installed/pi.py`, at the Harbor revision that Harbor-HF pins (`fcf27e5502e30436b067ef2d655368e86cc42cf9`). The first one is also present on Harbor `main` as of 2026-09-26. They showed up when a Terminal-Bench 2.1 run with Harbor's Pi agent was rerun on HF, and neither is caused by the model. The custom Code Mode harness in this repository talks to Pi over RPC and ships its own Node runtime, so it avoids both.

## Instruction that starts with a dash

Harbor builds the Pi command line as `pi --print --mode json ... <quoted instruction>`, with nothing between the last flag and the instruction. When a task instruction starts with `-`, Pi parses it as an option and exits with `Error: Unknown option: - You are given ...`. The Terminal-Bench 2.1 task `pytorch-model-recovery` fails this way on every attempt.

Passing `--` before the instruction fixes it. With a local Pi, `pi --print --mode json --model <model> -- "- hello"` reads the prompt, and the same command without `--` fails with `Unknown option: - hello`.

## curl install through apt

Before installing Pi, Harbor's `install()` calls `ensure_system_dependencies(environment, ("curl",))`. When an image has no curl, this runs `apt-get update && apt-get install -y curl`. On the Debian bullseye images of `qemu-startup` and `qemu-alpine-ssh`, `apt-get update` succeeds but the curl packages in `bullseye-security` return `404 Not Found`, so the install exits with status 100 and the agent never starts.

The failure depends on the state of the Debian mirror, so a plain retry can fail again. A sturdier install would use `wget` when it exists, retry apt with `--fix-missing`, or install Node without curl.

## Status

Neither bug has an upstream Harbor issue or pull request yet. Opening one needs the owner's confirmation, as the Harbor-HF instructions require.
