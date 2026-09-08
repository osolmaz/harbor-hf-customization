import json
from pathlib import Path

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
    args, env = runtime.command({"id": "example/model:provider"}, settings, tmp_path)
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


def test_missing_payload(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(runtime, "__file__", str(tmp_path / "runtime.py"))
    with pytest.raises(RuntimeError, match="payload"):
        runtime.command({}, tmp_path / "settings", tmp_path)
