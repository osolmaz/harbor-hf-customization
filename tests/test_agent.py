from pathlib import Path
from typing import cast, override
from unittest.mock import AsyncMock, Mock

import pytest
from acp import RequestError
from acp.interfaces import Client
from acp.schema import TextContentBlock

from harbor_pi_code_mode import agent
from harbor_pi_code_mode.rpc import PiRpc

STATS: dict[str, object] = {
    "tokens": {
        "input": 100,
        "output": 10,
        "cacheRead": 3,
        "cacheWrite": 2,
        "total": 115,
    },
    "cost": 0.01,
    "contextUsage": {"tokens": 100, "contextWindow": 2000},
}


class FakePi(PiRpc):
    def __init__(self) -> None:
        super().__init__()
        self.closed = False
        self.calls: list[str] = []
        self.selected = {"id": "example/model:provider", "provider": "hf-pinned"}

    @override
    async def start(
        self, command: list[str], cwd: str, env: dict[str, str], log: Path
    ) -> None:
        log.write_text("{}\n")

    @override
    async def request(self, command: str, **values: object) -> dict[str, object]:
        self.calls.append(command)
        if command == "get_state":
            return {"model": self.selected}
        if command == "get_session_stats":
            return STATS
        return {}

    @override
    async def close(self) -> None:
        self.closed = True


