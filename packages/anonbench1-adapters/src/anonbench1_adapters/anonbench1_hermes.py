"""Anonbench1's Hermes adapter, built on Harbor's adapter at the pinned commit.

This dated copy downloads the installer by full SHA, passes --commit, and checks
the installed source SHA, launcher, package version and exact Node at runtime.
It never changes an existing checkout to repair a mismatch.

Harbor's adapter needs five changes for Hermes v2026.9.24:

- Install with the installer from the pinned tag. Harbor downloads it from main, and that
  installer needs a pm/ package that this tag does not have.
- Check the version with `hermes --version`. This release removed `hermes version`.
- Send a custom endpoint chat completions through Hermes's custom provider. Harbor's
  adapter selects the openai-api provider, which uses the Responses API. On that route,
  Hermes recorded 0 tokens in the smoke test; whether the router sent no usage or Hermes
  did not read it was not checked.
- Preserve Hermes session counters separately from observed provider subtotals.
  Standard token fields use only native provider observations, with partial scope
  and unknown cost. Session and provider counters must not be spliced together.
- Install retries a few times. One install on 2026-10-05 failed because curl could not
  connect to GitHub. Only the install retries; a failed agent run is never repeated.

For NO_NETWORK tasks the serial qualification caller binds the exact native
Trial before setup. Admission verifies existing stock host/task runtimes;
only the host runs inference, while task tools retain the Harbor environment.
Run waits for task, host process-group and IPC finalization, then requires a
native generation/session-lineage export before writing the real trajectory.
The diagnostic route keeps auxiliary usage and totals unknown. Scored offline
execution remains blocked until filesystem, HTTP media and large-file transport
contracts are qualified. Online scored binding retains its native lifecycle. Reasoning
mismatches retain the native result and classify it without altering its reward.
Both routes require one typed provider effort and the private support binding.

Code mode is Hermes's `execute_code` tool. It is part of Hermes's default `hermes-cli`
toolset, which this adapter keeps, so it is on unless the job config turns it off.
"""

import asyncio
import json
import math
import shlex
import shutil
import time
from pathlib import Path
from typing import Any, Literal, override

import yaml
from anonbench1_adapters.anonbench1_pins import (
    HERMES_INSTALL_DIR,
    HERMES_SHA,
    HERMES_TAG,
    HERMES_VERSION,
    NODE_INSTALL,
    NODE_SELECT,
    require_version,
)
from harbor.agents.installed.base import NonZeroAgentExitCodeError, with_prompt_template
from harbor.agents.installed.hermes import Hermes, HermesOptions
from harbor.environments.base import BaseEnvironment
from harbor.models.agent.context import AgentContext
from harbor.models.task.config import NetworkMode
from harbor.trial.hooks import TrialEvent
from anonbench1_adapters.hermes_harbor.reasoning import (
    ProviderEffort,
    ReasoningSupport,
    native_reasoning,
    research_evidence_qualified,
)
from anonbench1_adapters.hermes_source import (
    HostHermesRuntime,
    TaskHermesRuntime,
    UpstreamHermesSource,
    file_sha256,
    require_digest,
    verify_source,
)
from anonbench1_adapters.offline_hermes_transport import HarborHermesTransport, settle_owned
from pydantic import BaseModel, ConfigDict, Field
from anonbench1_adapters.resource_telemetry import begin_resource_observation, observe_resources, publish_resources

INSTALLER = "https://raw.githubusercontent.com/NousResearch/hermes-agent/{ref}/scripts/install.sh"
HERMES_HOME = "/tmp/hermes"
SESSION_LOG = "/logs/agent/hermes-session.jsonl"
USAGE_LOG = "/logs/agent/hermes-usage.json"
USAGE_FIELDS = ("input_tokens", "cache_read_tokens", "cache_write_tokens", "output_tokens")
HERMES_COMMAND = (
    f'env -u PYTHONPATH -u PYTHONHOME "{HERMES_INSTALL_DIR}/venv/bin/python" '
    f'"{HERMES_INSTALL_DIR}/hermes"'
)
# Runs with Hermes's own Python, next to the hermes launcher in its venv, because the
# task image may have no Python. A failure leaves no usage file, and tokens stay unknown.
DUMP_USAGE = f"""
export PATH="$HOME/.local/bin:$PATH"
py="{HERMES_INSTALL_DIR}/venv/bin/python"
"$py" - > {USAGE_LOG}.tmp << 'PYEOF' && mv {USAGE_LOG}.tmp {USAGE_LOG} || rm -f {USAGE_LOG}.tmp
import json, sqlite3
con = sqlite3.connect("file:{HERMES_HOME}/state.db?mode=ro", uri=True)
con.row_factory = sqlite3.Row
rows = con.execute(
    "SELECT session_id, task, model, billing_provider, SUM(api_call_count) AS api_calls, "
    "SUM(input_tokens) AS input_tokens, SUM(cache_read_tokens) AS cache_read_tokens, "
    "SUM(cache_write_tokens) AS cache_write_tokens, SUM(output_tokens) AS output_tokens "
    "FROM session_model_usage GROUP BY session_id, task, model, billing_provider"
)
print(json.dumps([dict(row) for row in rows]))
PYEOF
"""
API_MODES = {"openai-completions": "chat_completions", "openai-responses": "codex_responses"}
INSTALL_ATTEMPTS = 3
INSTALL_RETRY_DELAY_SEC = 15


