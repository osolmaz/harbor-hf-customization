#!/bin/sh
set -eu
mkdir -p /logs/verifier
python - <<'PY'
from pathlib import Path

marker = Path("/tmp/openclaw-native-canary.txt")
passed = marker.is_file() and marker.read_bytes() == b"openclaw-native-ok"
Path("/logs/verifier/reward.txt").write_text("1" if passed else "0")
PY
