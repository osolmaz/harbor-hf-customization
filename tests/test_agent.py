from pathlib import Path
from typing import cast, override
from unittest.mock import AsyncMock

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

    @override
    async def start(
        self, command: list[str], cwd: str, env: dict[str, str], log: Path
    ) -> None:
        log.write_text("{}\n")

    @override
    async def request(self, command: str, **values: object) -> dict[str, object]:
        self.calls.append(command)
        if command == "get_state":
            return {"model": {"id": "example/model:provider", "provider": "hf-pinned"}}
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
    monkeypatch.setattr(agent, "command", lambda *args: (["fake"], {}))
    result = agent.PiCodeModeAgent(tmp_path)
    result.rpc = FakePi()
    result.on_connect(cast(Client, AsyncMock(spec=Client)))
    return result


async def test_native_acp_session(harness: agent.PiCodeModeAgent) -> None:
    initialized = await harness.initialize(1)
    assert initialized.agent_info is not None
    assert initialized.agent_info.name == "pi-code-mode"
    response = await harness.new_session("/app")
    assert response.config_options is not None
    assert response.config_options[0].category == "model"
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
    assert result.usage.cached_read_tokens == 3
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
    with pytest.raises(RuntimeError, match="credential"):
        await harness.new_session("/app")
    await harness.close()
