import json
from pathlib import Path
from unittest.mock import AsyncMock

import pytest

from harbor_openclaw_native import runtime
from harbor_openclaw_native.process import OpenClawProcess

REQUESTED = "openai/deepseek-ai/DeepSeek-V4-Flash-0731:baseten"
MODEL: dict[str, object] = {
    "id": "deepseek-ai/DeepSeek-V4-Flash-0731:baseten",
    "name": "deepseek-ai/DeepSeek-V4-Flash-0731:baseten",
    "contextWindow": 1048576,
    "maxTokens": 384000,
    "cost": {"input": 0.13, "output": 0.26},
}


def make_payload(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Path:
    monkeypatch.setattr(runtime, "__file__", str(tmp_path / "runtime.py"))
    monkeypatch.delenv("OPENAI_BASE_URL", raising=False)
    payload = tmp_path / "payload"
    for name in (
        "bin/node",
        "lib/node_modules/npm/bin/npm-cli.js",
        "openclaw.tgz",
        "package.json",
        "package-lock.json",
    ):
        path = payload / name
        path.parent.mkdir(parents=True, exist_ok=True)
        path.touch()
    (payload / "provenance.json").write_text(
        json.dumps(
            {
                "sourceCommit": runtime.SOURCE_COMMIT,
                "openclawVersion": runtime.OPENCLAW_VERSION,
                "nodeVersion": "24.18.0",
            }
        )
    )
    return payload


def test_validate_payload(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    payload = make_payload(tmp_path, monkeypatch)
    assert runtime.validate_payload()["sourceCommit"] == runtime.SOURCE_COMMIT
    (payload / "provenance.json").write_text("{}")
    with pytest.raises(RuntimeError, match="provenance does not match"):
        runtime.validate_payload()
    (payload / "bin/node").unlink()
    with pytest.raises(RuntimeError, match="runtime payload is missing"):
        runtime.validate_payload()


@pytest.mark.parametrize(("mode", "enabled"), [("direct", False), ("code", True)])
def test_config_and_command(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    mode: str,
    enabled: bool,
) -> None:
    payload = make_payload(tmp_path, monkeypatch)
    config = tmp_path / "openclaw.json"
    runtime.write_config(config, requested_model=REQUESTED, model=MODEL, code_mode=mode)
    value = json.loads(config.read_text())
    assert value["agents"]["defaults"]["models"][REQUESTED] == {
        "agentRuntime": {"id": "openclaw"},
        "codeMode": enabled,
    }
    assert value["tools"]["codeMode"] == {"enabled": enabled}
    assert value["models"]["providers"]["openai"]["api"] == "openai-completions"
    args, env = runtime.command(
        entrypoint=tmp_path / "openclaw.mjs",
        config=config,
        state=tmp_path / "state",
        workspace="/app",
        instruction=tmp_path / "instruction.txt",
        requested_model=REQUESTED,
        code_mode=mode,
    )
    assert args[0] == str(payload / "bin/node")
    assert args[-5:] == ["--thinking", "high", "--timeout", "0", "--json"]
    assert args[args.index("--code-mode") + 1] == mode
    assert env["OPENCLAW_TELEMETRY_DISABLED"] == "1"


def test_nim_route_uses_high_and_lean_tools(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    make_payload(tmp_path, monkeypatch)
    monkeypatch.setenv("OPENAI_BASE_URL", runtime.NIM_ENDPOINT)
    requested = "openai/private/vendor/reviewed-model"
    model: dict[str, object] = {
        "id": "private/vendor/reviewed-model",
        "reasoning": True,
        "thinkingLevelMap": {"high": "high"},
        "compat": {"supportsReasoningEffort": True},
        "contextWindow": 1000000,
        "maxTokens": 65536,
    }
    config = tmp_path / "openclaw.json"
    runtime.write_config(
        config, requested_model=requested, model=model, code_mode="direct"
    )
    value = json.loads(config.read_text())
    assert value["models"]["providers"]["openai"]["baseUrl"] == runtime.NIM_ENDPOINT
    assert value["models"]["providers"]["openai"]["models"] == [model]
    assert value["agents"]["defaults"]["experimental"]["localModelLean"] is True
    assert value["agents"]["defaults"]["models"][requested]["params"] == {
        "extra_body": {"reasoning_budget": 16384}
    }
    args, _env = runtime.command(
        entrypoint=tmp_path / "runtime/node_modules/openclaw/openclaw.mjs",
        config=config,
        state=tmp_path / "state",
        workspace="/app",
        instruction=tmp_path / "prompt",
        requested_model=requested,
        code_mode="direct",
    )
    assert args[args.index("--thinking") + 1] == "high"
    assert "--local-model-lean" in args
    assert "--auth-env-only" not in args


async def test_install(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    payload = make_payload(tmp_path, monkeypatch)
    settings = tmp_path / "settings"
    logs = tmp_path / "logs"
    logs.mkdir()
    process = AsyncMock()
    process.returncode = 0
    process.communicate.return_value = (b"installed\n", None)
    factory = AsyncMock(return_value=process)
    monkeypatch.setattr(runtime.asyncio, "create_subprocess_exec", factory)

    async def communicate() -> tuple[bytes, None]:
        (settings / "runtime/node_modules/openclaw").mkdir(parents=True)
        (settings / "runtime/node_modules/openclaw/openclaw.mjs").touch()
        return b"installed\n", None

    process.communicate.side_effect = communicate
    entrypoint = await runtime.install(settings, logs)
    assert entrypoint.is_file()
    assert (logs / "npm-install.log").read_text() == "installed\n"
    assert (
        json.loads((logs / "openclaw-source.json").read_text())["sourceCommit"]
        == runtime.SOURCE_COMMIT
    )
    assert (settings / "runtime/openclaw.tgz").read_bytes() == (
        payload / "openclaw.tgz"
    ).read_bytes()


async def test_install_failure(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    make_payload(tmp_path, monkeypatch)
    logs = tmp_path / "logs"
    logs.mkdir()
    process = AsyncMock()
    process.returncode = 1
    process.communicate.return_value = (b"failed", None)
    monkeypatch.setattr(
        runtime.asyncio, "create_subprocess_exec", AsyncMock(return_value=process)
    )
    with pytest.raises(RuntimeError, match=r"install failed \(1\)"):
        await runtime.install(tmp_path / "settings", logs)


async def test_run_parses_envelope(tmp_path: Path) -> None:
    process = AsyncMock(spec=OpenClawProcess)
    process.run.return_value = '{"ok": true}'
    logs = tmp_path / "logs"
    logs.mkdir()
    assert await runtime.run(
        process, args=["openclaw"], workspace="/app", env={}, logs=logs
    ) == {"ok": True}
    process.run.return_value = "bad"
    with pytest.raises(RuntimeError, match="stable JSON envelope"):
        await runtime.run(
            process, args=["openclaw"], workspace="/app", env={}, logs=logs
        )
