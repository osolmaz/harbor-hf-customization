import json
from io import BytesIO
from pathlib import Path
from urllib.request import Request

import pytest

from harbor_pi_code_mode import models, runtime
from harbor_pi_code_mode.values import count, number, record

BASE = {
    "id": "example/model",
    "api": "openai-completions",
    "provider": "huggingface",
    "reasoning": True,
    "input": ["text"],
    "maxTokens": 1000,
    "compat": {"supportsDeveloperRole": False},
}
PROVIDER = {
    "provider": "provider",
    "status": "live",
    "supports_tools": True,
    "context_length": 2000,
    "pricing": {"input": 1.0, "output": 2.0},
}


@pytest.fixture(autouse=True)
def empty_bundled_catalog(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(models, "BUNDLED_MODELS", {})


@pytest.mark.parametrize("route", ["openai", "huggingface"])
def test_native_model_metadata(monkeypatch: pytest.MonkeyPatch, route: str) -> None:
    monkeypatch.setattr(
        models,
        "fetch_json",
        lambda url: (
            {"example/model": BASE}
            if url == models.CATALOG
            else {"data": {"providers": [PROVIDER]}}
        ),
    )
    result = models.pinned_model(f"{route}/example/model:provider")
    assert result["id"] == "example/model:provider"
    assert result["cost"] == {
        "input": 1.0,
        "output": 2.0,
        "cacheRead": 1.0,
        "cacheWrite": 1.0,
    }
    assert result["contextWindow"] == 2000
    assert result["compat"] == BASE["compat"]
    assert "provider" not in result
    assert result == {
        "id": "example/model:provider",
        "api": "openai-completions",
        "reasoning": True,
        "input": ["text"],
        "maxTokens": 1000,
        "compat": {"supportsDeveloperRole": False},
        "contextWindow": 2000,
        "cost": {"input": 1.0, "output": 2.0, "cacheRead": 1.0, "cacheWrite": 1.0},
    }


@pytest.mark.parametrize(
    "value",
    [
        "model",
        "openai/example/model",
        "wrong/example/model:provider",
        "openai/:provider",
        "openai/model:",
    ],
)
def test_invalid_model_routes(value: str) -> None:
    with pytest.raises(ValueError, match="^Use an explicit HF model and provider$"):
        models.pinned_model(value)


def test_deepseek_v41_flash_contract() -> None:
    model = record(
        json.loads(Path(models.__file__).with_name("model-catalog.json").read_text())
    )["deepseek-ai/DeepSeek-V4.1-Flash"]
    assert model == {
        "id": "deepseek-ai/DeepSeek-V4.1-Flash",
        "name": "DeepSeek V4.1 Flash",
        "api": "openai-completions",
        "reasoning": True,
        "thinkingLevelMap": {
            "off": "none",
            "minimal": None,
            "low": "low",
            "medium": None,
            "high": "high",
            "xhigh": "xhigh",
            "max": "max",
        },
        "input": ["text", "image"],
        "maxTokens": 384000,
        "compat": {"supportsDeveloperRole": False},
    }


def test_bundled_model_does_not_require_remote_catalog(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    base = {**BASE, "id": "bundled/model"}
    monkeypatch.setattr(models, "BUNDLED_MODELS", {"bundled/model": base})

    def fetch(url: str) -> object:
        if url == models.CATALOG:
            raise AssertionError("The remote catalog must not be requested")
        return {"data": {"providers": [PROVIDER]}}

    monkeypatch.setattr(models, "fetch_json", fetch)
    result = models.pinned_model("openai/bundled/model:provider")
    assert result["id"] == "bundled/model:provider"
    assert result["maxTokens"] == 1000


@pytest.mark.parametrize(
    "catalog,provider",
    [
        ({}, PROVIDER),
        ([], PROVIDER),
        ({"example/model": {**BASE, "api": "wrong"}}, PROVIDER),
        ({"example/model": {**BASE, "id": "wrong"}}, PROVIDER),
        ({"example/model": BASE}, {**PROVIDER, "status": "offline"}),
        ({"example/model": BASE}, {**PROVIDER, "supports_tools": False}),
        ({"example/model": BASE}, {**PROVIDER, "pricing": {"input": -1, "output": 1}}),
        ({"example/model": BASE}, {**PROVIDER, "context_length": 0}),
    ],
)
def test_metadata_fails_closed(
    monkeypatch: pytest.MonkeyPatch, catalog: object, provider: object
) -> None:
    monkeypatch.setattr(
        models,
        "fetch_json",
        lambda url: (
            catalog if url == models.CATALOG else {"data": {"providers": [provider]}}
        ),
    )
    with pytest.raises(ValueError):
        models.pinned_model("openai/example/model:provider")


@pytest.mark.parametrize("value", [None, True, "1", -1, float("inf"), float("nan")])
def test_invalid_numbers(value: object) -> None:
    with pytest.raises(ValueError):
        number(value)


def test_records_and_counts() -> None:
    assert record({"a": 1}) == {"a": 1}
    assert count(2.0) == 2
    with pytest.raises(ValueError):
        count(1.5)
    with pytest.raises(ValueError):
        record([])
    with pytest.raises(ValueError):
        record({1: "bad"})


def test_isolated_native_settings(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setattr(runtime, "__file__", str(tmp_path / "runtime.py"))
    for filename in [
        "bin/node",
        "node_modules/@earendil-works/pi-coding-agent/dist/cli.js",
        "node_modules/pi-code-mode/dist/extension/index.js",
    ]:
        path = tmp_path / "payload" / filename
        path.parent.mkdir(parents=True, exist_ok=True)
        path.touch()
    monkeypatch.setenv("OPENAI_API_KEY", "test-only-secret")
    settings = tmp_path / "settings"
    args, env = runtime.command(
        {"id": "example/model:provider"}, settings, tmp_path, "code"
    )
    assert env["OPENAI_API_KEY"] == "test-only-secret"
    assert "--no-extensions" in args and "--mode" in args
    assert str(tmp_path / "payload/bin/node") == args[0]
    assert "test-only-secret" not in (settings / "models.json").read_text()
    assert (
        json.loads((settings / "models.json").read_text())["providers"]["hf-pinned"][
            "apiKey"
        ]
        == "${OPENAI_API_KEY}"
    )
    assert not (settings / "auth.json").exists()
    assert json.loads((settings / "config/pi-code-mode/config.json").read_text()) == {
        "mode": "codex"
    }


@pytest.mark.parametrize(
    "catalog,providers,expected",
    [
        ([], [], "Pi model catalog is unavailable"),
        ({}, [], "The requested model is not in Pi's chat-completions catalog"),
        ({"example/model": BASE}, None, "HF provider metadata is unavailable"),
        (
            {"example/model": BASE},
            [],
            "The requested provider does not offer live tool use",
        ),
        (
            {"example/model": {**BASE, "maxTokens": 0}},
            [PROVIDER],
            "Model limits must be positive",
        ),
    ],
)
def test_metadata_errors_are_actionable(
    monkeypatch: pytest.MonkeyPatch, catalog: object, providers: object, expected: str
) -> None:
    monkeypatch.setattr(
        models,
        "fetch_json",
        lambda url: (
            catalog if url == models.CATALOG else {"data": {"providers": providers}}
        ),
    )
    with pytest.raises(ValueError) as error:
        models.pinned_model("openai/example/model:provider")
    assert str(error.value) == expected


@pytest.mark.parametrize("engine", ["vllm", "llama-cpp"])
def test_endpoint_model_and_localpi_route(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, engine: runtime.EndpointEngine
) -> None:
    url = "https://reviewed.example/v1"
    key = "test-only-key"

    class FakeOpener:
        def open(self, request: Request, timeout: int) -> BytesIO:
            assert request.full_url == f"{url}/models"
            assert request.get_header("Authorization") == f"Bearer {key}"
            assert timeout == 12
            return BytesIO(b'{"data":[{"id":"example/model"}]}')

    monkeypatch.setattr(models, "build_opener", lambda *handlers: FakeOpener())
    model = models.endpoint_model("openai/example/model", url, key, 100000, 16384)
    assert model == {
        "id": "example/model",
        "api": "openai-completions",
        "reasoning": True,
        "input": ["text"],
        "contextWindow": 100000,
        "maxTokens": 16384,
        "cost": {"input": 0, "output": 0, "cacheRead": 0, "cacheWrite": 0},
    }
    monkeypatch.setattr(runtime, "_require_payload", lambda *args, **kwargs: None)
    monkeypatch.setenv("OPENAI_API_KEY", key)
    args, env = runtime.command(
        model,
        tmp_path / "settings",
        tmp_path,
        "direct",
        launcher="localpi",
        continuation_limit=2,
        max_output_tokens=16384,
        endpoint_engine=engine,
        endpoint_base_url=url,
        thinking_budget=8000,
    )
    payload = Path(runtime.__file__).parent / "payload"
    node = payload / "bin/node"
    pi = payload / "node_modules/@earendil-works/pi-coding-agent/dist/cli.js"
    assert args == [
        str(node),
        str(payload / "node_modules/localpi/dist/src/cli/main.js"),
        "--runtime",
        "auto",
        "--provider",
        engine,
        "--providers-file",
        str(tmp_path / "settings/providers.json"),
        "--model",
        "example/model",
        "--api-key",
        "${OPENAI_API_KEY}",
        "--model-profile",
        str(tmp_path / "settings/model-profile.json"),
        "--state-dir",
        str(tmp_path / "settings"),
        "--session-dir",
        str(tmp_path / "sessions"),
        "--pi-command",
        f"{node} {pi}",
        "--thinking",
        "high",
        "--thinking-phase-output-cap",
        "8000",
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
    providers = json.loads((tmp_path / "settings/providers.json").read_text())
    assert providers == {
        "providers": {
            engine: {
                "type": "llama-cpp" if engine == "llama-cpp" else "openai-compatible",
                "name": engine,
                "baseUrl": url,
                "discover": False,
            }
        }
    }
    profile = json.loads((tmp_path / "settings/model-profile.json").read_text())
    assert profile == {
        "id": engine,
        "model": "example/model",
        "base_url": url,
        "capabilities": {"reasoning": True},
        "client": {"context_window": 100000, "max_tokens": 16384},
    }
    assert key not in (tmp_path / "settings/model-profile.json").read_text()
    assert key not in (tmp_path / "settings/providers.json").read_text()
    assert env["OPENAI_API_KEY"] == key


def test_endpoint_rejects_unreviewed_or_mismatched_routes(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    class FakeOpener:
        def open(self, request: object, timeout: int) -> BytesIO:
            return BytesIO(b'{"data":[{"id":"different/model"}]}')

    monkeypatch.setattr(models, "build_opener", lambda *handlers: FakeOpener())
    for base_url in [
        "http://reviewed.example/v1",
        "https:///v1",
        "https://user@reviewed.example/v1",
        "https://:password@reviewed.example/v1",
        "https://reviewed.example/v1?bad=1",
        "https://reviewed.example/v1#fragment",
        "https://reviewed.example/other",
        "https://reviewed.example/v1/other",
    ]:
        with pytest.raises(ValueError, match="reviewed HTTPS"):
            models.endpoint_model(
                "openai/example/model", base_url, "key", 100000, 16384
            )
    with pytest.raises(ValueError, match="did not advertise"):
        models.endpoint_model(
            "openai/example/model", "https://reviewed.example/v1", "key", 100000, 16384
        )
    for requested in ["other/example/model", "openai/", "example/model"]:
        with pytest.raises(ValueError, match="explicit openai"):
            models.endpoint_model(
                requested, "https://reviewed.example/v1", "key", 100000, 16384
            )
    for context, output in [
        (0, 1),
        (-1, 1),
        (True, 1),
        (1, 0),
        (1, -1),
        (1, True),
        (8000, 16384),
    ]:
        with pytest.raises(ValueError, match="output within context"):
            models.endpoint_model(
                "openai/example/model",
                "https://reviewed.example/v1",
                "key",
                context,
                output,
            )


def test_endpoint_model_list_is_bounded(monkeypatch: pytest.MonkeyPatch) -> None:
    class FakeOpener:
        def __init__(self, body: bytes) -> None:
            self.body = body

        def open(self, request: Request, timeout: int) -> BytesIO:
            return BytesIO(self.body)

    maximum = 8 * 1024 * 1024
    for body in [b" " * (maximum + 1), b" " * maximum]:
        monkeypatch.setattr(
            models, "build_opener", lambda *handlers, body=body: FakeOpener(body)
        )
        if len(body) > maximum:
            with pytest.raises(
                ValueError, match="Endpoint model list exceeds its limit"
            ):
                models.endpoint_model(
                    "openai/example/model",
                    "https://reviewed.example/v1",
                    "key",
                    100000,
                    16384,
                )
        else:
            with pytest.raises(json.JSONDecodeError):
                models.endpoint_model(
                    "openai/example/model",
                    "https://reviewed.example/v1",
                    "key",
                    100000,
                    16384,
                )


def test_endpoint_requires_answer_room_before_creating_settings(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setattr(runtime, "_require_payload", lambda *args, **kwargs: None)
    with pytest.raises(ValueError, match="output limit above cap"):
        runtime.command(
            {"id": "example/model", "contextWindow": 100000, "maxTokens": 8000},
            tmp_path / "settings",
            tmp_path,
            "direct",
            launcher="localpi",
            endpoint_engine="vllm",
            endpoint_base_url="https://reviewed.example/v1",
            thinking_budget=8000,
            max_output_tokens=8000,
        )
    assert not (tmp_path / "settings").exists()


def test_old_localpi_payload_cannot_claim_an_endpoint_cap(tmp_path: Path) -> None:
    payload = tmp_path / "payload"
    for filename in [
        "bin/node",
        "node_modules/@earendil-works/pi-coding-agent/dist/cli.js",
        "node_modules/localpi/dist/src/cli/main.js",
    ]:
        path = payload / filename
        path.parent.mkdir(parents=True, exist_ok=True)
        path.touch()
    with pytest.raises(RuntimeError, match="does not support endpoint thinking caps"):
        runtime._require_payload(payload, "direct", "localpi", endpoint_engine="vllm")


def test_missing_payload(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(runtime, "__file__", str(tmp_path / "runtime.py"))
    with pytest.raises(RuntimeError, match="payload"):
        runtime.command({}, tmp_path / "settings", tmp_path, "code")
