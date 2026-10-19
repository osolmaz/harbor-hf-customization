"""Native adapters with strict version selection and setup attestation."""

from typing import Any, override

from harbor.environments.base import BaseEnvironment

from anonbench1_adapters.anonbench1_pi import Anonbench1Pi as _CopiedPi
from anonbench1_adapters.anonbench1_pins import require_version
from anonbench1_adapters.attestation import attest_version
from anonbench1_adapters.hf_connection import claude_environment
from anonbench1_adapters.observed_claude_code import ObservedClaudeCode
from anonbench1_adapters.observed_codex import ObservedCodex
from anonbench1_adapters.pricing import (
    QuotedClaudeCodeOptions,
    QuotedCodexOptions,
    register_quoted_price,
)


class Anonbench1Pi(_CopiedPi):
    """Pi with the reviewed runtime and setup attestation."""

    def __init__(self, *args: Any, version: str = "1.3.0", **kwargs: Any) -> None:  # noqa: ANN401 -- Harbor constructor kwargs vary by adapter.
        require_version(version, "1.3.0")
        super().__init__(*args, version=version, **kwargs)

    @override
    async def setup(self, environment: BaseEnvironment) -> None:
        await super().setup(environment)
        await attest_version(self, environment, "pi")


class Anonbench1Codex(ObservedCodex):
    """Native Codex with resource observations and setup attestation."""

    options_model = QuotedCodexOptions
    options: QuotedCodexOptions

    def __init__(self, *args: Any, version: str = "0.163.0", **kwargs: Any) -> None:  # noqa: ANN401 -- Harbor constructor kwargs vary by adapter.
        require_version(version, "0.163.0")
        super().__init__(*args, version=version, **kwargs)
        register_quoted_price(self.model_name, self.options.price, self.logs_dir)

    @override
    async def setup(self, environment: BaseEnvironment) -> None:
        await super().setup(environment)
        await attest_version(self, environment, "codex")


class Anonbench1ClaudeCode(ObservedClaudeCode):
    """Native Claude Code with resource observations and setup attestation."""

    options_model = QuotedClaudeCodeOptions
    options: QuotedClaudeCodeOptions

    def __init__(self, *args: Any, version: str = "2.1.292", **kwargs: Any) -> None:  # noqa: ANN401 -- Harbor constructor kwargs vary by adapter.
        require_version(version, "2.1.292")
        source_env = kwargs.get("extra_env") or {}
        model_name = kwargs.get("model_name") or ""
        kwargs["extra_env"] = claude_environment(source_env, model_name)
        if source_env.get("OPENAI_BASE_URL") == "https://router.huggingface.co/v1":
            kwargs["model_name"] = model_name.removeprefix("openai/")
        super().__init__(*args, version=version, **kwargs)
        register_quoted_price(self.model_name, self.options.price, self.logs_dir)

    @override
    async def setup(self, environment: BaseEnvironment) -> None:
        await super().setup(environment)
        await attest_version(self, environment, "claude-code")
