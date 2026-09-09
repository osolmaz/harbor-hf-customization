"""Resolve one exact OpenClaw model row from public route metadata."""

import json
from urllib.parse import quote
from urllib.request import Request, urlopen

from harbor_openclaw_native.values import count, number, record

ROUTER = "https://router.huggingface.co/v1"
CATALOG = "https://pi.dev/api/models/providers/huggingface"
_MAX_METADATA_BYTES = 8 * 1024 * 1024


def fetch_json(url: str) -> object:
    headers = {
        "Accept": "application/json",
        "User-Agent": "harbor-custom-harnesses/0.1",
    }
    request = Request(url=url, headers=headers, method="GET")
    with urlopen(request, timeout=12) as response:
        body = response.read(_MAX_METADATA_BYTES + 1)
    if len(body) > _MAX_METADATA_BYTES:
        raise ValueError("Model metadata exceeds its limit")
    return json.loads(body)


def _parse_request(requested: str) -> tuple[str, str]:
    route, slash, routed_model = requested.partition("/")
    model, colon, provider = routed_model.rpartition(":")
    if route != "openai" or not slash or not colon or not model or not provider:
        raise ValueError("Use an explicit OpenAI-compatible HF model and provider")
    return model, provider


def _catalog_row(model_id: str) -> dict[str, object]:
    catalog = fetch_json(CATALOG)
    if not isinstance(catalog, dict):
        raise ValueError("The public model catalog is unavailable")
    row = record(catalog.get(model_id, {}))
    if row.get("id") != model_id or row.get("api") != "openai-completions":
        raise ValueError("The requested model is not in the chat-completions catalog")
    return row


def _provider_row(model_id: str, provider_id: str) -> dict[str, object]:
    url = f"{ROUTER}/models/{quote(model_id, safe='/')}"
    providers = record(record(fetch_json(url))["data"]).get("providers")
    if not isinstance(providers, list):
        raise ValueError("HF provider metadata is unavailable")
    matches = [
        record(candidate)
        for candidate in providers
        if record(candidate).get("provider") == provider_id
    ]
    if len(matches) != 1:
        raise ValueError("The requested provider does not offer live tool use")
    provider = matches[0]
    if provider.get("status") != "live" or provider.get("supports_tools") is not True:
        raise ValueError("The requested provider does not offer live tool use")
    return provider


def pinned_model(requested: str) -> tuple[str, dict[str, object]]:
    base_id, provider_id = _parse_request(requested)
    catalog = _catalog_row(base_id)
    provider = _provider_row(base_id, provider_id)
    pricing = record(provider.get("pricing"))
    context = count(provider.get("context_length"))
    maximum = min(count(catalog.get("maxTokens")), context)
    if min(context, maximum) == 0:
        raise ValueError("Model limits must be positive")
    input_price = number(pricing.get("input"))
    return requested, {
        "id": f"{base_id}:{provider_id}",
        "name": f"{base_id}:{provider_id}",
        "reasoning": catalog.get("reasoning") is True,
        "input": catalog.get("input", ["text"]),
        "cost": {
            "input": input_price,
            "output": number(pricing.get("output")),
            "cacheRead": input_price,
            "cacheWrite": input_price,
        },
        "contextWindow": context,
        "maxTokens": maximum,
    }
