#!/usr/bin/env bash
# Package only the admitted source bundle. No model calls or benchmark input.
set -euo pipefail
bundle_dir=$1
runtime_dir=$2
mkdir -p "$runtime_dir/packs" "$runtime_dir/node" "$runtime_dir/core" "$runtime_dir/exporter"
cp "$bundle_dir/openclaw-2026.10.5.tgz" "$runtime_dir/packs/"
cp "$bundle_dir/openclaw-openclaw-atif-0.1.4.tgz" "$runtime_dir/packs/"
curl -fLSs --retry 3 https://nodejs.org/dist/v24.19.0/node-v24.19.0-linux-x64.tar.xz -o "$runtime_dir/node.tar.xz"
printf '%s  %s\n' 14b342e71204f811bde6153be8e04b62aef63c236fef92b55f9c83154b409647 "$runtime_dir/node.tar.xz" | sha256sum -c -
tar -xJf "$runtime_dir/node.tar.xz" --strip-components=1 -C "$runtime_dir/node"
rm "$runtime_dir/node.tar.xz"
export PATH="$runtime_dir/node/bin:$PATH"
test "$(node --version)" = v24.19.0
npm install --global npm@11.19.0
for target in core exporter; do
  if [ "$target" = core ]; then
    dep='"openclaw":"file:../packs/openclaw-2026.10.5.tgz"'
  else
    dep='"@openclaw/openclaw-atif":"file:../packs/openclaw-openclaw-atif-0.1.4.tgz"'
  fi
  printf '{"private":true,"dependencies":{%s}}\n' "$dep" > "$runtime_dir/$target/package.json"
  (cd "$runtime_dir/$target" && npm install --package-lock-only --ignore-scripts --no-audit --no-fund && npm ci --omit=dev --ignore-scripts --no-audit --no-fund)
done
export OPENCLAW_STATE_DIR="$RUNNER_TEMP/openclaw-runtime-proof/state"
export OPENCLAW_CONFIG_PATH="$RUNNER_TEMP/openclaw-runtime-proof/config.json"
export CODEX_HOME="$RUNNER_TEMP/openclaw-runtime-proof/codex"
export OPENCLAW_NO_RESPAWN=1
export PATH="$runtime_dir/core/node_modules/.bin:$PATH"
openclaw --version
openclaw plugins enable codex --accept-capabilities
openclaw plugins enable diagnostics-otel --accept-capabilities
openclaw plugins inspect codex --runtime --json > "$bundle_dir/codex-runtime-inspect.json"
openclaw plugins inspect diagnostics-otel --runtime --json > "$bundle_dir/diagnostics-runtime-inspect.json"
node - "$bundle_dir" "$runtime_dir" <<'JS'
const fs = require('node:fs');
const [bundle, root] = process.argv.slice(2);
for (const [file,id] of [['codex-runtime-inspect.json','codex'],['diagnostics-runtime-inspect.json','diagnostics-otel']]) {
  const x = JSON.parse(fs.readFileSync(`${bundle}/${file}`));
  if (x.plugin.id !== id || x.plugin.origin !== 'bundled' || x.plugin.status !== 'loaded') throw Error(`Plugin not loaded: ${id}`);
  if (id === 'codex' && !x.plugin.agentHarnessIds.includes('codex')) throw Error('Missing native Codex harness');
}
const info = JSON.parse(fs.readFileSync(`${root}/core/node_modules/openclaw/dist/build-info.json`));
if (info.version !== '2026.10.5' || info.commit !== '1b58cb9a1b4f4353e1add63c8506db4d890962e8') throw Error('OpenClaw source mismatch');
fs.writeFileSync(`${bundle}/runtime.json`, JSON.stringify({openclaw:info.version,source:info.commit,node:'24.19.0',atif:'0.1.4',atif_source:'8104f23723de448ebe5a0c715a047b76391bbd3c',managed_codex:'0.160.0',qualification:'runtime-installed; HF smoke pending'},null,2)+'\n');
JS
node --input-type=module - "$runtime_dir" "$bundle_dir" <<'JS'
import fs from 'node:fs';
import path from 'node:path';
import {pathToFileURL} from 'node:url';
import {execFileSync} from 'node:child_process';
const [root,bundle] = process.argv.slice(2);
const dist = `${root}/core/node_modules/openclaw/dist`;
const mod = await import(pathToFileURL(`${dist}/managed-binary-CpgqPfev.mjs`));
const start = Object.values(mod).find(x => typeof x === 'function' && x.name === 'resolveManagedCodexAppServerStartOptions');
const native = Object.values(mod).find(x => typeof x === 'function' && x.name === 'resolveManagedCodexNativeCommand');
if (!start || !native) throw Error('Missing native managed-binary owners');
const opts = await start({transport:'stdio',commandSource:'managed',managedCommandOrder:'package-only'}, {pluginRoot:`${dist}/extensions/codex`});
const executable = await native(opts.command);
if (!fs.realpathSync(executable).startsWith(fs.realpathSync(root) + path.sep)) throw Error('Managed binary outside runtime');
const version = execFileSync(executable,['--version'],{encoding:'utf8'}).trim();
if (version !== 'codex-cli 0.160.0') throw Error(`Managed binary mismatch: ${version}`);
fs.writeFileSync(`${bundle}/managed-codex-proof.json`,JSON.stringify({version,relative_path:path.relative(root,executable)},null,2));
JS
# Retain both frozen dependency graphs with the deployed runtime.
tar -czf "$bundle_dir/runtime.tar.gz" -C "$runtime_dir" .
sha256sum "$bundle_dir/runtime.tar.gz" | cut -d ' ' -f1 > "$bundle_dir/runtime.tar.gz.sha256"
