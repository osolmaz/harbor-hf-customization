"""No-inference smoke test for the packaged OpenClaw payload."""

import json
import subprocess
from pathlib import Path

from harbor_openclaw_native.runtime import OPENCLAW_VERSION, SOURCE_COMMIT, payload_root

payload = payload_root()
provenance = json.loads((payload / "provenance.json").read_text())
assert provenance == {
    "nodeVersion": "24.18.0",
    "openclawVersion": OPENCLAW_VERSION,
    "sourceCommit": SOURCE_COMMIT,
}
node = payload / "bin/node"
npm = payload / "lib/node_modules/npm/bin/npm-cli.js"
assert node.is_file() and npm.is_file()
with subprocess.Popen(
    [str(node), "--version"], stdout=subprocess.PIPE, text=True
) as process:
    stdout, _ = process.communicate(timeout=10)
    assert process.returncode == 0 and stdout.strip() == "v24.18.0"
package = json.loads((payload / "package.json").read_text())
assert package == {
    "private": True,
    "dependencies": {"openclaw": "file:openclaw.tgz"},
}
lock = json.loads((payload / "package-lock.json").read_text())
assert lock["lockfileVersion"] == 3
assert Path(payload / "openclaw.tgz").stat().st_size > 1_000_000
