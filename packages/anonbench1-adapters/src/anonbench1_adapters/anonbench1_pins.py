"""Install and runtime pins for this run, without changing historical adapters.

Harbor's pinned OpenClaw adapter selects Node 24 in every command. Its Pi
adapter sources nvm's default. Translate only those pinned command prefixes,
then check the actual runtime before executing the inherited command.
"""

import shlex
from typing import Any

NODE_VERSION = "26.11.1"
HERMES_SHA = "f97608f178d1ffeca59860195ab7da295f7c8e5f"
HERMES_TAG = "v2026.9.24"
HERMES_VERSION = "0.21.5"
HERMES_INSTALL_DIR = "$HOME/.local/share/anonbench1-hermes"
NODE_SELECT = (
    'export NVM_DIR="${NVM_DIR:-$HOME/.nvm}" && . "$NVM_DIR/nvm.sh" && '
    f"nvm use {NODE_VERSION} >/dev/null && "
    f'test "$(node --version)" = "v{NODE_VERSION}"'
)
NODE_INSTALL = (
    "set -euo pipefail; "
    "curl -fsSL https://raw.githubusercontent.com/nvm-sh/nvm/v0.40.2/install.sh "
    "| env -u NODE_VERSION bash && "
    'export NVM_DIR="$HOME/.nvm" && . "$NVM_DIR/nvm.sh" && '
    f"nvm install {NODE_VERSION} && nvm alias default {NODE_VERSION} && {NODE_SELECT}"
)


def require_version(actual: str | None, expected: str) -> None:
    if actual != expected:
        raise ValueError(f"This campaign requires version {expected}; got {actual!r}")


def npm_guard(package: str, version: str) -> str:
    expression = "require(process.argv[1]).version"
    return (
        f"{NODE_SELECT} && "
        f'test "$(node -p {shlex.quote(expression)} '
        f'"$(npm root -g)/{package}/package.json")" = {shlex.quote(version)}'
    )


class PinnedNodeRuntime:
    """Keep inherited provider, trajectory, session and code-mode behavior."""

    package: str
    pinned_version: str
    executable: str

    def get_version_command(self) -> str:
        return f"{npm_guard(self.package, self.pinned_version)} && {self.executable} --version"

    async def exec_as_agent(self, environment, command: str, **kwargs: Any) -> Any:
        for prefix in (
            ". ~/.nvm/nvm.sh && nvm use 24 >/dev/null && ",
            ". ~/.nvm/nvm.sh; ",
        ):
            if command.startswith(prefix):
                command = (
                    f"{npm_guard(self.package, self.pinned_version)} && {command[len(prefix) :]}"
                )
                break
        return await super().exec_as_agent(environment, command=command, **kwargs)

    async def run(self, instruction, environment, context) -> None:
        require_version(self._version, self.pinned_version)
        await self.exec_as_agent(environment, command=npm_guard(self.package, self.pinned_version))
        await super().run(instruction, environment, context)
