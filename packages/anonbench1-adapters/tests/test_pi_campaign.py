"""Tests for the Pi campaign adapter: MCP admission, run wiring, evidence."""

import json
from pathlib import Path
from typing import Any
from unittest.mock import AsyncMock

import pytest
from harbor.models.agent.context import AgentContext
from harbor.models.task.config import MCPServerConfig
from test_runtime import offline_env

from anonbench1_adapters import anonbench1_pi
from anonbench1_adapters.anonbench1_pi import Anonbench1Pi

MODEL = "openai/zai-org/GLM-5.3-Flash:fireworks-ai"
ENV = {"OPENAI_BASE_URL": "https://router.huggingface.co/v1", "OPENAI_API_KEY": "k"}


def make_agent(tmp_path: Path, **kwargs: object) -> Anonbench1Pi:
    options = {"version": "1.3.0", "model_api": "openai-completions"}
    return Anonbench1Pi(
        logs_dir=tmp_path,
        model_name=MODEL,
        extra_env=ENV,
        **{**options, **kwargs},  # ty: ignore[invalid-argument-type]
    )


def mock_run_internals(agent: Anonbench1Pi, tmp_path: Path, monkeypatch) -> list[str]:
    agent.ensure_system_dependencies = AsyncMock()
    agent.exec_as_agent = AsyncMock()
    agent._ensure_config_dir = AsyncMock()
    agent._upload_config_text = AsyncMock()
    agent._write_mcp_config = AsyncMock()
    agent._check_required_mcp = AsyncMock()
    agent._write_max_turns_extension = AsyncMock(return_value="max-turns.ts")
    agent._session_positions = AsyncMock(return_value={})
    agent._build_register_skills_command = lambda: None  # ty: ignore[invalid-assignment]
    monkeypatch.setattr(type(agent), "environment_logs_dir", tmp_path, raising=False)
    return [call.kwargs["command"] for call in agent.exec_as_agent.call_args_list]


def mcp_report(
    rows: list[dict[str, object]], errors: list[object] | None = None
) -> str:
    return json.dumps({"servers": rows, "errors": errors if errors is not None else []})


class _ExecResult:
    def __init__(self, stdout: str) -> None:
        self.return_code = 0
        self.stdout = stdout


async def test_check_required_mcp_accepts_a_connected_server(
    tmp_path: Path, monkeypatch
) -> None:
    agent = make_agent(tmp_path)
    agent.mcp_servers = [MCPServerConfig(name="docs", transport="stdio", command="d")]
    seen: dict[str, str] = {}

    async def exec_as_agent(self, environment, command, **kwargs):  # noqa: ANN001, ANN202
        seen["command"] = command
        return _ExecResult(
            mcp_report(
                [
                    {
                        "name": "docs",
                        "state": "connected",
                        "enabled": True,
                        "exposure": "direct",
                        "tools": ["search"],
                    }
                ]
            )
        )

    monkeypatch.setattr(
        anonbench1_pi.OpenClaw if False else Anonbench1Pi,
        "exec_as_agent",
        exec_as_agent,
    )
    await agent._check_required_mcp("env", {})  # ty: ignore[invalid-argument-type]
    assert "pi mcp list --json" in seen["command"]


async def test_check_required_mcp_rejects_a_broken_report(
    tmp_path: Path, monkeypatch
) -> None:
    agent = make_agent(tmp_path)
    agent.mcp_servers = [MCPServerConfig(name="docs", transport="stdio", command="d")]

    async def not_json(self, environment, command, **kwargs):  # noqa: ANN001, ANN202
        return _ExecResult("<html>nope</html>")

    monkeypatch.setattr(Anonbench1Pi, "exec_as_agent", not_json)
    with pytest.raises(RuntimeError, match="did not return JSON"):
        await agent._check_required_mcp("env", {})  # ty: ignore[invalid-argument-type]

    async def with_errors(self, environment, command, **kwargs):  # noqa: ANN001, ANN202
        return _ExecResult(mcp_report([], errors=["boom"]))

    monkeypatch.setattr(Anonbench1Pi, "exec_as_agent", with_errors)
    with pytest.raises(RuntimeError, match="configuration errors"):
        await agent._check_required_mcp("env", {})  # ty: ignore[invalid-argument-type]

    async def missing(self, environment, command, **kwargs):  # noqa: ANN001, ANN202
        return _ExecResult(mcp_report([]))

    monkeypatch.setattr(Anonbench1Pi, "exec_as_agent", missing)
    with pytest.raises(RuntimeError, match="missing or duplicated"):
        await agent._check_required_mcp("env", {})  # ty: ignore[invalid-argument-type]

    async def disconnected(self, environment, command, **kwargs):  # noqa: ANN001, ANN202
        return _ExecResult(
            mcp_report(
                [
                    {
                        "name": "docs",
                        "state": "error",
                        "enabled": True,
                        "exposure": "direct",
                        "tools": ["search"],
                    }
                ]
            )
        )

    monkeypatch.setattr(Anonbench1Pi, "exec_as_agent", disconnected)
    with pytest.raises(RuntimeError, match="did not expose its tools"):
        await agent._check_required_mcp("env", {})  # ty: ignore[invalid-argument-type]


