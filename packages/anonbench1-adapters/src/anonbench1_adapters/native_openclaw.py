"""Pinned OpenClaw bundle using Harbor's native installed-agent lifecycle.

This integration is a diagnostic candidate until its remote qualification passes.
It does not claim the dated gateway adapter's qualification receipts.
"""

import hashlib
import json
import re
import shlex
from pathlib import Path
from typing import Any, Literal, override

from harbor.agents.installed.openclaw import OpenClaw, OpenClawOptions
from harbor.environments.base import BaseEnvironment
from pydantic import BaseModel, Field

from anonbench1_adapters.attestation import attest_version
from anonbench1_adapters.openclaw_policy import admit_policy

BUNDLE = Path("/opt/harbor-hf-openclaw/runtime.tar.gz")
RUNTIME = "$HOME/.local/share/harbor/openclaw-runtime"
IDENTITY_CHECK = shlex.quote(
    "const b=require(process.argv[1]); "
    'if(b.version!=="2026.10.5" || '
    'b.commit!=="1b58cb9a1b4f4353e1add63c8506db4d890962e8")process.exit(1)'
)
SELECT = (
    f'export PATH="{RUNTIME}/node/bin:{RUNTIME}/core/node_modules/.bin:$PATH" && '
    'test "$(node --version)" = v24.19.0 && '
    f"node -e {IDENTITY_CHECK} "
    f'"{RUNTIME}/core/node_modules/openclaw/dist/build-info.json"'
)


class ModelPrice(BaseModel):
    """USD per million tokens."""

    input: float = Field(ge=0)
    output: float = Field(ge=0)
    cache_read: float = Field(ge=0)
    cache_write: float = Field(ge=0)


class Anonbench1OpenClawOptions(OpenClawOptions):
    runtime: Literal["openclaw"] = Field(
        description="Explicit native OpenClaw agent runtime."
    )
    code_mode: bool = Field(
        default=True, description="OpenClaw code mode for this model."
    )
    model_api: Literal["openai-completions", "openai-responses"] | None = Field(
        default=None, description="Wire API of a custom endpoint."
    )
    price: ModelPrice | None = Field(
        default=None, description="Prices for cost reporting."
    )
    context_window: int | None = Field(default=None, ge=1)
    max_output_tokens: int | None = Field(default=None, ge=1)


