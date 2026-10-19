"""Add shared resource and configured-effort observations to native Codex.

Unbound installation and native commands, sessions, conversion and cleanup remain
inherited. An explicit retained_runtime binding replaces installation with strict
admission and rechecks the task view before entering the native run.
The options model admits that binding through Harbor's native CLI preflight.
"""

from harbor.agents.installed.codex import Codex, CodexOptions
from pydantic import Field, StrictStr
from anonbench1_adapters.retained_codex import RetainedCodex
from anonbench1_adapters.standalone_observations import StandaloneObservations


class ObservedCodexOptions(CodexOptions):
    retained_runtime: dict[str, StrictStr] | None = Field(
        default=None,
        description="Retained archive/manifest paths and manifest SHA-256 for replay admission.",
    )


class ObservedCodex(StandaloneObservations, Codex):
    options_model = ObservedCodexOptions

    def __init__(self, *args, retained_runtime=None, **kwargs):
        super().__init__(*args, **kwargs)
        self._retained_runtime = (
            RetainedCodex(retained_runtime, version=self._version)
            if retained_runtime is not None else None
        )

    async def install(self, environment):
        if self._retained_runtime is None:
            return await super().install(environment)
        await self._retained_runtime.admit(self, environment, phase="setup")

    async def run(self, instruction, environment, context):
        if self._retained_runtime is not None:
            await self._retained_runtime.admit(self, environment, phase="before_run")
        return await super().run(instruction, environment, context)

    async def acp_install(self, environment):
        if self._retained_runtime is not None:
            raise RuntimeError("Retained Codex does not admit ACP installation")
        return await super().acp_install(environment)
