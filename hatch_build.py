"""Mark the bundled official Linux x64 runtime with its platform tag."""

from pathlib import Path
from typing import override

from hatchling.builders.config import BuilderConfig
from hatchling.builders.hooks.plugin.interface import BuildHookInterface


class CustomBuildHook(BuildHookInterface[BuilderConfig]):
    @override
    def initialize(self, version: str, build_data: dict[str, object]) -> None:
        if version == "editable":
            return
        if not (Path(self.root) / "payload/bin/node").is_file():
            raise RuntimeError("Build the pinned Linux x64 payload before the wheel")
        build_data["force_include"] = {
            str(Path(self.root) / "payload"): "harbor_pi_code_mode/payload"
        }
        build_data["pure_python"] = False
        build_data["tag"] = "py3-none-linux_x86_64"