class Anonbench1OpenClaw(OpenClaw):
    """Leave task execution, export, timeouts and cancellation to pinned Harbor."""

    _SETUP_CLI = "openclaw setup --baseline --workspace . --skip-bootstrap"
    options_model = Anonbench1OpenClawOptions
    options: Anonbench1OpenClawOptions

    def __init__(self, *args: Any, version: str = "2026.10.5", **kwargs: Any) -> None:  # noqa: ANN401
        if version != "2026.10.5":
            raise ValueError("OpenClaw requires version 2026.10.5")
        super().__init__(*args, version=version, **kwargs)

    @override
    def get_version_command(self) -> str:
        return f"{SELECT} && openclaw --version"

    @override
    async def install(self, environment: BaseEnvironment) -> None:
        expected = BUNDLE.with_suffix(".gz.sha256").read_text().strip()
        with BUNDLE.open("rb") as stream:
            actual = hashlib.file_digest(stream, "sha256").hexdigest()
        if re.fullmatch(r"[0-9a-f]{64}", expected) is None or actual != expected:
            raise ValueError("OpenClaw runtime archive checksum mismatch")
        remote = "/tmp/harbor-openclaw-runtime.tar.gz"
        await environment.upload_file(BUNDLE, remote)
        await self.exec_as_agent(
            environment,
            command=(
                f"printf '%s  %s\n' {expected} {remote} | sha256sum -c - && "
                f'mkdir -p "{RUNTIME}" && '
                f'tar -xzf {remote} -C "{RUNTIME}" && '
                f'ln -s "{RUNTIME}/exporter" '
                '"$HOME/.local/share/harbor/openclaw-atif" && '
                f"{SELECT} && openclaw --version"
            ),
            timeout_sec=self._install_exec_timeout_sec,
        )
        location = await self.exec_as_agent(
            environment,
            command=f'printf "%s" "{RUNTIME}/core/catalog-diagnostic"',
        )
        self._catalog_path = location.stdout.strip()
        if not self._catalog_path.startswith("/") or "\n" in self._catalog_path:
            raise ValueError("OpenClaw diagnostic directory must be absolute")
        await environment.upload_dir(
            Path(__file__).with_name("openclaw_catalog"), self._catalog_path
        )
        metadata = json.loads(BUNDLE.with_name("runtime.json").read_text())
        (self.logs_dir / "runtime-bundle.json").write_text(
            json.dumps(metadata, indent=2)
        )

    @override
    async def setup(self, environment: BaseEnvironment) -> None:
        await super().setup(environment)
        await attest_version(self, environment, "openclaw")

    @override
    async def exec_as_agent(
        self,
        environment: BaseEnvironment,
        command: str,
        env: dict[str, str] | None = None,
        cwd: str | None = None,
        timeout_sec: int | None = None,
    ) -> Any:  # noqa: ANN401 -- Harbor execution result and kwargs.
        prefix = ". ~/.nvm/nvm.sh && nvm use 24 >/dev/null && "
        if command.startswith(prefix + "openclaw agent --local --json "):
            await self._capture_policy(environment, env or {})
        if command.startswith(prefix):
            command = f"{SELECT} && {command[len(prefix) :]}"
        return await super().exec_as_agent(
            environment, command=command, env=env, cwd=cwd, timeout_sec=timeout_sec
        )

    async def _capture_policy(
        self, environment: BaseEnvironment, env: dict[str, str]
    ) -> None:
        state = env["OPENCLAW_STATE_DIR"]
        current = await super().exec_as_agent(environment, "pwd -P", env=env)
        workspace = current.stdout.strip()
        catalog = f"{self._CONTAINER_LOGS_AGENT}/openclaw-catalog.json"
        policy = f"{self._CONTAINER_LOGS_AGENT}/openclaw-thinking-policy.json"
        command = shlex.join(
            [
                "openclaw",
                "anonbench1-catalog",
                "--agent-dir",
                state + "/agents/main/agent",
                "--workspace-dir",
                workspace,
                "--output",
                catalog,
                "--thinking-model",
                str(self.model_name),
                "--thinking-runtime",
                "openclaw",
                "--thinking-output",
                policy,
            ]
        )
        await super().exec_as_agent(
            environment,
            command=f"{SELECT} && rm -f -- {catalog} {policy} && {command}",
            env=env,
            timeout_sec=self._CAPTURE_TIMEOUT_SEC,
        )
        catalog_path = self.logs_dir / "openclaw-catalog.json"
        policy_path = self.logs_dir / "openclaw-thinking-policy.json"
        await environment.download_file(catalog, catalog_path)
        await environment.download_file(policy, policy_path)
        binding = admit_policy(
            json.loads(policy_path.read_text()),
            catalog_path.read_bytes(),
            str(self.model_name),
            self.options.model_api,
            self.options.thinking,
        )
        schema_check = shlex.quote(
            "const {DatabaseSync}=require('node:sqlite'); "
            "const db=new DatabaseSync(process.argv[1],{readOnly:true}); "
            "const version=db.prepare('PRAGMA user_version').get().user_version; "
            "db.close(); if(version!==20)throw Error('Unexpected state schema'); "
            "console.log(version);"
        )
        schema = await super().exec_as_agent(
            environment,
            command=f"{SELECT} && node -e {schema_check} "
            + shlex.quote(state + "/state/openclaw.sqlite"),
            env=env,
            timeout_sec=self._IO_TIMEOUT_SEC,
        )
        binding["state_schema_version"] = int(schema.stdout.strip())
        (self.logs_dir / "openclaw-policy-binding.json").write_text(
            json.dumps(binding, indent=2) + "\n"
        )

    @override
    def _build_full_openclaw_config(self) -> dict[str, Any]:
        cfg = super()._build_full_openclaw_config()
        if getattr(self, "_catalog_path", None):
            plugins = cfg.setdefault("plugins", {})
            plugins.setdefault("load", {}).setdefault("paths", []).append(
                self._catalog_path
            )
            plugins.setdefault("entries", {})["anonbench1-catalog"] = {"enabled": True}
            if plugins.get("allow") and "anonbench1-catalog" not in plugins["allow"]:
                plugins["allow"].append("anonbench1-catalog")
        assert self.model_name is not None
        provider, model_id = self.model_name.split("/", 1)
        defaults = cfg.setdefault("agents", {}).setdefault("defaults", {})
        defaults["skipBootstrap"] = True
        defaults["model"] = {"primary": self.model_name}
        defaults["utilityModel"] = self.model_name
        defaults["pdfModel"] = {"primary": self.model_name}
        defaults["imageModel"] = {"primary": self.model_name}
        agent_models = defaults.setdefault("models", {})
        agent_models[self.model_name] = {
            **agent_models.get(self.model_name, {}),
            "agentRuntime": {"id": self.options.runtime},
            "codeMode": self.options.code_mode,
        }
        endpoint = cfg.get("models", {}).get("providers", {}).get(provider)
        if isinstance(endpoint, dict) and endpoint.get("baseUrl"):
            self._register_endpoint_model(endpoint, model_id)
        return cfg

    def _register_endpoint_model(self, endpoint: dict[str, Any], model_id: str) -> None:
        if self.options.model_api is None:
            raise ValueError("A custom endpoint needs the model_api argument")
        endpoint["api"] = self.options.model_api
        entry: dict[str, Any] = {
            "id": model_id,
            "name": model_id,
            "reasoning": self.options.thinking not in (None, "off"),
            "compat": {"supportsUsageInStreaming": True},
        }
        if self.options.context_window is not None:
            entry["contextWindow"] = self.options.context_window
        if self.options.max_output_tokens is not None:
            entry["maxTokens"] = self.options.max_output_tokens
        if self.options.price is not None:
            price = self.options.price
            entry["cost"] = {
                "input": price.input,
                "output": price.output,
                "cacheRead": price.cache_read,
                "cacheWrite": price.cache_write,
            }
        others = [
            row
            for row in endpoint.get("models", [])
            if row.get("id") not in (model_id, self.model_name)
        ]
        endpoint["models"] = [entry, *others]