async def test_run_wires_mcp_and_max_turns_extensions(
    tmp_path: Path, monkeypatch
) -> None:
    agent = make_agent(tmp_path, max_turns=7)
    agent.mcp_servers = [
        MCPServerConfig(name="docs", transport="stdio", command="d", args=[])
    ]
    mock_run_internals(agent, tmp_path, monkeypatch)

    async def ready(self, environment, env):  # noqa: ANN001, ANN202
        return None

    monkeypatch.setattr(Anonbench1Pi, "_check_required_mcp", ready)
    await agent.run("task", offline_env(), context=AgentContext())
    commands = [
        call.kwargs["command"]
        for call in agent.exec_as_agent.call_args_list  # ty: ignore[unresolved-attribute]
    ]
    assert any("builtin:mcp" in command for command in commands)
    assert any("max-turns.ts" in command for command in commands)


async def test_run_rejects_a_wrong_version_before_any_command(
    tmp_path: Path, monkeypatch
) -> None:
    agent = make_agent(tmp_path, version="1.0.2")
    mock_run_internals(agent, tmp_path, monkeypatch)
    with pytest.raises(ValueError, match="requires version"):
        await agent.run("task", offline_env(), context=AgentContext())
    assert agent.exec_as_agent.await_count == 0  # ty: ignore[unresolved-attribute]


def test_populate_keeps_recorded_usage_but_still_writes_evidence(
    tmp_path: Path, monkeypatch
) -> None:
    agent = make_agent(tmp_path)
    sessions = tmp_path / "pi" / "sessions"
    sessions.mkdir(parents=True)
    entry = {
        "customType": "anonbench1-provider-evidence",
        "data": {
            "models": [
                {
                    "requested_model": "zai-org/GLM-5.3-Flash:fireworks-ai",
                    "observed_model": "zai-org/GLM-5.3-Flash:fireworks-ai",
                }
            ],
            "codemode_active": True,
            "usage": [{"input_tokens": 5}],
        },
    }
    (sessions / "s1.jsonl").write_text(json.dumps(entry) + "\n")
    trajectory = {"steps": [{"metrics": {"prompt_tokens": 10}}]}
    (tmp_path / "trajectory.json").write_text(json.dumps(trajectory))
    agent._session_usage_start = {}
    monkeypatch.setattr(
        anonbench1_pi.Pi, "populate_context_post_run", lambda self, context: None
    )
    context = _Context()
    agent.populate_context_post_run(context)
    saved = json.loads((tmp_path / "trajectory.json").read_text())
    qualification = saved["final_metrics"]["extra"]["pi_research_evidence"]
    assert qualification["model_mismatch"] is False
    assert qualification["status"] == "unverified"
    assert qualification["codemode"] == "active-observed"
    assert qualification["usage"] == "partial-raw-observations"
    # Recorded usage survives: only all-zero accounting is nulled.
    assert context.n_input_tokens == 7
    evidence = context.metadata or {}
    assert evidence["pi_research_evidence"]["complete_call_accounting"] is False


def test_populate_nulls_all_zero_usage(tmp_path: Path, monkeypatch) -> None:
    agent = make_agent(tmp_path)
    agent._session_usage_start = {}
    monkeypatch.setattr(
        anonbench1_pi.Pi, "populate_context_post_run", lambda self, context: None
    )
    (tmp_path / "trajectory.json").write_text(
        json.dumps(
            {
                "steps": [
                    {
                        "metrics": {
                            "prompt_tokens": 0,
                            "completion_tokens": 0,
                            "cached_tokens": 0,
                            "cost_usd": 0,
                        }
                    }
                ]
            }
        )
    )
    context = _Context()
    context.n_input_tokens = 0
    context.n_output_tokens = 0
    context.n_cache_tokens = 0
    context.cost_usd = 0
    agent.populate_context_post_run(context)
    assert context.n_input_tokens is None
    assert context.cost_usd is None
    saved = json.loads((tmp_path / "trajectory.json").read_text())
    assert saved["final_metrics"]["total_prompt_tokens"] is None
    assert saved["steps"][0]["metrics"] is None


class _Context:
    def __init__(self) -> None:
        self.n_input_tokens = 7
        self.n_cache_tokens = 0
        self.n_output_tokens = 2
        self.cost_usd = 0.01
        self.metadata: dict[str, Any] | None = None
