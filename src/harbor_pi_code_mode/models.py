"""Configure Pi's native provider with authoritative model and HF price data."""

import json
from email.message import Message
from pathlib import Path
from typing import IO, override
from urllib.parse import quote, urlsplit
from urllib.request import HTTPRedirectHandler, Request, build_opener, urlopen

from harbor_pi_code_mode.values import count, number, record

ROUTER = "https://router.huggingface.co/v1"
CATALOG = "https://pi.dev/api/models/providers/huggingface"
BUNDLED_MODELS = record(
    json.loads(Path(__file__).with_name("model-catalog.json").read_text())
)


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


class _NoRedirect(HTTPRedirectHandler):
    @override
    def redirect_request(
        self,
        req: Request,
        fp: IO[bytes],
        code: int,
        msg: str,
        headers: Message,
        newurl: str,
    ) -> None:
        raise ValueError("The endpoint model list redirected")


def endpoint_model(
    requested: str, base_url: str, api_key: str, context_window: int, max_tokens: int
) -> dict[str, object]:
    """Bind a reviewed endpoint to the exact model advertised by its /models route.

    Host billing is hourly and not known to Pi's per-token cost calculator.
    """
    parts = urlsplit(base_url)
    if (
        parts.scheme != "https"
        or not parts.hostname
        or parts.username
        or parts.password
        or parts.query
        or parts.fragment
        or not parts.path.rstrip("/").endswith("/v1")
    ):
        raise ValueError("An endpoint requires a reviewed HTTPS /v1 base URL")
    if not requested.startswith("openai/") or not requested.removeprefix("openai/"):
        raise ValueError("An endpoint requires an explicit openai/<served-model-id>")
    if (
        isinstance(context_window, bool)
        or context_window <= 0
        or isinstance(max_tokens, bool)
        or max_tokens <= 0
        or max_tokens > context_window
    ):
        raise ValueError("Set positive endpoint limits, with output within context")
    model_id = requested.removeprefix("openai/")
    request = Request(
        f"{base_url.rstrip('/')}/models",
        headers={"Accept": "application/json", "Authorization": f"Bearer {api_key}"},
    )
    with build_opener(_NoRedirect()).open(request, timeout=12) as response:
        body = response.read(8 * 1024 * 1024 + 1)
    if len(body) > 8 * 1024 * 1024:
        raise ValueError("Endpoint model list exceeds its limit")
    listing = record(json.loads(body))
    available = listing.get("data")
    if not isinstance(available, list) or not any(
        record(item).get("id") == model_id for item in available
    ):
        raise ValueError("The reviewed endpoint did not advertise the requested model")
    return {
        "id": model_id,
        "api": "openai-completions",
        "reasoning": True,
        "input": ["text"],
        "contextWindow": context_window,
        "maxTokens": max_tokens,
        # Endpoint hosts are billed by elapsed time, not by token. This value is
        # not the host bill; a separate cumulative host limit is required.
        "cost": {"input": 0, "output": 0, "cacheRead": 0, "cacheWrite": 0},
    }


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
    base_value = BUNDLED_MODELS.get(base_id)
    if base_value is None:
        catalog = fetch_json(CATALOG)
        if not isinstance(catalog, dict):
            raise ValueError("Pi model catalog is unavailable")
        base_value = catalog.get(base_id, {})
    base = record(base_value)
    if base.get("id") != base_id or base.get("api") != "openai-completions":
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
                # HF's public metadata quotes input/output, not cache discounts.
                # Charge cached input at the input rate rather than inventing
                # free tokens. Pi's reported cost is a conservative estimate.
                "cacheRead": number(pricing.get("input")),
                "cacheWrite": number(pricing.get("input")),
            },
            "contextWindow": context,
            "maxTokens": min(count(base.get("maxTokens")), context),
        }
    )
    if context == 0 or result["maxTokens"] == 0:
        raise ValueError("Model limits must be positive")
    return result
