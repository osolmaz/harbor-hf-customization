"""Anonbench1's Pi adapter, built on Harbor's adapter at the pinned commit.

This dated copy reuses the existing Pi adapter. It additionally enforces Pi 1.3.0
and Node 26.11.1 at install and runtime. MCP uses Pi 1.1's built-in extension,
not Harbor's incompatible legacy extension. The run override preserves Harbor's
direct model connection, skill/max-turn registration, session offsets, output and
trajectory accounting. One native-session accounting pass supplies result, model,
and trajectory totals across current-run branches, including aborted turns. Required servers are checked before the first model turn.
Pi's package supports stdio and streamable HTTP; legacy SSE is not reinterpreted.

NO_NETWORK setup/run selects a host SDK with qualified pre-staged runtimes and
remote task tools. Trial receives no extra_env overlay; the unchanged online
branch restores it only after policy dispatch. The prompt template renders once
before either run branch. Offline execution never installs over the task network.

The supported stream hook records selective observations in the existing session.
Post-run metadata separates execution from research qualification: returned-model
mismatches fail qualification, and SDK-only zero usage remains unknown. Capture
is partial; requested identity and normalized totals are not upgraded to proof.

The existing run-specific behavior is retained:

- Code mode is on unless the job config turns it off. Pi ships its `codemode` tool
  inactive, so the adapter lists Pi's default tools plus `codemode` with `--tools`.
- A custom endpoint's model entry carries the job config's prices and limits, so Pi
  reports cost, not only tokens.
- Explicit model input modalities preserve reviewed image capability in custom
  definitions; omitted modalities retain Pi's native text-only default.
- Task MCP tools are included in the explicit tool allowlist so native direct
  exposure remains active and callable, including from codemode.
- Install retries a few times. On 2026-10-06, 3 of the first 14 trials of a job failed
  at install: npm lost its registry connection, or nvm could not fetch the Node index.
  Only the install retries; a failed agent run is never repeated.
"""

import asyncio
import json
import re
import shlex
import tempfile
from pathlib import Path
from typing import Any, Literal, override

from anonbench1_adapters.anonbench1_pins import NODE_INSTALL, PinnedNodeRuntime, npm_guard, require_version
from harbor.agents.installed.base import NonZeroAgentExitCodeError, with_prompt_template
from harbor.agents.installed.pi import (
    _CUSTOM_PROVIDER,
    _PI_CONFIG_DIR_ENV,
    _REMOTE_PI_CONFIG_DIR,
    Pi,
    PiOptions,
)
from harbor.agents.model_connection import ResolvedModelConnection
from harbor.environments.base import BaseEnvironment
from harbor.models.agent.context import AgentContext, ModelUsage
from harbor.models.task.config import NetworkMode
from harbor.utils.trajectory_utils import format_trajectory_json
from anonbench1_adapters.offline_pi import OfflinePiTools
from pydantic import BaseModel, Field, field_validator
from anonbench1_adapters.resource_telemetry import begin_resource_observation, observe_resources, publish_resources

DEFAULT_TOOLS = ("read", "bash", "edit", "write")
INSTALL_ATTEMPTS = 3
INSTALL_RETRY_DELAY_SEC = 15


class ModelPrice(BaseModel):
    """USD per million tokens."""

    input: float = Field(ge=0)
    output: float = Field(ge=0)
    cache_read: float = Field(ge=0)
    cache_write: float = Field(ge=0)


class OfflineRuntime(BaseModel):
    """Reviewed local staging paths; no package installation or auto-discovery."""

    host_node: Path
    host_pi: Path
    task_node: Path
    task_pi: Path


class Anonbench1PiOptions(PiOptions):
    offline_runtime: OfflineRuntime | None = None
    code_mode: bool = Field(default=True, description="Add Pi's codemode tool.")
    price: ModelPrice | None = Field(default=None, description="Prices for cost reporting.")
    context_window: int | None = Field(default=None, ge=1)
    max_output_tokens: int | None = Field(default=None, ge=1)
    model_input: list[Literal["text", "image"]] | None = Field(
        default=None, min_length=1, max_length=2,
        description="Explicitly admitted input modalities for a custom endpoint model.",
    )

    @field_validator("model_input")
    @classmethod
    def unique_model_input(cls, value):
        if value is not None and len(value) != len(set(value)):
            raise ValueError("model_input must not repeat a modality")
        return value