class ModelPrice(BaseModel):
    """USD per million tokens."""

    input: float = Field(ge=0)
    output: float = Field(ge=0)
    cache_read: float = Field(ge=0)
    cache_write: float = Field(ge=0)


class Anonbench1HermesOptions(HermesOptions):
    reasoning_effort: ProviderEffort
    reasoning_support: ReasoningSupport
    code_mode: bool = Field(default=True, description="Keep Hermes's execute_code tool.")
    model_api: Literal["openai-completions", "openai-responses"] = Field(
        description="Wire API of the custom endpoint."
    )
    price: ModelPrice | None = Field(default=None, description="Prices for cost reporting.")


class HermesQualificationInputs(BaseModel):
    """Explicit private runtime inputs; none are inferred from ambient installations."""

    model_config = ConfigDict(extra="forbid")
    host: HostHermesRuntime
    task: TaskHermesRuntime
    task_cwd: str
    tool_config_path: Path
    tool_config_sha256: str
    native_tool_names: list[str]
    executables: dict[str, dict[str, str]]
    qualified_tools: list[str]


def finite_allowance(value) -> float:
    if type(value) not in (float, int) or not math.isfinite(value) or value <= 0:
        raise ValueError("Qualification requires a finite positive resolved Trial allowance")
    return float(value)


def require_offline_qualification(mode: str) -> None:
    if mode != "qualification":
        raise RuntimeError(
            "Stock Hermes NO_NETWORK permits only non-scoring qualification: "
            "live task filesystem views, HTTP media acquisition and 50 MiB file transport "
            "remain unqualified"
        )


