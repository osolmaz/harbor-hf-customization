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


def payload_path(tmp_path: Path, file: str) -> str:
    return str(tmp_path / "payload" / file)


def make_payload(tmp_path: Path, files: list[str], missing: int | None = None) -> None:
    for index, file in enumerate(files):
        if index != missing:
            path = tmp_path / "payload" / file
            path.parent.mkdir(parents=True, exist_ok=True)
            path.touch()


LOCALPI_PAYLOAD = [
    "bin/node",
    "node_modules/@earendil-works/pi-coding-agent/dist/cli.js",
    "node_modules/pi-code-mode/dist/extension/index.js",
    "node_modules/localpi/dist/src/cli/main.js",
]


def test_qwen_endpoint_canary_manifest_keeps_thinking_on() -> None:
    manifest_path = "harnesses/localpi/harbor-agent-qwen-endpoint-cap-canary.json"
    root = next(
        parent
        for parent in Path(__file__).resolve().parents
        if (parent / manifest_path).is_file()
    )
    manifest = json.loads((root / manifest_path).read_text())
    args = manifest["runtime"]["entrypoint"]
    options = dict(zip(args[1::2], args[2::2], strict=True))
    assert manifest["version"] == "0.1.0rc10"
    assert options["--launcher"] == "localpi"
    assert options["--endpoint-engine"] == "vllm"
    assert options["--endpoint-context-window"] == "131072"
    assert options["--thinking"] == "high"
    assert options["--thinking-format"] == "qwen-chat-template"
    assert options["--max-output-tokens"] == "16384"
    assert options["--thinking-phase-output-cap"] == "8000"
    assert (
        0
        < int(options["--thinking-phase-output-cap"])
        < int(options["--max-output-tokens"])
    )


def test_llama_cpp_endpoint_canary_manifest_keeps_thinking_on() -> None:
    manifest_path = "harnesses/localpi/harbor-agent-llama-cpp-endpoint-cap-canary.json"
    root = next(
        parent
        for parent in Path(__file__).resolve().parents
        if (parent / manifest_path).is_file()
    )
    manifest = json.loads((root / manifest_path).read_text())
    args = manifest["runtime"]["entrypoint"]
    options = dict(zip(args[1::2], args[2::2], strict=True))
    assert manifest["version"] == "0.1.0rc10"
    assert options["--launcher"] == "localpi"
    assert options["--endpoint-engine"] == "llama-cpp"
    assert options["--endpoint-context-window"] == "65536"
    assert options["--thinking"] == "high"
    assert options["--thinking-format"] == "none"
    assert options["--max-output-tokens"] == "16384"
    assert options["--thinking-phase-output-cap"] == "8000"
    assert (
        0
        < int(options["--thinking-phase-output-cap"])
        < int(options["--max-output-tokens"])
    )


