#!/bin/sh
set -eu
mkdir -p /logs/verifier
python - <<'PY'
from pathlib import Path

marker = Path("/tmp/code-mode-canary.txt")
passed = marker.is_file() and marker.read_bytes() == b"code-mode-ok"
Path("/logs/verifier/reward.txt").write_text("1" if passed else "0")
PY
