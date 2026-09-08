"""Configure Pi's native provider with authoritative model and HF price data."""

import json
from urllib.parse import quote
from urllib.request import Request, urlopen

from harbor_pi_code_mode.values import count, number, record

ROUTER = "https://router.huggingface.co/v1"
CATALOG = "https://pi.dev/api/models/providers/huggingface"


def fetch_json(url: str) -> object:
    request = Request(
        url,
        headers={
            "Accept": "application/json",
            "User-Agent": "harbor-custom-harnesses/0.1",
        },
    )
    with urlopen(request, timeout=12) as response:
        body = response.read(8 * 1024 * 1024 + 1)
    if len(body) > 8 * 1024 * 1024:
        raise ValueError("Model metadata exceeds its limit")
    return json.loads(body)


def pinned_model(requested: str) -> dict[str, object]:
    route, separator, model_id = requested.partition("/")
    base_id, provider_separator, provider_id = model_id.rpartition(":")
    if (
        route not in {"openai", "huggingface"}
        or not separator
        or not provider_separator
        or not base_id
        or not provider_id
    ):
        raise ValueError("Use an explicit HF model and provider")
    catalog = fetch_json(CATALOG)
    if not isinstance(catalog, list):
        raise ValueError("Pi model catalog is unavailable")
    base = next(
        (record(item) for item in catalog if record(item).get("id") == base_id), None
    )
    if base is None or base.get("api") != "openai-completions":
        raise ValueError("The requested model is not in Pi's chat-completions catalog")
    data = record(
        record(fetch_json(f"{ROUTER}/models/{quote(base_id, safe='/')}"))["data"]
    )
    providers = data.get("providers")
    if not isinstance(providers, list):
        raise ValueError("HF provider metadata is unavailable")
    provider = next(
        (
            record(item)
            for item in providers
            if record(item).get("provider") == provider_id
        ),
        None,
    )
    if (
        provider is None
        or provider.get("status") != "live"
        or provider.get("supports_tools") is not True
    ):
        raise ValueError("The requested provider does not offer live tool use")
    pricing = record(provider.get("pricing"))
    context = count(provider.get("context_length"))
    result = {
        key: value
        for key, value in base.items()
        if key
        not in {"provider", "baseUrl", "id", "cost", "contextWindow", "maxTokens"}
    }
    result.update(
        {
            "id": model_id,
            "cost": {
                "input": number(pricing.get("input")),
                "output": number(pricing.get("output")),
                "cacheRead": 0,
                "cacheWrite": 0,
            },
            "contextWindow": context,
            "maxTokens": min(count(base.get("maxTokens")), context),
        }
    )
    if context == 0 or result["maxTokens"] == 0:
        raise ValueError("Model limits must be positive")
    return result