class Anonbench1Hermes(Hermes):
    options_model = Anonbench1HermesOptions
    options: Anonbench1HermesOptions

    @property
    @override
    def extra_env(self) -> dict[str, str]:
        # Trial consumes this before setup knows the task network policy.
        # The online branch explicitly restores the existing scoped overlay.
        return {}

    def bind_trial(
        self, trial, inputs: HermesQualificationInputs, task_config_sha256: str,
        *, qualification_only=False,
    ) -> None:
        """Called by the serial qualification consumer after public Trial.create()."""
        if qualification_only is not True:
            raise ValueError("Only explicit non-scoring qualification is admitted")
        self._bind_execution_trial(trial, inputs, task_config_sha256, "qualification")

    def bind_scored_trial(self, trial, inputs: HermesQualificationInputs, task_config_sha256: str) -> None:
        """Separate scored entry; completion still requires native research evidence."""
        self._bind_execution_trial(trial, inputs, task_config_sha256, "scored")

    def _bind_execution_trial(self, trial, inputs, task_config_sha256, mode) -> None:
        from harbor.trial.single_step import SingleStepTrial

        if mode not in ("qualification", "scored"):
            raise ValueError("An explicit supported execution mode is required")
        self.options.reasoning_support.admit(self.model_name.split("/", 1)[1], self.options.reasoning_effort)
        if mode == "scored" and (trial.config.verifier.disable or trial.config.install_only):
            raise ValueError("Scored binding requires the native verifier and agent run")
        if hasattr(self, "_qualification_trial") or trial.agent is not self:
            raise ValueError("Qualification must bind the exact unbound Trial agent")
        if type(trial) is not SingleStepTrial or trial.task.has_steps:
            raise ValueError("Only explicitly admitted single-step Trials can be qualified")
        if trial.config.source_trial is not None or trial.config.user_agent is not None or trial.config.job_id is not None:
            raise ValueError("Qualification is not a regrade, user-agent bridge or Job execution route")
        require_digest(task_config_sha256, 64)
        if file_sha256(trial.task.paths.config_path) != task_config_sha256:
            raise ValueError("Task configuration differs from the admitted single-step input")
        finite_allowance(trial.agent_setup_timeout_sec)
        offline = trial.agent_environment.network_policy.network_mode == NetworkMode.NO_NETWORK
        if offline:
            require_offline_qualification(mode)
            finite_allowance(trial.agent_timeout_sec)
        require_version(self._version, HERMES_TAG)
        if not inputs.task_cwd.startswith("/") or "\0" in inputs.task_cwd:
            raise ValueError("Qualification requires an explicit absolute task cwd")
        require_digest(inputs.tool_config_sha256, 64)
        if not inputs.tool_config_path.is_absolute():
            raise ValueError("Qualification tool configuration needs an explicit host input path")
        self._qualification_trial = trial
        self._execution_mode = mode
        self._scored_evidence_validated = False
        self._qualification_only = mode == "qualification"
        self._offline_completion = None
        self._qualification_inputs = inputs
        self._setup_deadline = self._run_deadline = None
        self._offline_transport = None

        if offline:
            async def agent_start(event):
                if event.trial_id != trial.id or self._run_deadline is not None:
                    raise RuntimeError("Qualification run identity changed or dispatch was repeated")
                # AGENT_START precedes Harbor's own wait_for. This clock is
                # deliberately conservative, not its inaccessible internal deadline.
                self._run_deadline = asyncio.get_running_loop().time() + finite_allowance(trial.agent_timeout_sec)

            trial.add_hook(TrialEvent.AGENT_START, agent_start)

    def _remaining(self, phase: str) -> float:
        deadline = getattr(self, "_" + phase + "_deadline", None)
        if deadline is None:
            raise RuntimeError("Qualification phase was not admitted by its bound Trial")
        remaining = deadline - asyncio.get_running_loop().time()
        if remaining <= 0:
            raise TimeoutError("Conservative qualification phase allowance expired")
        return remaining

    def _require_execution_route(self, environment: BaseEnvironment) -> None:
        if environment.network_policy.network_mode == NetworkMode.NO_NETWORK:
            trial = getattr(self, "_qualification_trial", None)
            if (trial is None or trial.agent is not self or trial.agent_environment is not environment
                    or getattr(self, "_execution_mode", None) not in ("qualification", "scored")):
                # False/absent qualification flags never create a scored binding.
                raise RuntimeError("NO_NETWORK Hermes requires the bound single-step qualification Trial")
            require_offline_qualification(self._execution_mode)

    @override
    async def setup(self, environment: BaseEnvironment) -> None:
        self._require_execution_route(environment)
        if environment.network_policy.network_mode == NetworkMode.NO_NETWORK:
            if self._setup_deadline is not None:
                raise RuntimeError("Qualification setup cannot be replayed")
            self._setup_deadline = asyncio.get_running_loop().time() + finite_allowance(
                self._qualification_trial.agent_setup_timeout_sec,
            )
            try:
                async with asyncio.timeout_at(self._setup_deadline):
                    # Native installed-agent setup still owns its directories,
                    # version lifecycle and call to our admission-only install.
                    await super().setup(environment)
            except BaseException:
                if self._offline_transport is not None:
                    await settle_owned(asyncio.create_task(self._offline_transport.close()))
                raise
            return
        with environment.scoped_exec_env(self._extra_env):
            await super().setup(environment)

    @override
    def get_version_command(self) -> str | None:
        return f"{self._runtime_guard()} && {HERMES_COMMAND} --version"

    @override
    async def install(self, environment: BaseEnvironment) -> None:
        self._require_execution_route(environment)
        if environment.network_policy.network_mode == NetworkMode.NO_NETWORK:
            await self._admit_offline(environment)
            return
        with environment.scoped_exec_env(self._extra_env):
            await self._install_online(environment)

    async def _install_online(self, environment: BaseEnvironment) -> None:
        require_version(self._version, HERMES_TAG)
        for attempt in range(1, INSTALL_ATTEMPTS + 1):
            try:
                await self._install_once(environment)
                return
            except NonZeroAgentExitCodeError:
                if attempt == INSTALL_ATTEMPTS:
                    raise
                self.logger.warning(f"Hermes install attempt {attempt} failed; retrying")
                await asyncio.sleep(INSTALL_RETRY_DELAY_SEC * attempt)

    async def _install_once(self, environment: BaseEnvironment) -> None:
        assert self._version is not None
        await self.ensure_system_dependencies(environment, ("curl", "git", "ripgrep", "xz"))
        await self.exec_as_agent(
            environment,
            command=(
                "set -euo pipefail; "
                f"{NODE_INSTALL} && curl -fsSL {INSTALLER.format(ref=HERMES_SHA)} "
                f"| bash -s -- --skip-setup --branch {HERMES_TAG} --commit {HERMES_SHA} "
                f'--dir "{HERMES_INSTALL_DIR}" && '
                'export PATH="$HOME/.local/bin:$PATH" && '
                f"mkdir -p {HERMES_HOME}/sessions {HERMES_HOME}/skills {HERMES_HOME}/memories && "
                f"{self._runtime_guard()} && {HERMES_COMMAND} --version"
            ),
        )

    def _runtime_guard(self) -> str:
        expression = "from hermes_cli import __version__; print(__version__)"
        return (
            f"{NODE_SELECT} && "
            f'test "$(git -C "{HERMES_INSTALL_DIR}" rev-parse HEAD)" = {HERMES_SHA} && '
            f'git -C "{HERMES_INSTALL_DIR}" diff --quiet HEAD -- && '
            f'test "$("{HERMES_INSTALL_DIR}/venv/bin/python" -I -c '
            f'{shlex.quote(expression)})" = {HERMES_VERSION}'
        )

    def build_config(self) -> dict[str, Any]:
        """Harbor's Hermes config, with the model on a custom chat endpoint."""
        if not self.model_name or "/" not in self.model_name:
            raise ValueError("Model name must be in the format provider/model_name")
        if not self.options.code_mode:
            raise ValueError(
                "This adapter keeps Hermes's default tools, which include execute_code"
            )
        base_url = self._get_env("OPENAI_BASE_URL")
        if not base_url:
            raise ValueError("Set OPENAI_BASE_URL in the agent env")
        config = yaml.safe_load(self._build_config_yaml(self.model_name, self.options.max_turns))
        config.pop("provider", None)
        config["model"] = {
            "provider": "custom",
            "default": self.model_name.split("/", 1)[1],
            "base_url": base_url.rstrip("/"),
            "key_env": "OPENAI_API_KEY",
            "api_mode": API_MODES[self.options.model_api],
        }
        requested = self.options.reasoning_effort
        support = self.options.reasoning_support
        support.admit(config["model"]["default"], requested)
        config.setdefault("agent", {})["reasoning_effort"] = native_reasoning(requested)
        config["plugins"] = {"enabled": ["harbor-task"], "entries": {"harbor-task": {"settings": {"reasoning": {
            "trial_id": self.session_id,
            "requested_provider_effort": requested,
            "support": support.model_dump(),
            "execution_mode": getattr(self, "_execution_mode", "qualification"),
            "evidence_path": "/logs/agent/hermes-reasoning.json",
        }}}}}
        return config

    @with_prompt_template
    @override
    @observe_resources
    async def run(
        self, instruction: str, environment: BaseEnvironment, context: AgentContext
    ) -> None:
        self._require_execution_route(environment)
        if environment.network_policy.network_mode == NetworkMode.NO_NETWORK:
            await self._run_offline(instruction, environment, context)
            return
        with environment.scoped_exec_env(self._extra_env):
            await self._run_online(instruction, environment, context)

    async def _admit_offline(self, environment: BaseEnvironment) -> None:
        self._remaining("setup")
        inputs = self._qualification_inputs
        if self.mcp_servers:
            raise RuntimeError("Offline Hermes MCP is not qualified; preserve the online MCP route")
        identity = UpstreamHermesSource.load(Path(inputs.host.source_identity), inputs.host.source_identity_sha256)
        if inputs.task.source_identity_sha256 != inputs.host.source_identity_sha256:
            raise ValueError("Host and task must use the same upstream source identity")
        verify_source(Path(inputs.host.source), identity)
        if file_sha256(Path(inputs.host.python)) != inputs.host.python_sha256:
            raise ValueError("Host interpreter bytes differ from their admitted identity")
        if file_sha256(inputs.tool_config_path) != inputs.tool_config_sha256:
            raise ValueError("Task tool profile differs from its admitted bytes")
        profile = yaml.safe_load(inputs.tool_config_path.read_text())
        if not isinstance(profile, dict) or any(k in profile for k in ("model", "auxiliary", "provider", "providers", "mcp_servers", "plugins")):
            raise ValueError("Task tool profile must exclude inference, MCP and additional plugin configuration")
        if profile.get("terminal", {}).get("env") != "local":
            raise ValueError("Task tool profile must select task-local terminal execution")
        config = self.build_config()
        if self.options.toolsets:
            config["toolsets"] = [name.strip() for name in self.options.toolsets.split(",")]
        for key, value in profile.items():
            if key in config and key != "terminal" and value != config[key]:
                raise ValueError("Task tool profile changes the campaign's native profile")
            if key != "terminal":
                config[key] = value
        config["terminal"] = {**config["terminal"], **profile["terminal"],
                              "env": "harbor_task", "backend": "harbor_task", "cwd": inputs.task_cwd}
        entry = config["plugins"]["entries"]["harbor-task"]
        entry["allow_tool_override"] = True
        entry["settings"]["reasoning"]["evidence_path"] = str(self.logs_dir / "hermes-reasoning.json")
        host = self.logs_dir / "hermes-host"
        host.mkdir(mode=0o700)
        state = host / "state"
        state.mkdir(mode=0o700)
        adapter = Path(__file__).parent
        shutil.copytree(adapter / "hermes_harbor", state / "plugins" / "harbor-task")
        (state / "config.yaml").write_text(yaml.safe_dump(config, sort_keys=False))
        (state / "config.yaml").chmod(0o600)
        self._host_home, self._host_config = host, config
        if self.skills_dir:
            # Harbor owns the injected skill input. Read it through Harbor,
            # never interpret a task skill path as a controller filesystem path.
            await environment.download_dir(source_dir=self.skills_dir, target_dir=state / "skills")
        self._remaining("setup")
        transport = HarborHermesTransport(environment, inputs.task, identity)
        self._offline_transport = transport
        await transport.prepare()
        self._remaining("setup")
        check = await environment.exec(command="cd -- " + shlex.quote(inputs.task_cwd) + " && pwd -P", timeout_sec=min(10, self._remaining("setup")))
        if check.return_code or (check.stdout or "").strip() != inputs.task_cwd:
            raise ValueError("Admitted task cwd is not the actual canonical task directory")
        self._task_tool_config = transport.root + "-tools.yaml"
        await environment.upload_file(source_path=inputs.tool_config_path, target_path=self._task_tool_config)
        self._remaining("setup")

    async def _run_offline(self, instruction, environment, context):
        from dataclasses import asdict

        transport = self._offline_transport
        if transport is None or not transport.accepting:
            raise RuntimeError("Offline qualification requires its completed native setup")
        inputs = self._qualification_inputs
        context.metadata = {**(context.metadata or {}), "qualification_only": self._qualification_only,
                            "deadline_basis": "conservative setup-entry/AGENT_START; native cancellation authoritative"}
        try:
            if self._resume:
                raise RuntimeError("This campaign adapter runs single-step tasks only; native resume remains unsupported")
            begin_resource_observation()
            async with asyncio.timeout_at(self._run_deadline):
                await transport.prepare_native_tools(cwd=inputs.task_cwd, configuration={
                    "config_path": self._task_tool_config, "config_sha256": inputs.tool_config_sha256,
                    "names": inputs.native_tool_names, "executables": inputs.executables,
                    "toolsets": self._host_config["toolsets"],
                    "timeout": self._remaining("run"),
                }, deadline=self._run_deadline)
                self._remaining("run")
                host = self._host_home
                env = {"HOME": str(host), "HERMES_HOME": str(host / "state"),
                       "PATH": str(Path(inputs.host.node).parent) + ":/usr/bin:/bin",
                       "PYTHONDONTWRITEBYTECODE": "1", "TERMINAL_ENV": "harbor_task",
                       "HERMES_DISABLE_LAZY_INSTALLS": "1",
                       "TERMINAL_CWD": inputs.task_cwd}
                key = self._host_config["model"]["key_env"]
                value = self._get_env(key)
                if not value:
                    raise ValueError("Host provider binding is absent")
                env[key] = value
                completion = await transport.run_host(
                    [inputs.host.python, "-I", "-B", str(Path(__file__).with_name("offline_hermes_host.py"))],
                    env=env, cwd=host, log_path=self.logs_dir / "hermes.txt",
                    qualification_only=self._qualification_only,
                    execution_mode=self._execution_mode,
                    bootstrap={
                        "generation": transport.generation, "source": inputs.host.source,
                        "source_identity": inputs.host.source_identity,
                        "source_identity_sha256": inputs.host.source_identity_sha256,
                        "host_runtime": asdict(inputs.host), "config": self._host_config,
                        "session_id": self.session_id, "task_id": str(self._qualification_trial.id),
                        "cwd": inputs.task_cwd, "instruction": instruction,
                        # Convert the loop's remaining allowance to the host
                        # process's monotonic clock without extending it.
                        "deadline": time.monotonic() + self._remaining("run"),
                        "qualified_tools": inputs.qualified_tools,
                    },
                )
        finally:
            await settle_owned(asyncio.create_task(transport.close()))
        if transport.fatal or transport.accepting:
            raise RuntimeError("Offline finalization closed with a fatal or unsettled generation")
        self._offline_completion = completion
        # Trial skips population when metadata already makes AgentContext
        # nonempty. Populate here before returning successful execution.
        self.populate_context_post_run(context)

    async def _run_online(
        self, instruction: str, environment: BaseEnvironment, context: AgentContext
    ) -> None:
        require_version(self._version, HERMES_TAG)
        await self.exec_as_agent(
            environment,
            command='export PATH="$HOME/.local/bin:$PATH" && ' + self._runtime_guard(),
        )
        if self._resume:
            raise RuntimeError("This adapter runs single-step tasks only")
        config_yaml = yaml.safe_dump(self.build_config(), sort_keys=False)
        await environment.upload_dir(
            Path(__file__).parent / "hermes_harbor", HERMES_HOME + "/plugins/harbor-task",
        )
        env = {
            "HERMES_HOME": HERMES_HOME,
            "TERMINAL_ENV": "local",
            "HARBOR_INSTRUCTION": instruction,
        }
        await self.exec_as_agent(
            environment,
            command=f"mkdir -p {HERMES_HOME} && cat > {HERMES_HOME}/config.yaml << 'EOF'\n{config_yaml}EOF",
            env=env,
            timeout_sec=10,
        )
        for command in (
            self._build_register_mcp_servers_command(),
            self._build_register_skills_command(),
        ):
            if command:
                await self.exec_as_agent(environment, command=command, env=env, timeout_sec=10)
        flags = self.build_cli_flags()
        run = (
            'export PATH="$HOME/.local/bin:$PATH" && '
            f"{self._runtime_guard()} && "
            f'{HERMES_COMMAND} --yolo chat -q "$HARBOR_INSTRUCTION" -Q {flags} '
            "2>&1 | stdbuf -oL tee /logs/agent/hermes.txt"
        )
        begin_resource_observation()
        try:
            await self.exec_as_agent(environment, command=run, env=env)
        finally:
            await self.exec_as_agent(
                environment,
                command=(
                    'export PATH="$HOME/.local/bin:$PATH" && '
                    f"{HERMES_COMMAND} sessions export {SESSION_LOG} --source oneshot 2>/dev/null || true"
                ),
                env={"HERMES_HOME": HERMES_HOME},
                timeout_sec=30,
            )
            await self.exec_as_agent(environment, command=DUMP_USAGE, timeout_sec=30)

    @override
    @publish_resources
    def populate_context_post_run(self, context: AgentContext) -> None:
        trial = getattr(self, "_qualification_trial", None)
        if trial is not None and trial.agent_environment.network_policy.network_mode == NetworkMode.NO_NETWORK:
            self._populate_offline_context(context)
            return
        super().populate_context_post_run(context)
        context.n_input_tokens = context.n_cache_tokens = context.n_output_tokens = None
        context.cost_usd = None
        usage_path = self.logs_dir / "hermes-usage.json"
        if not usage_path.exists():
            self.logger.warning("Hermes usage table was not exported; tokens are unknown")
            self._populate_reasoning_context(context)
            return
        try:
            rows = json.loads(usage_path.read_text())
        except json.JSONDecodeError:
            self.logger.warning("Hermes usage table export is not valid JSON; tokens are unknown")
            self._populate_reasoning_context(context)
            return
        record_session_usage(context, rows)
        self._populate_reasoning_context(context)

    def _populate_reasoning_context(self, context, session_ids=None):
        context.n_input_tokens = context.n_output_tokens = context.n_cache_tokens = None
        context.cost_usd = None
        # Partial provider counters cannot be backfilled from conversation steps.
        context.model_usage = {}
        try:
            evidence = json.loads((self.logs_dir / "hermes-reasoning.json").read_text())
        except FileNotFoundError:
            # Absence is an observation gap, not evidence of failed execution.
            # The requested value is configuration; no native fact is invented.
            evidence = {
                "trial_id": self.session_id,
                "requested_provider_effort": self.options.reasoning_effort,
                "requested_provider_effort_source": "campaign.Trial.options",
                "evidence_source": "missing_native_reasoning_file",
                "native_selected_effort": None, "native_selected_config": None,
                "native_selected_effort_source": "missing_native_reasoning_file",
                "selected_provider_effort": None,
                "selected_provider_effort_source": "missing_native_reasoning_file",
                "effective_wire_effort": None,
                "effective_wire_effort_source": "missing_native_reasoning_file",
                "provider_effective_effort": None,
                "provider_effective_effort_source": "missing_native_reasoning_file",
                "provider_acceptance": None, "configuration_qualified": None,
                "requested_level_status": "unknown", "provider_usage": None,
                "reasoning_tokens": None, "usage_status": "unknown",
                "usage_complete": False, "auxiliary_usage_complete": False,
                "research_qualified": False,
            }
        except (OSError, ValueError) as exc:
            raise RuntimeError("Native reasoning observations are missing or malformed") from exc
        else:
            if (
                not isinstance(evidence, dict)
                or evidence.get("trial_id") != self.session_id
                or evidence.get("support_source") != self.options.reasoning_support.source
                or evidence.get("requested_provider_effort") != self.options.reasoning_effort
                or not isinstance(evidence.get("session_ids"), list) or not evidence["session_ids"]
                or (session_ids is not None and not set(evidence["session_ids"]).issubset(session_ids))
            ):
                raise RuntimeError("Native reasoning observations do not belong to this Trial")
        usage = evidence.get("provider_usage") or {}
        for field, source in (("n_input_tokens", "input_tokens"),
                              ("n_output_tokens", "output_tokens"),
                              ("n_cache_tokens", "cache_read_tokens")):
            value = usage.get(source)
            if type(value) is int and value >= 0:
                setattr(context, field, value)
        accounting = {
            "source": evidence.get("usage_evidence_source", "not_observed"),
            "scope": "observed_native_llm_execution_responses_across_observed_sessions",
            "status": "observed_subtotal" if any(
                value is not None for value in (context.n_input_tokens, context.n_output_tokens)
            ) else "unknown",
            "complete_call_accounting": False,
            "auxiliary_usage_complete": False,
            "cache_tokens_included_in_input": True,
            "reasoning_tokens_included_in_output": True,
            "cost_status": "unknown",
        }
        self._scored_evidence_validated = (
            getattr(self, "_execution_mode", None) == "scored"
            and research_evidence_qualified(evidence, self.options.reasoning_effort)
        )
        context.metadata = {
            **(context.metadata or {}), "hermes_reasoning": evidence,
            "execution_mode": getattr(self, "_execution_mode", "qualification"),
            "qualification_only": getattr(self, "_execution_mode", None) != "scored",
            "research_qualified": self._scored_evidence_validated,
            "requested_level_qualified": self._scored_evidence_validated,
            "hermes_accounting": accounting,
        }
        trajectory_path = self.logs_dir / "trajectory.json"
        if trajectory_path.exists():
            trajectory = json.loads(trajectory_path.read_text())
            metrics = trajectory.get("final_metrics")
            if isinstance(metrics, dict):
                metrics.update(total_prompt_tokens=context.n_input_tokens,
                               total_completion_tokens=context.n_output_tokens,
                               total_cached_tokens=context.n_cache_tokens, total_cost_usd=None)
                metrics["extra"] = {**(metrics.get("extra") or {}), "hermes_reasoning": evidence,
                                    "hermes_accounting": accounting}
            trajectory_path.write_text(json.dumps(trajectory, indent=2))

    def _populate_offline_context(self, context: AgentContext) -> None:
        # Route selection is bound to Trial, never inferred from an optional
        # export marker. Missing/corrupt offline exports cannot reach legacy
        # accounting, even during Harbor's post-failure output recovery.
        context.n_input_tokens = context.n_cache_tokens = context.n_output_tokens = None
        context.cost_usd = context.model_usage = None
        transport = self._offline_transport
        completion = self._offline_completion
        if (
            self._execution_mode not in ("qualification", "scored") or transport is None or transport.fatal
            or transport.accepting or not isinstance(completion, dict)
            or completion != {"generation": transport.generation, "confirmed_closed": True,
                              "qualification_only": self._qualification_only, "execution_mode": self._execution_mode,
                              "usage_status": "unknown", "research_qualified": False}
        ):
            raise RuntimeError("Offline export requires confirmed runner/task settlement")
        try:
            text = (self.logs_dir / "hermes-session.jsonl").read_text()
            native = json.loads(text)
        except (OSError, ValueError) as exc:
            raise RuntimeError("Native offline session export is missing or malformed") from exc
        if not isinstance(native, dict):
            # Invalid persisted peer data retains the export RuntimeError contract.
            raise RuntimeError("Native offline session export must be an object")  # noqa: TRY004
        execution = native.get("harbor_execution")
        expected = {
            "generation": transport.generation, "session_id": self.session_id,
            "task_id": str(self._qualification_trial.id), "qualification_only": self._qualification_only,
            "execution_mode": self._execution_mode,
            "main_completed": True, "fatal": False,
            "auxiliary_usage_complete": False, "usage_status": "unknown", "research_qualified": False,
        }
        if (not isinstance(execution, dict)
            or type(execution.get("token_queue_flushed")) is not bool or any(
            type(execution.get(key)) is not type(value) or execution.get(key) != value
            for key, value in expected.items()
        )):
            raise RuntimeError("Native offline export does not match this completed qualification")
        # A bounded accounting flush can time out after model completion.
        # Process/task settlement is checked independently above; keep the fact.
        expected["token_queue_flushed"] = execution["token_queue_flushed"]
        segments = native.get("segments")
        if not isinstance(segments, list) or not segments or any(not isinstance(s, dict) for s in segments):
            raise RuntimeError("Native offline export has no session lineage")
        ids = [segment.get("id") for segment in segments]
        if (
            any(not isinstance(sid, str) or not sid for sid in ids)
            or len(set(ids)) != len(ids) or native.get("lineage_session_ids") != ids
            or self.session_id not in ids or native.get("id") != ids[-1]
            or any(not isinstance(segment.get("messages"), list) for segment in segments)
        ):
            raise RuntimeError("Native offline export session lineage does not match this run")
        # Compression segments own the root trajectory. Reasoning also observes
        # delegated sessions; only persisted parent links establish their Trial
        # ownership, not their presence in export_all or their requested names.
        sessions = native.get("native_sessions")
        if not isinstance(sessions, list) or not sessions:
            raise RuntimeError("Native offline export session ownership is missing or malformed")
        parents, children = {}, {}
        for session in sessions:
            if not isinstance(session, dict):
                # Invalid persisted peer data retains the export RuntimeError contract.
                raise RuntimeError("Native offline export session ownership is missing or malformed")  # noqa: TRY004
            sid, parent = session.get("id"), session.get("parent_session_id")
            if (
                not isinstance(sid, str) or not sid or sid in parents
                or (parent is not None and (not isinstance(parent, str) or not parent))
            ):
                raise RuntimeError("Native offline export session ownership is missing or malformed")
            parents[sid] = parent
            children.setdefault(parent, []).append(sid)
        if any(sid not in parents or parents[sid] != segment.get("parent_session_id")
               for sid, segment in zip(ids, segments, strict=True)):
            raise RuntimeError("Native offline export session ownership disagrees with its lineage")
        owned_sessions = set(ids)
        pending = list(ids)
        while pending:
            for sid in children.get(pending.pop(), []):
                if sid not in owned_sessions:
                    owned_sessions.add(sid)
                    pending.append(sid)
        messages = [message for segment in segments for message in segment["messages"]]
        if (
            not messages or any(not isinstance(message, dict) for message in messages)
            or native.get("messages") != messages
            or type(native.get("message_count")) is not int or native["message_count"] != len(messages)
            or not any(message.get("role") == "assistant" and message.get("content") for message in messages)
        ):
            raise RuntimeError("Native offline export has no complete conversation")
        trajectory = self._convert_hermes_session_to_atif(text, ids[-1])
        if trajectory is None or not any(step.source == "agent" for step in trajectory.steps):
            raise RuntimeError("Native offline export did not produce an agent trajectory")
        metrics = trajectory.final_metrics
        if metrics is not None:
            metrics.total_prompt_tokens = metrics.total_completion_tokens = None
            metrics.total_cached_tokens = metrics.total_cost_usd = None
            metrics.extra = {**(metrics.extra or {}), "hermes_execution": expected}
        trajectory.notes = (
            (trajectory.notes or "") + " Auxiliary usage and totals are unknown; this trajectory does not establish research qualification."
        ).strip()
        (self.logs_dir / "trajectory.json").write_text(json.dumps(trajectory.to_json_dict(), indent=2))
        context.metadata = {**(context.metadata or {}), "qualification_only": self._qualification_only,
                            "execution_mode": self._execution_mode, "hermes_execution": expected}
        self._populate_reasoning_context(context, owned_sessions)


def record_session_usage(context: AgentContext, rows: list[dict[str, Any]]) -> None:
    """Retain session counters independently of provider-response accounting."""
    def subtotal(group, field):
        values = [row.get(field) for row in group]
        return (sum(values) if values and all(type(v) is int and v >= 0 for v in values)
                else None)

    tasks = {row.get("task") or "main" for row in rows}
    context.metadata = {
        **(context.metadata or {}),
        "hermes_usage": {
            "source": "hermes.session_model_usage",
            "scope": "exported_session_model_task_rows",
            "complete": False,
            "sessions": len({row.get("session_id") for row in rows}),
            "api_calls": subtotal(rows, "api_calls"),
            "rows": rows,
            "by_task": {
                task: {field: subtotal([row for row in rows if (row.get("task") or "main") == task], field)
                       for field in USAGE_FIELDS}
                for task in sorted(tasks)
            },
        },
    }