class Anonbench1Pi(PinnedNodeRuntime, Pi):
    package = "@earendil-works/pi-coding-agent"
    pinned_version = "1.3.0"
    executable = "pi"
    options_model = Anonbench1PiOptions
    options: Anonbench1PiOptions

    _offline_run = False

    @property
    @override
    def extra_env(self) -> dict[str, str]:
        # Trial reads this before setup knows the task policy. Only the online
        # branch may inject the original environment into task processes.
        return {}

    @staticmethod
    def _is_offline(environment: BaseEnvironment) -> bool:
        return environment.network_policy.network_mode == NetworkMode.NO_NETWORK

    @override
    async def setup(self, environment: BaseEnvironment) -> None:
        if self._is_offline(environment):
            await self.install(environment)
        else:
            with environment.scoped_exec_env(self._extra_env):
                await super().setup(environment)

    @override
    async def install(self, environment: BaseEnvironment) -> None:
        require_version(self._version, self.pinned_version)
        self._offline_run = self._is_offline(environment)
        if self._offline_run:
            await self._install_offline(environment)
        else:
            with environment.scoped_exec_env(self._extra_env):
                await self._install_online(environment)

    async def _install_online(self, environment: BaseEnvironment) -> None:
        require_version(self._version, self.pinned_version)
        self._build_mcp_config()
        for attempt in range(1, INSTALL_ATTEMPTS + 1):
            try:
                await self.ensure_system_dependencies(environment, ("curl",))
                await self.exec_as_agent(
                    environment,
                    command=(
                        f"{NODE_INSTALL} && "
                        f"npm install -g --ignore-scripts {self.package}@{self.pinned_version} && "
                        f"{npm_guard(self.package, self.pinned_version)} && "
                        f'test "$(pi --version)" = {self.pinned_version}'
                    ),
                )
                return
            except NonZeroAgentExitCodeError:
                if attempt == INSTALL_ATTEMPTS:
                    raise
                self.logger.warning(f"Pi install attempt {attempt} failed; retrying")
                await asyncio.sleep(INSTALL_RETRY_DELAY_SEC * attempt)

    @override
    def _build_mcp_config(self) -> dict[str, Any]:
        servers: dict[str, Any] = {}
        namespaces: set[str] = set()
        for server in self.mcp_servers:
            namespace = server.name.replace("-", "_")
            if not re.fullmatch(r"[A-Za-z0-9_-]+", server.name) or namespace in namespaces:
                raise ValueError("Pi MCP server names must be valid and have unique namespaces")
            namespaces.add(namespace)
            if server.transport == "stdio":
                config = {"type": "stdio", "command": server.command, "args": list(server.args)}
            elif server.transport == "streamable-http":
                config = {"type": "http", "url": server.url}
            else:
                raise ValueError(
                    "Pi 1.3.0 built-in MCP does not support legacy SSE; "
                    "a verified stdio or streamable HTTP task contract is required"
                )
            servers[server.name] = {**config, "enabled": True, "exposure": "direct"}
        return {"mcpServers": servers}

    async def _check_required_mcp(self, environment: BaseEnvironment, env: dict[str, str]) -> None:
        result = await self.exec_as_agent(
            environment,
            command=f"{npm_guard(self.package, self.pinned_version)} && pi mcp list --json",
            env=env,
        )
        try:
            report = json.loads(result.stdout)
        except (json.JSONDecodeError, TypeError) as exc:
            raise RuntimeError("Pi MCP readiness did not return JSON") from exc
        rows = report.get("servers") if isinstance(report, dict) else None
        if not isinstance(rows, list) or report.get("errors") != []:
            raise RuntimeError("Pi MCP readiness returned configuration errors")
        for server in self.mcp_servers:
            matches = [
                row for row in rows if isinstance(row, dict) and row.get("name") == server.name
            ]
            if len(matches) != 1:
                raise RuntimeError("A required MCP server is missing or duplicated")
            row = matches[0]
            tools = row.get("tools")
            if (
                row.get("state") != "connected"
                or row.get("enabled") is not True
                or row.get("exposure") != "direct"
                or not isinstance(tools, list)
                or not tools
                or not all(isinstance(tool, str) and tool for tool in tools)
                or row.get("toolExposure")
            ):
                raise RuntimeError("A required MCP server did not expose its tools")

    @override
    @with_prompt_template
    @observe_resources
    async def run(
        self, instruction: str, environment: BaseEnvironment, context: AgentContext
    ) -> None:
        require_version(self._version, self.pinned_version)
        self._offline_run = self._is_offline(environment)
        if self._offline_run:
            await self._run_offline(instruction, environment, context)
        else:
            with environment.scoped_exec_env(self._extra_env):
                await self._run_online(instruction, environment, context)

    async def _run_online(
        self, instruction: str, environment: BaseEnvironment, context: AgentContext
    ) -> None:
        require_version(self._version, self.pinned_version)
        self._build_mcp_config()
        await self.exec_as_agent(environment, command=npm_guard(self.package, self.pinned_version))
        if not self.model_name or "/" not in self.model_name:
            raise ValueError("Model name must be in the format provider/model_name")
        provider, model_id = self.model_name.split("/", 1)
        access = self.model_connection
        provider = access.provider or provider
        env = dict(access.env)
        if provider == "anthropic" and (token := self._get_env("ANTHROPIC_OAUTH_TOKEN")):
            env["ANTHROPIC_OAUTH_TOKEN"] = token

        models_json = self._build_custom_models_json(access, model_id)
        if models_json is not None or self.mcp_servers:
            env[_PI_CONFIG_DIR_ENV] = _REMOTE_PI_CONFIG_DIR.as_posix()
        if models_json is not None:
            await self._write_custom_models_json(environment, models_json)
            provider = _CUSTOM_PROVIDER

        await self._ensure_config_dir(environment)
        evidence_file = Path(__file__).with_name("campaign_evidence.mjs")
        evidence_path = (_REMOTE_PI_CONFIG_DIR / evidence_file.name).as_posix()
        await self._upload_config_text(
            environment,
            content=evidence_file.read_text(),
            remote_path=evidence_path,
            filename=evidence_file.name,
        )
        extensions = [evidence_path]
        if self.mcp_servers:
            await self._write_mcp_config(environment)
            await self._check_required_mcp(environment, env)
            extensions.append("builtin:mcp")
        if self.options.max_turns is not None:
            extensions.append(
                await self._write_max_turns_extension(environment, self.options.max_turns)
            )
        extension_args = "".join(f"--extension {shlex.quote(path)} " for path in extensions)
        skills_command = self._build_register_skills_command()
        if skills_command:
            await self.exec_as_agent(environment, command=skills_command)
        self._session_usage_start = (
            await self._session_positions(environment) if self._resume else {}
        )
        output_path = self.environment_logs_dir / self._OUTPUT_FILENAME
        session_dir = self.environment_logs_dir / self._SESSIONS_DIRECTORY
        begin_resource_observation()
        await self.exec_as_agent(
            environment,
            command=(
                f"{npm_guard(self.package, self.pinned_version)} && "
                f"mkdir -p {shlex.quote(str(session_dir))} && pi --print --mode json "
                f"--session-dir {shlex.quote(str(session_dir))} "
                f"{'--continue ' if self._resume else ''}"
                f"--provider {shlex.quote(provider)} --model {shlex.quote(model_id)} "
                f"{extension_args}{self.build_cli_flags()} {shlex.quote(instruction)} "
                f'2>&1 </dev/null | grep -v \'"type":"message_update"\' '
                f"| stdbuf -oL tee {shlex.quote(str(output_path))}"
            ),
            env=env,
        )

    @override
    @publish_resources
    def populate_context_post_run(self, context: AgentContext) -> None:
        super().populate_context_post_run(context)
        # Harbor otherwise backfills this from only the visible conversation branch.
        context.model_usage = getattr(self, "_accounting_model_usage", {})
        if hasattr(self, "_accounting_cost"):
            context.cost_usd = self._accounting_cost
        expected = (self.model_name or "").split("/", 1)[-1]
        observed: set[str] = set()
        evidence: list[dict[str, Any]] = []
        reasoning_evidence: list[dict[str, Any]] = []
        sessions = list((self.logs_dir / self._SESSIONS_DIRECTORY).glob("*.jsonl"))
        if sessions:
            session = max(sessions, key=lambda path: (path.stat().st_mtime_ns, path.name))
            copied = self._session_usage_start.get(session.name, 0)
            for index, line in enumerate(session.read_text().splitlines()):
                try:
                    entry = json.loads(line)
                except json.JSONDecodeError:
                    continue
                if not isinstance(entry, dict):
                    continue
                data = entry.get("data")
                if entry.get("customType") == "anonbench1-provider-evidence" and isinstance(
                    data, dict
                ):
                    reasoning_evidence.append(data)
                if index < copied:
                    continue
                if entry.get("customType") == "anonbench1-provider-evidence" and isinstance(
                    data, dict
                ):
                    evidence.append(data)
                    for row in data.get("models", []):
                        if isinstance(row, dict) and isinstance(row.get("observed_model"), str):
                            observed.add(row["observed_model"])
                message = entry.get("message")
                if isinstance(message, dict) and isinstance(message.get("responseModel"), str):
                    observed.add(message["responseModel"])

        mismatch = any(model != expected for model in observed)
        inactive = self.options.code_mode and any(
            row.get("codemode_active") is False for row in evidence
        )
        reasoning = self._reasoning_qualification(evidence, expected)
        qualification = {
            "status": "failed"
            if mismatch or inactive
            else (
                "unqualified"
                if reasoning["requested_level_qualification"] == "unqualified"
                else "unverified"
            ),
            "requested_model": expected,
            "observed_models": sorted(observed),
            "model_mismatch": mismatch,
            "provider_identity": "unverified; configured provider is requested metadata",
            "codemode": "inactive-observed"
            if inactive
            else (
                "active-observed"
                if evidence and all(row.get("codemode_active") is True for row in evidence)
                else "unverified"
            ),
            "usage": "partial-raw-observations"
            if any(row.get("usage") for row in evidence)
            else "unverified",
            "reasoning": reasoning,
            "complete_call_accounting": False,
            "execution_completion_is_research_qualification": False,
        }
        accounting = {
            "source": "pi.native_session.usage",
            "scope": "current_run_all_session_branches_including_recorded_auxiliary_calls",
            "status": "observed_subtotal",
            "complete_call_accounting": False,
            "cache_tokens_included_in_input": True,
            "reasoning_tokens_included_in_output": True,
        }
        context.metadata = {**(context.metadata or {}), "pi_research_evidence": qualification,
                            "pi_usage": accounting}
        # Pi initializes usage to zero even when the provider omits usage. Raw zero
        # observations remain in the session; they do not establish aggregate zeros.
        unknown_zero = all(
            value in (None, 0)
            for value in (
                context.n_input_tokens,
                context.n_output_tokens,
                context.n_cache_tokens,
                context.cost_usd,
            )
        )
        if unknown_zero:
            context.n_input_tokens = context.n_output_tokens = context.n_cache_tokens = None
            context.cost_usd = None
            context.model_usage = {}
            accounting["status"] = "unknown"
        trajectory_path = self.logs_dir / self._TRAJECTORY_FILENAME
        if trajectory_path.exists():
            trajectory = json.loads(trajectory_path.read_text())
            final = trajectory.setdefault("final_metrics", {})
            final.setdefault("extra", {})["pi_research_evidence"] = qualification
            final["extra"]["pi_usage"] = accounting
            if unknown_zero:
                final["extra"]["model_usage"] = {}
                for field in (
                    "total_prompt_tokens",
                    "total_completion_tokens",
                    "total_cached_tokens",
                    "total_cost_usd",
                ):
                    final[field] = None
                for field in ("cache_write_tokens", "pi_total_tokens"):
                    final["extra"][field] = None
            current_reasoning: list[int | None] = []
            for step in trajectory.get("steps", []):
                metrics = step.get("metrics")
                reasoning = None
                if metrics:
                    extra = metrics.setdefault("extra", {})
                    normalized = extra.get("reasoning_tokens")
                    if (
                        isinstance(normalized, int)
                        and not isinstance(normalized, bool)
                        and normalized > 0
                    ):
                        reasoning = normalized
                    elif step.get("llm_call_count") == 1:
                        identity = step.get("extra") or {}
                        response_id = identity.get("response_id")
                        message_id = identity.get("pi_event_id")
                        rows = [
                            row
                            for row in reasoning_evidence
                            if message_id and row.get("message_entry_id") == message_id
                        ]
                        counts = {
                            count["tokens"]
                            for row in rows
                            for count in row.get("reasoning_usage", [])
                            if isinstance(count, dict)
                            and response_id
                            and count.get("response_id") == response_id
                            and type(count.get("tokens")) is int
                            and count["tokens"] >= 0
                        }
                        if (
                            rows
                            and not any(row.get("truncated") for row in rows)
                            and counts == {0}
                        ):
                            reasoning = 0
                    if step.get("llm_call_count") != 1:
                        reasoning = None
                    extra["reasoning_tokens"] = reasoning
                    if all(
                        metrics.get(field) in (None, 0)
                        for field in (
                            "prompt_tokens",
                            "completion_tokens",
                            "cached_tokens",
                            "cost_usd",
                        )
                    ):
                        if reasoning is None:
                            step["metrics"] = None
                        else:
                            for field in (
                                "prompt_tokens",
                                "completion_tokens",
                                "cached_tokens",
                                "cost_usd",
                            ):
                                metrics[field] = None
                            extra["cache_write_tokens"] = extra["pi_total_tokens"] = None
                if step.get("llm_call_count") and not step.get("is_copied_context"):
                    # Merged/nested and auxiliary calls lack complete raw call attribution.
                    current_reasoning.append(reasoning if step["llm_call_count"] == 1 else None)
            final["extra"]["reasoning_tokens"] = (
                sum(current_reasoning)
                if current_reasoning
                and None not in current_reasoning
                and not final["extra"].get("pi_auxiliary_usage")
                and not getattr(self, "_accounting_hidden_usage", False)
                else None
            )
            trajectory_path.write_text(format_trajectory_json(trajectory))

    def _reasoning_qualification(self, evidence, expected_model):
        requested = self.options.thinking
        api = self.options.model_api
        expected_wire = (
            ("none" if requested == "off" else requested)
            if api in ("openai-completions", "openai-responses")
            else None
        )
        native = sorted(
            {
                row["thinking_level"]
                for row in evidence
                if isinstance(row.get("thinking_level"), str)
            }
        )
        wire = [
            item
            for row in evidence
            for item in (row.get("provider_request_efforts") or [])
            if isinstance(item, dict)
        ]
        provider = [
            item
            for row in evidence
            for item in (row.get("provider_efforts") or [])
            if isinstance(item, dict) and isinstance(item.get("value"), str)
        ]
        capabilities = [
            row["model_capabilities"]
            for row in evidence
            if isinstance(row.get("model_capabilities"), dict)
        ]
        native_mismatch = (
            any(value != requested for value in native)
            if requested is not None and native
            else None
        )
        wire_mismatch = (
            any(item.get("value") != expected_wire for item in wire)
            if expected_wire is not None and wire
            else None
        )
        provider_mismatch = (
            any(item["value"] != expected_wire for item in provider)
            if expected_wire is not None and provider
            else None
        )
        provider_complete = bool(evidence) and all(
            not row.get("truncated")
            and any(
                isinstance(item, dict) and isinstance(item.get("value"), str)
                for item in (row.get("provider_efforts") or [])
            )
            for row in evidence
        )
        selected_mapping = False
        for model in capabilities:
            if (
                model.get("id") != expected_model
                or model.get("api") != api
                or model.get("reasoning") is not True
            ):
                continue
            levels = model.get("thinking_level_map") or {}
            # Pi defaults these four supported native levels to the same wire spelling.
            mapped = levels.get(
                requested,
                requested if requested in ("minimal", "low", "medium", "high") else None,
            )
            selected_mapping |= expected_wire is not None and mapped == expected_wire
        configuration = (
            "unqualified"
            if native_mismatch or wire_mismatch
            else "qualified"
            if selected_mapping or wire_mismatch is False
            else "unknown"
        )
        return {
            "requested_native_level": requested,
            "requested_provider_effort": expected_wire,
            "native": {
                "values": native or None,
                "evidence_source": "pi.getThinkingLevel" if native else "not_observed",
            },
            "wire": {
                "observations": wire or None,
                "evidence_source": "before_provider_request.payload"
                if wire
                else "not_observed",
            },
            "provider": {
                "observations": provider or None,
                "evidence_source": "provider_stream_event" if provider else "not_observed",
            },
            "model_capabilities": {
                "observations": capabilities or None,
                "evidence_source": "ExtensionContext.model" if capabilities else "not_observed",
            },
            "configuration_qualification": configuration,
            "provider_acceptance": "reported-mismatch"
            if provider_mismatch
            else (
                "reported-match"
                if provider_mismatch is False and provider_complete
                else "unknown"
            ),
            "requested_level_qualification": "unqualified"
            if any((native_mismatch, wire_mismatch, provider_mismatch))
            else "unknown",
            "native_mismatch": native_mismatch,
            "wire_mismatch": wire_mismatch,
            "provider_mismatch": provider_mismatch,
        }

    @override
    def build_cli_flags(self) -> str:
        tools = [*DEFAULT_TOOLS, *(["codemode"] if self.options.code_mode else [])]
        if self.mcp_servers:
            tools.append("mcp__*")
        return " ".join(
            part for part in (super().build_cli_flags(), f"--tools {shlex.quote(','.join(tools))}") if part
        )

    @override
    def _build_custom_models_json(
        self, access: ResolvedModelConnection, model_id: str
    ) -> dict[str, Any] | None:
        models_json = super()._build_custom_models_json(access, model_id)
        if models_json is None:
            if self.options.model_input is not None:
                raise ValueError("model_input requires an explicitly configured custom endpoint")
            return None
        for provider in models_json["providers"].values():
            # pi-ai adds stream_options to non-streaming requests unless the
            # provider compat says otherwise; some routers reject that with 400.
            provider["compat"] = {"supportsUsageInStreaming": False}
            for model in provider["models"]:
                if self.options.model_input is not None:
                    model["input"] = list(self.options.model_input)
                if self.options.thinking == "off" and provider["api"] in (
                    "openai-completions",
                    "openai-responses",
                ):
                    # A non-reasoning row omits effort rather than sending provider none.
                    model["reasoning"] = True
                    model["thinkingLevelMap"] = {"off": "none"}
                if self.options.context_window is not None:
                    model["contextWindow"] = self.options.context_window
                if self.options.max_output_tokens is not None:
                    model["maxTokens"] = self.options.max_output_tokens
                if self.options.price is not None:
                    price = self.options.price
                    model["cost"] = {
                        "input": price.input,
                        "output": price.output,
                        "cacheRead": price.cache_read,
                        "cacheWrite": price.cache_write,
                    }
        return models_json

    def _admit(self, environment: BaseEnvironment) -> None:
        require_version(self._version, self.pinned_version)
        if environment.network_policy.network_mode != NetworkMode.NO_NETWORK:
            raise ValueError("Offline Pi requires the existing NO_NETWORK policy")
        if self.mcp_servers or environment.task_env_config.mcp_servers:
            raise ValueError("Offline Pi has no qualified MCP transport")
        if self.options.offline_runtime is None:
            raise ValueError("NO_NETWORK Pi requires qualified offline_runtime staging inputs")
        for path in self.options.offline_runtime.model_dump().values():
            if not path.is_absolute() or not path.is_dir():
                raise ValueError("Offline Pi requires absolute, pre-staged runtime directories")

    @staticmethod
    def _host_env(runtime: OfflineRuntime, home: Path) -> dict[str, str]:
        return {
            "HOME": str(home),
            "PATH": f"{runtime.host_node / 'bin'}:/usr/bin:/bin",
            "PI_CODING_AGENT_DIR": str(home / "agent"),
            "PI_OFFLINE": "1",
            "LANG": "C.UTF-8",
        }

    async def _install_offline(self, environment: BaseEnvironment) -> None:
        self._admit(environment)
        runtime = self.options.offline_runtime
        with tempfile.TemporaryDirectory(prefix="harbor-pi-pin-") as directory:
            process = await asyncio.create_subprocess_exec(
                str(runtime.host_node / "bin/node"),
                "--input-type=module",
                "-e",
                "if (process.version !== 'v26.11.1') throw Error('Node pin mismatch');"
                "const pi = await import(process.argv[1]);"
                "if (pi.VERSION !== '1.3.0') throw Error('Pi pin mismatch');",
                (
                    runtime.host_pi / "node_modules/@earendil-works/pi-coding-agent/dist/index.js"
                ).as_uri(),
                env=self._host_env(runtime, Path(directory)),
                stdout=asyncio.subprocess.DEVNULL,
                stderr=asyncio.subprocess.DEVNULL,
            )
            try:
                if await process.wait():
                    raise RuntimeError("Offline Pi host runtime pin check failed")
            finally:
                if process.returncode is None:
                    process.terminate()
                    await OfflinePiTools._finish_owned(asyncio.create_task(process.wait()))

    async def _run_offline(
        self, instruction: str, environment: BaseEnvironment, context: AgentContext
    ) -> None:
        await self._install_offline(environment)
        if not self.model_name or "/" not in self.model_name:
            raise ValueError("Model name must be in the format provider/model_name")
        provider, model_id = self.model_name.split("/", 1)
        access = self.model_connection
        provider = access.provider or provider
        models = self._build_custom_models_json(access, model_id)
        if models is not None:
            provider = _CUSTOM_PROVIDER
        result = await environment.exec("pwd -P")
        if result.return_code != 0 or not (result.stdout or "").strip().startswith("/"):
            raise RuntimeError("Offline Pi could not resolve the task working directory")
        task_cwd = result.stdout.strip()
        runtime = self.options.offline_runtime
        sessions = self.logs_dir / self._SESSIONS_DIRECTORY
        sessions.mkdir(parents=True, exist_ok=True)
        self._session_usage_start = (
            {path.name: len(path.read_text().splitlines()) for path in sessions.glob("*.jsonl")}
            if self._resume
            else {}
        )
        transport = OfflinePiTools(
            environment, cwd=task_cwd, node_dir=runtime.task_node, pi_dir=runtime.task_pi
        )
        with tempfile.TemporaryDirectory(prefix="harbor-pi-host-") as directory:
            home = Path(directory)
            agent_dir = home / "agent"
            agent_dir.mkdir(mode=0o700)
            models_path = agent_dir / "models.json"
            models_path.write_text(json.dumps(models or {"providers": {}}))
            models_path.chmod(0o600)
            config_path = home / "run.json"
            config_path.write_text(
                json.dumps(
                    {
                        "entry": str(
                            runtime.host_pi
                            / "node_modules/@earendil-works/pi-coding-agent/dist/index.js"
                        ),
                        "cwd": task_cwd,
                        "agentDir": str(agent_dir),
                        "skillsDir": self.skills_dir,
                        "sessionDir": str(sessions),
                        "resume": self._resume,
                        "provider": provider,
                        "model": model_id,
                        "thinking": self.options.thinking,
                        "codeMode": self.options.code_mode,
                        "maxTurns": self.options.max_turns,
                        "instruction": instruction,
                        "output": str(self.logs_dir / self._OUTPUT_FILENAME),
                    }
                )
            )
            config_path.chmod(0o600)
            env = {**self._extra_env, **access.env, **self._host_env(runtime, home)}
            if provider == "anthropic" and (token := self._get_env("ANTHROPIC_OAUTH_TOKEN")):
                env["ANTHROPIC_OAUTH_TOKEN"] = token
            try:
                begin_resource_observation()
                await transport.start()
                await transport.run_host(
                    [
                        str(runtime.host_node / "bin/node"),
                        str(Path(__file__).with_name("offline_session.mjs")),
                        str(config_path),
                    ],
                    env=env,
                    cwd=home,
                )
            finally:
                await transport.close()
                # Host-native logs feed the inherited converter. Keep Harbor's
                # non-mounted download path from replacing them with stale logs.
                if not environment.capabilities.mounted:
                    await environment.upload_dir(self.logs_dir, str(self.environment_logs_dir))

    @override
    def _accounting_metrics(self, events, copied_until):
        metrics = []
        models = {}
        unpriced = set()
        current_model = self.model_name
        for position, event in events:
            if (event.get("type") == "model_change"
                    and event.get("provider") and event.get("modelId")):
                current_model = f"{event['provider']}/{event['modelId']}"
            observed = super()._accounting_metrics([(position, event)], copied_until)
            # Pi 1.1 records offline auxiliary calls outside conversation messages.
            if self._offline_run and position > copied_until and event.get("type") == "usage":
                metric = self._metrics_from_usage(event.get("usage"))
                if metric is not None:
                    observed.append(metric)
            message = event.get("message") or event
            model = current_model
            if message.get("model"):
                provider = message.get("provider") or (model or "").split("/", 1)[0]
                model = f"{provider}/{message['model']}"
            for metric in observed:
                metrics.append(metric)
                if model:
                    total = models.setdefault(model, ModelUsage())
                    total.n_input_tokens += metric.prompt_tokens or 0
                    total.n_output_tokens += metric.completion_tokens or 0
                    total.n_cache_tokens += metric.cached_tokens or 0
                    if metric.cost_usd is not None:
                        total.cost_usd = (total.cost_usd or 0) + metric.cost_usd
                    elif metric.prompt_tokens or metric.completion_tokens:
                        unpriced.add(model)
        for model in unpriced:
            models[model].cost_usd = None
        self._accounting_model_usage = models
        return metrics

    @override
    def _load_trajectory(self):
        loaded = super()._load_trajectory()
        if loaded is None:
            return None
        trajectory, metrics = loaded
        final = trajectory.final_metrics
        self._accounting_hidden_usage = any(
            getattr(final, target) != sum(getattr(metric, source) or 0 for metric in metrics)
            for target, source in (("total_prompt_tokens", "prompt_tokens"),
                                   ("total_completion_tokens", "completion_tokens"))
        )
        for target, source in (
            ("total_prompt_tokens", "prompt_tokens"),
            ("total_completion_tokens", "completion_tokens"),
            ("total_cached_tokens", "cached_tokens"),
        ):
            setattr(final, target, sum(getattr(metric, source) or 0 for metric in metrics))
        costs = [metric.cost_usd for metric in metrics]
        final.total_cost_usd = sum(costs) if costs and None not in costs else None
        self._accounting_cost = final.total_cost_usd
        for field in ("cache_write_tokens", "pi_total_tokens"):
            final.extra[field] = sum((metric.extra or {}).get(field, 0) for metric in metrics)
        final.extra["model_usage"] = {
            model: usage.model_dump() for model, usage in self._accounting_model_usage.items()
        }
        trajectory.notes = ((trajectory.notes or "") + " Final usage covers all current-run "
                            "session branches, including calls absent from conversation steps.").strip()
        return trajectory, metrics

    @override
    def _convert_session_events_to_trajectory(self, events, copied_until=0):
        trajectory = super()._convert_session_events_to_trajectory(events, copied_until)
        if trajectory is None or not self._offline_run:
            return trajectory
        nested = {}
        for position, event in events:
            message = event.get("message", {})
            if (
                isinstance(message, dict)
                and message.get("role") == "toolResult"
                and "nestedCalls" in message
            ):
                nested[message.get("toolCallId")] = message["nestedCalls"]
            if position > copied_until and event.get("type") == "usage":
                metric = self._metrics_from_usage(event.get("usage"))
                if metric is None:
                    continue
                final = trajectory.final_metrics
                final.extra = final.extra or {}
                final.extra.setdefault("pi_auxiliary_usage", []).append(event)
        if "pi_auxiliary_usage" in (trajectory.final_metrics.extra or {}):
            trajectory.notes = (
                (trajectory.notes or "")
                + " Final totals include current native auxiliary usage records retained in "
                "final_metrics.extra.pi_auxiliary_usage outside conversation steps."
            ).strip()
        for step in trajectory.steps:
            if step.observation:
                for result in step.observation.results:
                    if result.source_call_id in nested:
                        result.extra = {
                            **(result.extra or {}),
                            "pi_nested_calls": nested[result.source_call_id],
                        }
        return trajectory