@pytest.fixture
def harness(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> agent.PiCodeModeAgent:
    monkeypatch.setenv("HARBOR_ACP_REQUESTED_MODEL", "openai/example/model:provider")
    monkeypatch.setenv("OPENAI_API_KEY", "test-only")
    monkeypatch.setattr(
        agent, "pinned_model", lambda requested: {"id": "example/model:provider"}
    )
    monkeypatch.setattr(agent, "command", lambda *args, **kwargs: (["fake"], {}))
    result = agent.PiCodeModeAgent("code", tmp_path)
    result.rpc = FakePi()
    result.on_connect(cast(Client, AsyncMock(spec=Client)))
    return result


async def test_endpoint_uses_only_reviewed_binding(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setenv("HARBOR_ACP_REQUESTED_MODEL", "openai/example/model")
    monkeypatch.setenv("OPENAI_API_KEY", "test-only")
    monkeypatch.setenv("OPENAI_BASE_URL", "https://reviewed.example/v1")
    observed: list[object] = []

    def fake_model(*args: object) -> dict[str, object]:
        observed.extend(args)
        return {"id": "example/model"}

    monkeypatch.setattr(agent, "endpoint_model", fake_model)
    monkeypatch.setattr(agent, "command", lambda *args, **kwargs: (["fake"], {}))
    instance = agent.PiCodeModeAgent(
        "direct",
        tmp_path,
        launcher="localpi",
        endpoint_engine="vllm",
        endpoint_context_window=100000,
        max_output_tokens=16384,
        thinking_budget=8000,
    )
    instance.rpc = FakePi()
    instance.rpc.selected = {"id": "example/model", "provider": "vllm"}
    instance.on_connect(cast(Client, AsyncMock(spec=Client)))
    await instance.new_session("/app")
    assert observed == [
        "openai/example/model",
        "https://reviewed.example/v1",
        "test-only",
        100000,
        16384,
    ]
    await instance.close()

    monkeypatch.delenv("OPENAI_BASE_URL")
    missing = agent.PiCodeModeAgent(
        "direct",
        tmp_path,
        launcher="localpi",
        endpoint_engine="vllm",
        endpoint_context_window=100000,
        max_output_tokens=16384,
        thinking_budget=8000,
    )
    with pytest.raises(RuntimeError, match="reviewed endpoint URL"):
        await missing.new_session("/app")


async def test_native_acp_session(harness: agent.PiCodeModeAgent) -> None:
    initialized = await harness.initialize(1)
    assert initialized.agent_info is not None
    assert initialized.agent_info.name == "pi-code-mode"
    assert initialized.agent_info.version == "0.1.0rc8"
    assert initialized.protocol_version == 1
    assert initialized.agent_capabilities is not None
    response = await harness.new_session("/app")
    assert response.config_options is not None
    option = response.config_options[0]
    assert option.model_dump(by_alias=True, exclude_none=True) == {
        "id": "model",
        "name": "Model",
        "category": "model",
        "type": "select",
        "currentValue": "openai/example/model:provider",
        "options": [
            {
                "value": "openai/example/model:provider",
                "name": "openai/example/model:provider",
            }
        ],
    }
    selected = await harness.set_config_option(
        "model", response.session_id, harness.model_id
    )
    assert selected.config_options[0].id == "model"
    with pytest.raises(RequestError):
        await harness.set_config_option("model", response.session_id, "different")
    with pytest.raises(RequestError):
        await harness.new_session("/app")
    with pytest.raises(RequestError):
        harness.require_session("missing")
    await harness.cancel(response.session_id)
    assert harness.cancelled.is_set()
    await harness.close()
    assert isinstance(harness.rpc, FakePi) and harness.rpc.closed


async def test_direct_agent_identity(tmp_path: Path) -> None:
    harness = agent.PiCodeModeAgent("direct", tmp_path)
    initialized = await harness.initialize(1)
    assert initialized.agent_info is not None
    assert initialized.agent_info.name == "pi-direct"
    assert harness.logs == tmp_path
    await harness.close()


async def test_forwards_tools_and_authoritative_usage(
    harness: agent.PiCodeModeAgent,
) -> None:
    response = await harness.new_session("/app")
    events: list[dict[str, object]] = [
        {"type": "message_end", "message": {"role": "user", "content": []}},
        {
            "type": "message_end",
            "message": {
                "role": "assistant",
                "content": [
                    {"type": "text", "text": "hello"},
                    {"type": "thinking", "thinking": "reason"},
                ],
            },
        },
        {
            "type": "tool_execution_start",
            "toolCallId": "one",
            "toolName": "exec",
            "args": {"code": "text(1)"},
        },
        {
            "type": "tool_execution_end",
            "toolCallId": "one",
            "result": {"content": []},
            "isError": False,
        },
        {"type": "compaction_end", "aborted": False},
        {"type": "agent_settled"},
    ]
    for event in events:
        harness.rpc.events.put_nowait(event)
    result = await harness.prompt(
        response.session_id, [TextContentBlock(type="text", text="test")]
    )
    assert result.stop_reason == "end_turn"
    assert result.usage is not None and result.usage.input_tokens == 105
    assert result.usage.model_dump(by_alias=True, exclude_none=True) == {
        "inputTokens": 105,
        "outputTokens": 10,
        "totalTokens": 115,
        "cachedReadTokens": 3,
        "cachedWriteTokens": 2,
    }
    prompt_calls = cast(FakePi, harness.rpc).calls
    assert "prompt" in prompt_calls
    connection = cast(AsyncMock, harness.conn)
    updates = [
        call.kwargs["update"] for call in connection.session_update.call_args_list
    ]
    assert any(
        update.session_update == "tool_call" and update.title == "exec"
        for update in updates
    )
    costs = [
        update.cost.amount
        for update in updates
        if update.session_update == "usage_update"
    ]
    assert costs == [0.01, 0.01]
    assert [
        call.kwargs["session_id"] for call in connection.session_update.call_args_list
    ] == [response.session_id] * len(updates)
    for update in updates:
        if update.session_update == "usage_update":
            assert update.used == 100 and update.size == 2000
            assert update.cost.currency == "USD"
        elif update.session_update == "tool_call":
            assert update.tool_call_id == "one" and update.status == "in_progress"
            assert update.raw_input == {"code": "text(1)"}
        elif update.session_update == "tool_call_update":
            assert update.tool_call_id == "one" and update.status == "completed"
            assert update.raw_output == {"content": []}
    await harness.close()


async def test_inference_failure_retains_usage(harness: agent.PiCodeModeAgent) -> None:
    response = await harness.new_session("/app")
    harness.rpc.events.put_nowait(
        {
            "type": "message_end",
            "message": {"role": "assistant", "stopReason": "error", "content": []},
        }
    )
    harness.rpc.events.put_nowait({"type": "agent_settled"})
    with pytest.raises(RuntimeError, match="usage was retained"):
        await harness.prompt(response.session_id, [])
    assert harness.active is False
    await harness.close()


async def test_rpc_failure_and_duplicate_prompt(harness: agent.PiCodeModeAgent) -> None:
    response = await harness.new_session("/app")
    harness.active = True
    with pytest.raises(RequestError):
        await harness.prompt(response.session_id, [])
    harness.active = False
    harness.rpc.events.put_nowait(None)
    with pytest.raises(RuntimeError, match="stopped"):
        await harness.prompt(response.session_id, [])
    await harness.close()


async def test_missing_credential(
    harness: agent.PiCodeModeAgent, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.delenv("OPENAI_API_KEY")
    with pytest.raises(
        RuntimeError, match="^The inference credential is not configured$"
    ):
        await harness.new_session("/app")
    await harness.close()


async def test_message_contract(harness: agent.PiCodeModeAgent) -> None:
    response = await harness.new_session("/app")
    connection = cast(AsyncMock, harness.conn)
    await harness.message({"message": {"role": "user", "content": []}})
    connection.session_update.assert_not_called()
    await harness.message(
        {
            "message": {
                "role": "assistant",
                "content": [
                    {"type": "text", "text": "answer"},
                    {"type": "thinking", "thinking": "reason"},
                    {"type": "text"},
                    {"type": "thinking"},
                    {"type": "unknown"},
                ],
            }
        }
    )
    updates = [
        call.kwargs["update"] for call in connection.session_update.call_args_list
    ]
    assert [(item.session_update, item.content.text) for item in updates[:-1]] == [
        ("agent_message_chunk", "answer"),
        ("agent_thought_chunk", "reason"),
        ("agent_message_chunk", ""),
        ("agent_thought_chunk", ""),
    ]
    assert not harness.failed
    with pytest.raises(ValueError, match="^Invalid Pi message content$"):
        await harness.message({"message": {"role": "assistant", "content": None}})
    await harness.event(
        {"type": "tool_execution_end", "toolCallId": "failed-tool", "isError": True}
    )
    update = connection.session_update.call_args.kwargs["update"]
    assert update.status == "failed" and update.tool_call_id == "failed-tool"
    assert update.raw_output is None
    await harness.cancel(response.session_id)
    assert harness.cancelled.is_set()
    assert cast(FakePi, harness.rpc).calls[-1] == "abort"
    await harness.close()


async def test_session_start_contract(
    harness: agent.PiCodeModeAgent, monkeypatch: pytest.MonkeyPatch
) -> None:
    model = {"id": "example/model:provider"}
    resolve = Mock(return_value=model)
    launch = Mock(return_value=(["runtime"], {"TEST": "value"}))
    monkeypatch.setattr(agent, "pinned_model", resolve)
    monkeypatch.setattr(agent, "command", launch)
    start = AsyncMock()
    monkeypatch.setattr(harness.rpc, "start", start)
    await harness.new_session("/workspace")
    resolve.assert_called_once_with("openai/example/model:provider")
    assert harness.settings is not None
    settings = Path(harness.settings.name)
    launch.assert_called_once_with(
        model, settings, harness.logs, "code", None, "pi", 0, None, "high", "none"
    )
    start.assert_awaited_once_with(
        ["runtime"], "/workspace", {"TEST": "value"}, harness.logs / "pi-events.jsonl"
    )
    assert settings.exists()
    await harness.close()
    assert not settings.exists()


@pytest.mark.parametrize(
    "model",
    [
        {"id": "wrong", "provider": "hf-pinned"},
        {"id": "example/model:provider", "provider": "wrong"},
    ],
)
async def test_model_attestation(
    harness: agent.PiCodeModeAgent, monkeypatch: pytest.MonkeyPatch, model: object
) -> None:
    monkeypatch.setattr(
        harness.rpc, "request", AsyncMock(return_value={"model": model})
    )
    with pytest.raises(
        RuntimeError, match="^Pi selected a different model or provider$"
    ):
        await harness.new_session("/app")
    await harness.close()


async def test_serve_cleanup(monkeypatch: pytest.MonkeyPatch) -> None:
    harness = AsyncMock()
    constructor = Mock(return_value=harness)
    runner = AsyncMock(side_effect=RuntimeError("transport closed"))
    monkeypatch.setattr(agent, "PiCodeModeAgent", constructor)
    monkeypatch.setattr(agent, "run_agent", runner)
    with pytest.raises(RuntimeError, match="transport closed"):
        await agent.serve("direct")
    constructor.assert_called_once_with(
        code_mode="direct",
        max_provider_requests=None,
        launcher="pi",
        continuation_limit=0,
        max_output_tokens=None,
        thinking="high",
        thinking_format="none",
        endpoint_engine=None,
        endpoint_context_window=None,
        thinking_budget=None,
    )
    runner.assert_awaited_once_with(harness)
    harness.close.assert_awaited_once_with()


@pytest.mark.parametrize("limit", [None, 1, 4])
def test_cli_request_bound(monkeypatch: pytest.MonkeyPatch, limit: int | None) -> None:
    import sys

    argv = ["harbor-pi-code-mode", "--code-mode", "code"]
    if limit is not None:
        argv.extend(["--max-provider-requests", str(limit)])
    monkeypatch.setattr(sys, "argv", argv)
    serve = Mock(return_value="awaitable")
    run = Mock()
    monkeypatch.setattr(agent, "serve", serve)
    monkeypatch.setattr(agent.asyncio, "run", run)
    agent.main()
    serve.assert_called_once_with("code", limit, "pi", 0, None, "high", "none")
    run.assert_called_once_with("awaitable")


@pytest.mark.parametrize("limit", [0, 2])
def test_cli_launcher_and_continuation(
    monkeypatch: pytest.MonkeyPatch, limit: int
) -> None:
    import sys

    monkeypatch.setattr(
        sys,
        "argv",
        [
            "harbor-pi-code-mode",
            "--code-mode",
            "direct",
            "--launcher",
            "localpi",
            "--continue-on-truncation",
            str(limit),
        ],
    )
    serve = Mock(return_value="awaitable")
    monkeypatch.setattr(agent, "serve", serve)
    monkeypatch.setattr(agent.asyncio, "run", Mock())
    agent.main()
    serve.assert_called_once_with(
        "direct", None, "localpi", limit, None, "high", "none"
    )


@pytest.mark.parametrize(
    ("level", "format_name"), [("off", "qwen-chat-template"), ("low", "none")]
)
def test_cli_thinking_options(
    monkeypatch: pytest.MonkeyPatch, level: str, format_name: str
) -> None:
    import sys

    monkeypatch.setattr(
        sys,
        "argv",
        [
            "harbor-pi-code-mode",
            "--code-mode",
            "direct",
            "--thinking",
            level,
            "--thinking-format",
            format_name,
        ],
    )
    serve = Mock(return_value="awaitable")
    monkeypatch.setattr(agent, "serve", serve)
    monkeypatch.setattr(agent.asyncio, "run", Mock())
    agent.main()
    serve.assert_called_once_with("direct", None, "pi", 0, None, level, format_name)


def test_cli_rejects_an_unknown_thinking_level(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    import sys

    monkeypatch.setattr(
        sys,
        "argv",
        ["harbor-pi-code-mode", "--code-mode", "direct", "--thinking", "loud"],
    )
    with pytest.raises(SystemExit) as error:
        agent.main()
    assert error.value.code == 2


@pytest.mark.parametrize("limit", [8192, 1])
def test_cli_output_token_limit(monkeypatch: pytest.MonkeyPatch, limit: int) -> None:
    import sys

    monkeypatch.setattr(
        sys,
        "argv",
        [
            "harbor-pi-code-mode",
            "--code-mode",
            "direct",
            "--launcher",
            "localpi",
            "--max-output-tokens",
            str(limit),
        ],
    )
    serve = Mock(return_value="awaitable")
    monkeypatch.setattr(agent, "serve", serve)
    monkeypatch.setattr(agent.asyncio, "run", Mock())
    agent.main()
    serve.assert_called_once_with("direct", None, "localpi", 0, limit, "high", "none")


def test_cli_rejects_a_nonpositive_output_token_limit(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    import sys

    monkeypatch.setattr(
        sys,
        "argv",
        [
            "harbor-pi-code-mode",
            "--code-mode",
            "direct",
            "--max-output-tokens",
            "0",
        ],
    )
    with pytest.raises(SystemExit) as error:
        agent.main()
    assert error.value.code == 2


def test_cli_rejects_a_negative_continuation_limit(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    import sys

    monkeypatch.setattr(
        sys,
        "argv",
        [
            "harbor-pi-code-mode",
            "--code-mode",
            "direct",
            "--continue-on-truncation",
            "-1",
        ],
    )
    with pytest.raises(SystemExit):
        agent.main()


@pytest.mark.parametrize("limit", ["0", "-1", "bad"])
def test_cli_rejects_invalid_bound(monkeypatch: pytest.MonkeyPatch, limit: str) -> None:
    import sys

    monkeypatch.setattr(
        sys,
        "argv",
        [
            "harbor-pi-code-mode",
            "--code-mode",
            "code",
            "--max-provider-requests",
            limit,
        ],
    )
    with pytest.raises(SystemExit) as error:
        agent.main()
    assert error.value.code == 2
