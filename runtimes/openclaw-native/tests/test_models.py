from unittest.mock import Mock

import pytest

from harbor_openclaw_native import models

REQUESTED = "openai/deepseek-ai/DeepSeek-V4-Flash-0731:baseten"
NIM_REQUESTED = "openai/private/vendor/reviewed-model"


@pytest.fixture(autouse=True)
def use_default_router(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.delenv("OPENAI_BASE_URL", raising=False)


def test_reviewed_nim_route(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("OPENAI_BASE_URL", models.NIM_ENDPOINT)
    requested, model = models.pinned_model(NIM_REQUESTED)
    assert requested == NIM_REQUESTED
    assert model == {
        "id": "private/vendor/reviewed-model",
        "name": "private/vendor/reviewed-model",
        "reasoning": True,
        "input": ["text"],
        "contextWindow": 1000000,
        "maxTokens": 65536,
        "thinkingLevelMap": {"high": "high"},
        "compat": {"supportsReasoningEffort": True},
    }
    with pytest.raises(ValueError, match="exact reviewed endpoint model"):
        models.pinned_model(REQUESTED)
    monkeypatch.setenv("OPENAI_BASE_URL", "https://other.example/v1")
    with pytest.raises(ValueError, match="not supported"):
        models.pinned_model(NIM_REQUESTED)


def provider_data(**changes: object) -> dict[str, object]:
    provider: dict[str, object] = {
        "provider": "baseten",
        "status": "live",
        "supports_tools": True,
        "context_length": 1048576,
        "pricing": {"input": 0.13, "output": 0.26},
    }
    provider.update(changes)
    return {"data": {"providers": [provider]}}


def test_pinned_model(monkeypatch: pytest.MonkeyPatch) -> None:
    responses = iter(
        [
            {
                "deepseek-ai/DeepSeek-V4-Flash-0731": {
                    "id": "deepseek-ai/DeepSeek-V4-Flash-0731",
                    "api": "openai-completions",
                    "reasoning": True,
                    "input": ["text"],
                    "maxTokens": 384000,
                }
            },
            provider_data(),
        ]
    )
    monkeypatch.setattr(models, "fetch_json", lambda _url: next(responses))
    requested, model = models.pinned_model(REQUESTED)
    assert requested == REQUESTED
    assert model == {
        "id": "deepseek-ai/DeepSeek-V4-Flash-0731:baseten",
        "name": "deepseek-ai/DeepSeek-V4-Flash-0731:baseten",
        "reasoning": True,
        "input": ["text"],
        "cost": {
            "input": 0.13,
            "output": 0.26,
            "cacheRead": 0.13,
            "cacheWrite": 0.13,
        },
        "contextWindow": 1048576,
        "maxTokens": 384000,
    }


def test_fetch_json_contract(monkeypatch: pytest.MonkeyPatch) -> None:
    response = Mock()
    response.read.return_value = b'{"ok": true}'
    context = Mock()
    context.__enter__ = Mock(return_value=response)
    context.__exit__ = Mock(return_value=False)
    open_url = Mock(return_value=context)
    monkeypatch.setattr(models, "urlopen", open_url)
    assert models.fetch_json("https://example.com/model") == {"ok": True}
    request = open_url.call_args.args[0]
    assert request.full_url == "https://example.com/model"
    assert dict(request.header_items()) == {
        "Accept": "application/json",
        "User-agent": "harbor-custom-harnesses/0.1",
    }
    assert open_url.call_args.kwargs == {"timeout": 12}
    response.read.return_value = b" " * (8 * 1024 * 1024 + 1)
    with pytest.raises(ValueError, match="^Model metadata exceeds its limit$"):
        models.fetch_json("https://example.com/model")


@pytest.mark.parametrize(
    ("requested", "catalog", "route", "message"),
    [
        ("bad", {}, {}, "Use an explicit"),
        (REQUESTED, [], {}, "catalog is unavailable"),
        (REQUESTED, {}, {}, "not in the chat-completions catalog"),
    ],
)
def test_invalid_model_inputs(
    monkeypatch: pytest.MonkeyPatch,
    requested: str,
    catalog: object,
    route: object,
    message: str,
) -> None:
    responses = iter([catalog, route])
    monkeypatch.setattr(models, "fetch_json", lambda _url: next(responses))
    with pytest.raises(ValueError, match=message):
        models.pinned_model(requested)


@pytest.mark.parametrize(
    "provider",
    [
        None,
        {"provider": "baseten", "status": "staging", "supports_tools": True},
        {"provider": "baseten", "status": "live", "supports_tools": False},
    ],
)
def test_rejects_unavailable_provider(
    monkeypatch: pytest.MonkeyPatch, provider: dict[str, object] | None
) -> None:
    catalog = {
        "deepseek-ai/DeepSeek-V4-Flash-0731": {
            "id": "deepseek-ai/DeepSeek-V4-Flash-0731",
            "api": "openai-completions",
            "maxTokens": 10,
        }
    }
    route = {"data": {"providers": [] if provider is None else [provider]}}
    responses = iter([catalog, route])
    monkeypatch.setattr(models, "fetch_json", lambda _url: next(responses))
    with pytest.raises(ValueError, match="does not offer live tool use"):
        models.pinned_model(REQUESTED)