def test_localpi_launcher_contract(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setattr(runtime, "__file__", str(tmp_path / "runtime.py"))
    make_payload(tmp_path, LOCALPI_PAYLOAD)
    monkeypatch.setenv("PATH", "/usr/bin")
    model: dict[str, object] = {
        "id": "example/model:provider",
        "contextWindow": 128000,
        "maxTokens": 16384,
    }
    settings, logs = tmp_path / "settings", tmp_path / "logs"
    args, env = runtime.command(model, settings, logs, "direct", None, "localpi", 2)
    assert args == [
        payload_path(tmp_path, LOCALPI_PAYLOAD[0]),
        payload_path(tmp_path, LOCALPI_PAYLOAD[3]),
        "--runtime",
        "auto",
        "--provider",
        "hf-pinned",
        "--providers-file",
        str(settings / "providers.json"),
        "--provider-id",
        "hf-pinned",
        "--model",
        "example/model:provider",
        "--api-key",
        "${OPENAI_API_KEY}",
        "--model-profile",
        str(settings / "model-profile.json"),
        "--state-dir",
        str(settings),
        "--session-dir",
        str(logs / "sessions"),
        "--pi-command",
        f"{tmp_path / 'payload/bin/node'} {tmp_path / 'payload' / LOCALPI_PAYLOAD[1]}",
        "--thinking",
        "high",
        "--no-approval",
        "--stats",
        "off",
        "--continue-on-truncation",
        "2",
        "--mode",
        "rpc",
        "--no-extensions",
        "--no-skills",
        "--no-prompt-templates",
        "--no-themes",
    ]
    assert {
        key: env[key]
        for key in ["PI_OFFLINE", "PI_TELEMETRY", "XDG_CONFIG_HOME", "PATH"]
    } == {
        "PI_OFFLINE": "1",
        "PI_TELEMETRY": "0",
        "XDG_CONFIG_HOME": str(settings / "config"),
        "PATH": f"{tmp_path / 'payload/bin'}:/usr/bin",
    }
    # localpi owns Pi's configuration, so the harness writes only its profile.
    assert "PI_CODING_AGENT_DIR" not in env
    assert not (settings / "models.json").exists()
    assert not (settings / "settings.json").exists()
    assert json.loads((settings / "providers.json").read_text()) == {
        "providers": {
            "hf-pinned": {
                "type": "openai-compatible",
                "name": "Hugging Face router",
                "baseUrl": "https://router.huggingface.co/v1",
                "discover": False,
            }
        }
    }
    assert json.loads((settings / "model-profile.json").read_text()) == {
        "id": "hf-pinned",
        "model": "example/model:provider",
        "base_url": "https://router.huggingface.co/v1",
        "capabilities": {"reasoning": False},
        "client": {"context_window": 128000, "max_tokens": 16384},
    }

    # Code mode appends its extension, and the request limit appends its own.
    code_args, _ = runtime.command(model, settings, logs, "code", 4, "localpi")
    assert code_args[-2:] == ["-e", str(tmp_path / "request-limit.mjs")]
    assert payload_path(tmp_path, LOCALPI_PAYLOAD[2]) in code_args
    assert "--continue-on-truncation" not in code_args

    # A model without declared limits keeps localpi's own client settings.
    bare_args, _ = runtime.command(
        {"id": "example/model:provider"}, settings, logs, "direct", None, "localpi"
    )
    index = args.index("--continue-on-truncation")
    assert bare_args == args[:index] + args[index + 2 :]
    assert json.loads((settings / "model-profile.json").read_text()) == {
        "id": "hf-pinned",
        "model": "example/model:provider",
        "base_url": "https://router.huggingface.co/v1",
        "capabilities": {"reasoning": False},
    }

    with pytest.raises(ValueError, match="^The continuation limit cannot be negative$"):
        runtime.command(model, settings, logs, "direct", None, "localpi", -1)
    with pytest.raises(ValueError, match="^Launcher must be pi or localpi$"):
        runtime.command(
            model,
            settings,
            logs,
            "direct",
            None,
            cast(runtime.Launcher, cast(object, "invalid")),
        )


def test_thinking_options_reach_localpi_and_its_profile(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setattr(runtime, "__file__", str(tmp_path / "runtime.py"))
    make_payload(tmp_path, LOCALPI_PAYLOAD)
    monkeypatch.setenv("PATH", "/usr/bin")
    model: dict[str, object] = {
        "id": "example/model:provider",
        "contextWindow": 128000,
        "maxTokens": 16384,
    }
    settings, logs = tmp_path / "settings", tmp_path / "logs"
    args, _ = runtime.command(
        model,
        settings,
        logs,
        "direct",
        None,
        "localpi",
        0,
        16384,
        "off",
        "qwen-chat-template",
    )
    assert args[args.index("--thinking") + 1] == "off"
    assert json.loads((settings / "model-profile.json").read_text())[
        "capabilities"
    ] == {
        "reasoning": True,
        "thinking_format": "qwen-chat-template",
    }

    # The default keeps the provider's own thinking behavior and the catalog entry.
    default_args, _ = runtime.command(model, settings, logs, "direct", None, "localpi")
    assert default_args[default_args.index("--thinking") + 1] == "high"
    assert json.loads((settings / "model-profile.json").read_text())[
        "capabilities"
    ] == {"reasoning": False}
    assert model == {
        "id": "example/model:provider",
        "contextWindow": 128000,
        "maxTokens": 16384,
    }

    for level, format_name in (("shout", "none"), ("off", "guessed")):
        with pytest.raises(ValueError):
            runtime.command(
                model,
                settings,
                logs,
                "direct",
                None,
                "localpi",
                0,
                None,
                cast(runtime.ThinkingLevel, cast(object, level)),
                cast(runtime.ThinkingFormat, cast(object, format_name)),
            )


def test_thinking_format_patches_the_pi_launcher_model(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setattr(runtime, "__file__", str(tmp_path / "runtime.py"))
    make_payload(tmp_path, LOCALPI_PAYLOAD)
    monkeypatch.setenv("PATH", "/usr/bin")
    model: dict[str, object] = {
        "id": "example/model:provider",
        "reasoning": True,
        "compat": {"supportsStrictMode": True},
        "contextWindow": 128000,
        "maxTokens": 16384,
    }
    settings, logs = tmp_path / "settings", tmp_path / "logs"
    args, _ = runtime.command(
        model,
        settings,
        logs,
        "direct",
        None,
        "pi",
        0,
        None,
        "off",
        "qwen-chat-template",
    )
    assert args[args.index("--thinking") + 1] == "off"
    pinned = json.loads((settings / "models.json").read_text())["providers"][
        "hf-pinned"
    ]["models"][0]
    assert pinned["compat"] == {
        "supportsStrictMode": True,
        "thinkingFormat": "qwen-chat-template",
    }
    assert model["compat"] == {"supportsStrictMode": True}


def test_output_token_limit_caps_the_declared_reply_length(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setattr(runtime, "__file__", str(tmp_path / "runtime.py"))
    make_payload(tmp_path, LOCALPI_PAYLOAD)
    monkeypatch.setenv("PATH", "/usr/bin")
    model: dict[str, object] = {
        "id": "example/model:provider",
        "contextWindow": 128000,
        "maxTokens": 32768,
    }
    settings, logs = tmp_path / "settings", tmp_path / "logs"
    runtime.command(model, settings, logs, "direct", None, "localpi", 0, 16384)
    assert json.loads((settings / "model-profile.json").read_text())["client"] == {
        "context_window": 128000,
        "max_tokens": 16384,
    }
    assert model["maxTokens"] == 32768

    other = tmp_path / "other"
    runtime.command(model, other, logs, "direct", None, "pi", 0, 999999)
    providers = json.loads((other / "models.json").read_text())["providers"]
    assert providers["hf-pinned"]["models"][0]["maxTokens"] == 128000
    with pytest.raises(ValueError, match="positive"):
        runtime.command(model, other, logs, "direct", None, "pi", 0, 0)


def test_localpi_launcher_requires_its_payload(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setattr(runtime, "__file__", str(tmp_path / "runtime.py"))
    make_payload(tmp_path, LOCALPI_PAYLOAD, missing=3)
    with pytest.raises(RuntimeError, match="^The pinned runtime payload is missing$"):
        runtime.command({}, tmp_path / "settings", tmp_path, "direct", None, "localpi")


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


def test_nim_base_url_reaches_pi_models(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setattr(runtime, "__file__", str(tmp_path / "runtime.py"))
    make_payload(tmp_path, LOCALPI_PAYLOAD)
    monkeypatch.setenv("PATH", "/usr/bin")
    settings, logs = tmp_path / "settings", tmp_path / "logs"
    args, _ = runtime.command(
        {"id": "private/vendor/model"},
        settings,
        logs,
        "code",
        thinking="xhigh",
        base_url="https://integrate.api.nvidia.com/v1",
    )
    assert args[args.index("--thinking") + 1] == "xhigh"
    provider = json.loads((settings / "models.json").read_text())["providers"][
        "hf-pinned"
    ]
    assert provider["baseUrl"] == "https://integrate.api.nvidia.com/v1"
    with pytest.raises(ValueError, match="HF router or NVIDIA NIM"):
        runtime.command(
            {"id": "x"}, settings, logs, "code", base_url="https://other.example/v1"
        )
