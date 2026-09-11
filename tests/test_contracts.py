"""Exact public protocol and runtime configuration contracts."""

import json
from pathlib import Path
from typing import cast
from unittest.mock import Mock

import pytest

from harbor_pi_code_mode import models, runtime
from harbor_pi_code_mode.values import count, number, record


@pytest.mark.parametrize("value", [0, 0.0, 1, 1.25])
def test_number_values(value: int | float) -> None:
    assert number(value) == float(value)
    assert isinstance(number(value), float)


def test_empty_record_and_zero_count() -> None:
    assert record({}) == {}
    assert count(0) == 0
    assert isinstance(count(0), int)


@pytest.mark.parametrize("value", [[], {1: "bad"}])
def test_record_error(value: object) -> None:
    with pytest.raises(ValueError, match="^Expected a JSON object$"):
        record(value)


@pytest.mark.parametrize("value", [None, True, "1", -1, float("inf"), float("nan")])
def test_number_error(value: object) -> None:
    with pytest.raises(ValueError, match="^Expected a nonnegative finite number$"):
        number(value)


def test_count_error() -> None:
    with pytest.raises(ValueError, match="^Expected an integer count$"):
        count(0.5)


def test_metadata_request(monkeypatch: pytest.MonkeyPatch) -> None:
    response = Mock()
    response.read.return_value = b'{"ok": true}'
    context = Mock()
    context.__enter__ = Mock(return_value=response)
    context.__exit__ = Mock(return_value=False)
    open_url = Mock(return_value=context)
    monkeypatch.setattr(models, "urlopen", open_url)
    assert models.fetch_json("https://example.com/models") == {"ok": True}
    request = open_url.call_args.args[0]
    assert request.full_url == "https://example.com/models"
    assert dict(request.header_items()) == {
        "Accept": "application/json",
        "User-agent": "harbor-custom-harnesses/0.1",
    }
    assert open_url.call_args.kwargs == {"timeout": 12}
    response.read.assert_called_once_with(8 * 1024 * 1024 + 1)
    response.read.return_value = b" " * (8 * 1024 * 1024 + 1)
    with pytest.raises(ValueError, match="^Model metadata exceeds its limit$"):
        models.fetch_json("https://example.com/models")
    response.read.return_value = b"0" + b" " * (8 * 1024 * 1024 - 1)
    assert models.fetch_json("https://example.com/models") == 0


def test_runtime_contract(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(runtime, "__file__", str(tmp_path / "runtime.py"))
    files = [
        "bin/node",
        "node_modules/@earendil-works/pi-coding-agent/dist/cli.js",
        "node_modules/pi-code-mode/dist/extension/index.js",
    ]
    for file in files:
        path = tmp_path / "payload" / file
        path.parent.mkdir(parents=True, exist_ok=True)
        path.touch()
    monkeypatch.setenv("PATH", "/usr/bin")
    model: dict[str, object] = {"id": "example/model:provider"}
    settings, logs = tmp_path / "settings", tmp_path / "logs"
    args, env = runtime.command(model, settings, logs, "code")
    assert args == [
        str(tmp_path / "payload" / files[0]),
        str(tmp_path / "payload" / files[1]),
        "--mode",
        "rpc",
        "--provider",
        "hf-pinned",
        "--model",
        "example/model:provider",
        "--thinking",
        "high",
        "--session-dir",
        str(logs / "sessions"),
        "--no-approve",
        "--no-extensions",
        "--no-skills",
        "--no-prompt-templates",
        "--no-themes",
        "-e",
        str(tmp_path / "payload" / files[2]),
    ]
    assert {
        key: env[key]
        for key in [
            "PI_CODING_AGENT_DIR",
            "PI_OFFLINE",
            "PI_TELEMETRY",
            "XDG_CONFIG_HOME",
            "PATH",
        ]
    } == {
        "PI_CODING_AGENT_DIR": str(settings),
        "PI_OFFLINE": "1",
        "PI_TELEMETRY": "0",
        "XDG_CONFIG_HOME": str(settings / "config"),
        "PATH": f"{tmp_path / 'payload/bin'}:/usr/bin",
    }
    assert json.loads((settings / "models.json").read_text()) == {
        "providers": {
            "hf-pinned": {
                "baseUrl": "https://router.huggingface.co/v1",
                "api": "openai-completions",
                "apiKey": "${OPENAI_API_KEY}",
                "models": [model],
            }
        }
    }
    assert json.loads((settings / "settings.json").read_text()) == {
        "retry": {"enabled": False},
        "compaction": {"enabled": True},
    }
    assert json.loads((settings / "config/pi-code-mode/config.json").read_text()) == {
        "mode": "codex"
    }
    assert not (settings / "auth.json").exists()

    direct_settings = tmp_path / "direct-settings"
    direct_args, _ = runtime.command(model, direct_settings, logs, "direct")
    assert direct_args == args[:-2]
    assert not (direct_settings / "config/pi-code-mode/config.json").exists()

    # Initialization is idempotent in an already-created settings directory.
    assert runtime.command(model, settings, logs, "code") == (args, env)
    limited_args, limited_env = runtime.command(model, settings, logs, "code", 4)
    assert limited_args == [*args, "-e", str(tmp_path / "request-limit.mjs")]
    assert limited_env == {**env, "HARBOR_PI_MAX_PROVIDER_REQUESTS": "4"}
    monkeypatch.setenv("HARBOR_PI_MAX_PROVIDER_REQUESTS", "999")
    assert (
        "HARBOR_PI_MAX_PROVIDER_REQUESTS"
        not in runtime.command(model, settings, logs, "code")[1]
    )
    with pytest.raises(
        ValueError, match="^The provider request limit must be positive$"
    ):
        runtime.command(model, settings, logs, "code", 0)
    with pytest.raises(ValueError, match="^Code mode must be direct or code$"):
        runtime.command(
            model, settings, logs, cast(runtime.CodeMode, cast(object, "invalid"))
        )


@pytest.mark.parametrize("missing", [0, 1, 2])
def test_each_payload_component_required(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, missing: int
) -> None:
    monkeypatch.setattr(runtime, "__file__", str(tmp_path / "runtime.py"))
    files = [
        "bin/node",
        "node_modules/@earendil-works/pi-coding-agent/dist/cli.js",
        "node_modules/pi-code-mode/dist/extension/index.js",
    ]
    for index, file in enumerate(files):
        if index != missing:
            path = tmp_path / "payload" / file
            path.parent.mkdir(parents=True, exist_ok=True)
            path.touch()
    with pytest.raises(RuntimeError, match="^The pinned runtime payload is missing$"):
        runtime.command({}, tmp_path / "settings", tmp_path, "code")
