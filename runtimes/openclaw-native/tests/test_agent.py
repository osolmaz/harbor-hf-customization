from pathlib import Path
from typing import cast
from unittest.mock import AsyncMock, Mock

import pytest
from acp import RequestError
from acp.interfaces import Client
from acp.schema import TextContentBlock

from harbor_openclaw_native import agent
from harbor_openclaw_native.models import NIM_ENDPOINT, ROUTER

REQUESTED = "openai/deepseek-ai/DeepSeek-V4-Flash-0731:baseten"
MODEL: dict[str, object] = {
    "id": "deepseek-ai/DeepSeek-V4-Flash-0731:baseten",
    "contextWindow": 1048576,
}
ENVELOPE: dict[str, object] = {
    "ok": True,
    "status": "ok",
    "final": "done",
    "provider": "openai",
    "model": "deepseek-ai/DeepSeek-V4-Flash-0731:baseten",
    "codeModeEngaged": True,
    "usage": {
        "input": 100,
        "output": 10,
        "cacheRead": 3,
        "cacheWrite": 2,
        "totalTokens": 115,
    },
    "costUsd": 0.01,
}


@pytest.fixture
def harness(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> agent.OpenClawNativeAgent:
    monkeypatch.setenv("HARBOR_ACP_REQUESTED_MODEL", REQUESTED)
    monkeypatch.setenv("OPENAI_API_KEY", "test-only")
    monkeypatch.setenv("OPENAI_BASE_URL", ROUTER)
    monkeypatch.setattr(agent, "pinned_model", lambda requested: (requested, MODEL))
    monkeypatch.setattr(agent, "write_config", Mock())
    monkeypatch.setattr(
        agent, "install", AsyncMock(return_value=tmp_path / "openclaw.mjs")
    )
    result = agent.OpenClawNativeAgent("code", tmp_path / "logs")
    result.on_connect(cast(Client, AsyncMock(spec=Client)))
    return result


async def test_session_and_prompt(
    harness: agent.OpenClawNativeAgent,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    initialized = await harness.initialize(1)
    assert initialized.agent_info is not None
    assert initialized.agent_info.name == "openclaw-native"
    assert initialized.agent_info.version == "0.1.0rc4"
    session = await harness.new_session("/app")
    assert harness.settings is not None
    assert (Path(harness.settings.name) / "state").is_dir()
    assert session.config_options is not None
    assert len(session.config_options) == 1
    model_option = session.config_options[0]
    assert model_option.id == "model"
    assert model_option.current_value == REQUESTED
    assert [(option.value, option.name) for option in model_option.options] == [
        (REQUESTED, REQUESTED)
    ]
    configured = await harness.set_config_option("model", session.session_id, REQUESTED)
    assert configured.config_options == session.config_options
    monkeypatch.setattr(agent, "command", Mock(return_value=(["runtime"], {"A": "b"})))
    execute = AsyncMock(return_value=ENVELOPE)
    monkeypatch.setattr(agent, "run", execute)
    response = await harness.prompt(
        session.session_id, [TextContentBlock(type="text", text="work")]
    )
    assert response.stop_reason == "end_turn"
    assert response.usage is not None
    assert response.usage.total_tokens == 115
    assert response.usage.input_tokens == 105
    assert response.usage.output_tokens == 10
    assert harness.conn is not None
    execute.assert_awaited_once()
    await harness.close()


async def test_direct_mode_attestation(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setenv("HARBOR_ACP_REQUESTED_MODEL", REQUESTED)
    result = agent.OpenClawNativeAgent("direct", tmp_path)
    result.attest({**ENVELOPE, "codeModeEngaged": False})
    with pytest.raises(RuntimeError, match="engaged Code Mode in the direct arm"):
        result.attest(ENVELOPE)


@pytest.mark.parametrize(
    "envelope",
    [
        {**ENVELOPE, "provider": "other"},
        {**ENVELOPE, "model": "other"},
        {key: value for key, value in ENVELOPE.items() if key != "codeModeEngaged"},
    ],
)
def test_code_mode_attestation_rejects_mismatch(
    harness: agent.OpenClawNativeAgent, envelope: dict[str, object]
) -> None:
    with pytest.raises(RuntimeError):
        harness.attest(envelope)


async def test_rejects_bad_session_and_missing_key(
    harness: agent.OpenClawNativeAgent,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    with pytest.raises(RequestError):
        harness.require_session("wrong")
    monkeypatch.delenv("OPENAI_API_KEY")
    with pytest.raises(RuntimeError, match="credential is not configured"):
        await harness.new_session("/app")


async def test_rejects_unsupported_session_options(
    harness: agent.OpenClawNativeAgent,
) -> None:
    with pytest.raises(RequestError):
        await harness.new_session("/app", additional_directories=["/tmp"])


@pytest.mark.parametrize(
    ("config_id", "session_id", "value"),
    [
        ("other", "current", REQUESTED),
        ("model", "current", "openai/other"),
        ("model", "wrong", REQUESTED),
    ],
)
async def test_rejects_unsupported_model_selection(
    harness: agent.OpenClawNativeAgent,
    config_id: str,
    session_id: str,
    value: str,
) -> None:
    session = await harness.new_session("/app")
    selected_session = session.session_id if session_id == "current" else session_id
    with pytest.raises(RequestError):
        await harness.set_config_option(config_id, selected_session, value)


async def test_unpriced_endpoint_does_not_report_zero_dollar_cost(
    harness: agent.OpenClawNativeAgent, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setenv("OPENAI_BASE_URL", NIM_ENDPOINT)
    harness.model = {"contextWindow": 1000000}
    harness.session_id = "session"
    await harness.report_usage(ENVELOPE)
    assert harness.conn is not None
    update_mock = cast(AsyncMock, cast(object, harness.conn.session_update))
    assert update_mock.await_args is not None
    update = update_mock.await_args.kwargs["update"]
    assert update.cost is None
    assert update.size == 1000000


async def test_error_envelope_keeps_usage(
    harness: agent.OpenClawNativeAgent,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    session = await harness.new_session("/app")
    monkeypatch.setattr(agent, "command", Mock(return_value=(["runtime"], {})))
    monkeypatch.setattr(
        agent,
        "run",
        AsyncMock(
            return_value={
                **ENVELOPE,
                "ok": False,
                "status": "error",
                "error": {"message": "provider failed"},
            }
        ),
    )
    with pytest.raises(RuntimeError, match="provider failed"):
        await harness.prompt(
            session.session_id, [TextContentBlock(type="text", text="work")]
        )
    assert harness.active is False


async def test_cancel(harness: agent.OpenClawNativeAgent) -> None:
    harness.session_id = "session"
    cancel = AsyncMock()
    harness.process.cancel = cancel
    await harness.cancel("session")
    assert harness.cancelled.is_set()
    cancel.assert_awaited_once()


async def test_serve_cleanup(monkeypatch: pytest.MonkeyPatch) -> None:
    harness = AsyncMock()
    constructor = Mock(return_value=harness)
    runner = AsyncMock(side_effect=RuntimeError("transport closed"))
    monkeypatch.setattr(agent, "OpenClawNativeAgent", constructor)
    monkeypatch.setattr(agent, "run_agent", runner)
    with pytest.raises(RuntimeError, match="transport closed"):
        await agent.serve("direct")
    constructor.assert_called_once_with("direct")
    harness.close.assert_awaited_once()


@pytest.mark.parametrize("mode", ["direct", "code"])
def test_cli(monkeypatch: pytest.MonkeyPatch, mode: str) -> None:
    import sys

    monkeypatch.setattr(sys, "argv", ["harbor-openclaw-native", "--code-mode", mode])
    serve = Mock(return_value="awaitable")
    run = Mock()
    monkeypatch.setattr(agent, "serve", serve)
    monkeypatch.setattr(agent.asyncio, "run", run)
    agent.main()
    serve.assert_called_once_with(mode)
    run.assert_called_once_with("awaitable")
