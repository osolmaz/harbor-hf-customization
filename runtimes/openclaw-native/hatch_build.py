"""Bundle the reviewed Linux x64 OpenClaw payload in the runtime wheel."""

from pathlib import Path
from typing import override

from hatchling.builders.config import BuilderConfig
from hatchling.builders.hooks.plugin.interface import BuildHookInterface


class CustomBuildHook(BuildHookInterface[BuilderConfig]):
    @override
    def initialize(self, version: str, build_data: dict[str, object]) -> None:
        if version == "editable":
            return
        payload = Path(self.root) / "payload"
        required = (
            payload / "bin/node",
            payload / "lib/node_modules/npm/bin/npm-cli.js",
            payload / "openclaw.tgz",
            payload / "package-lock.json",
            payload / "provenance.json",
        )
        if not all(path.is_file() for path in required):
            raise RuntimeError("Build the pinned Linux x64 payload before the wheel")
        build_data["force_include"] = {str(payload): "harbor_openclaw_native/payload"}
        build_data["pure_python"] = False
        build_data["tag"] = "py3-none-linux_x86_64"
