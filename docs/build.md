# Build a pinned harness runtime

Build the wheel on Linux x64 using the reviewed Dockerfile:

```sh
docker buildx build --platform linux/amd64 \
  -f build/pi-code-mode/Dockerfile --output type=local,dest=dist .
```

The image pins official Node and Rust images by digest. It builds the Code Mode
source at a full commit using its npm and Cargo locks. Pi dependencies use the
separate npm lock in `build/pi-code-mode/`. The wheel contains the Python ACP
adapter, Node, Pi, the extension, and their license files.

The build runs no-inference smoke tests with the real packaged Pi in direct and
Code Mode against a local scripted HTTP peer. These are transport and tool
tests, not model inference or benchmark evidence. A third test sets a
one-request limit and verifies that Pi aborts before a second HTTP request
reaches the peer.

Publish a successful wheel as a prerelease asset in this repository. Do not
replace an existing asset. Create the native harness project with a direct URL
dependency on that exact wheel and run `uv lock`. The lock retains the wheel
hash; no custom download protocol or artifact registry is needed.

Build the pinned OpenClaw native runtime separately:

```sh
docker buildx build --platform linux/amd64 \
  -f build/openclaw-native/Dockerfile \
  --output type=local,dest=dist/openclaw-native .
```

This build checks out the exact OpenClaw commit and runs OpenClaw's canonical
self-contained Docker package builder and package-integrity check. The wheel
contains the package tarball, official Node 24.18.0, npm, an npm lock, license
files, and the ACP adapter. It does not contain credentials. Runtime setup uses
`npm ci` against that lock. The no-inference payload smoke test verifies the
source marker, Node version, lock format, and package artifact.

A harness source pin consists of the full repository commit and its native
`source_dir` and manifest path. Operators must review that exact source before
it receives a credential. Keep deployment identifiers and remote canary records
in the operator's existing private control and artifact stores.

## Boundaries

The adapter uses Pi's documented `get_state`, `prompt`, `abort`,
`get_session_stats`, and `agent_settled` RPC messages. It does not call Pi
internals or implement a second conversation loop. Pi's standard sessions and
raw RPC events are normal agent output below `/logs/agent/pi-code-mode/`.
Harbor collects them with its agent logs and writes authoritative trial results.

Pi owns session entries, including the Code Mode extension's normal contract
entry. Other transient data consists of standard Pi settings in a temporary
directory. The custom model file contains an environment-variable name, never
the credential value. No Pi schema or internal API is changed.

Only one ACP session and one active prompt are accepted by a process. Model
selection is advertised through ACP's native model configuration option and
must match the model supplied by Harbor. Provider failures and missing usage
fail closed. The adapter reports Pi's actual statistics rather than inventing
zero-cost success. Pi reports an unknown context token count just after
compaction. The adapter skips that context-only update and still returns the
cumulative token counts. If cost rises while context usage is unknown, the
adapter fails rather than hide the extra cost. Cost is Pi's estimate from HF's
quoted rates, with cached input charged at the full input rate when no cache
price is published.

For a bounded probe, a native manifest can add `--max-provider-requests 4` to
its entrypoint. This uses Pi's public `before_provider_request` hook and
`ctx.abort()` before an excess request. The counter is process-local and also
counts compaction requests. It does not change Pi's session format or write a
budget store. A fresh process gets a fresh limit, so operators must count all
attempts and retries against the campaign limit. The ordinary entrypoint has
no request limit. Harbor still owns trial timeouts and retries.
