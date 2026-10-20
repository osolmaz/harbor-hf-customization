"""Supply explicit quotes to Harbor's existing token cost calculations."""

import json
from pathlib import Path

import litellm
from harbor.agents.installed.claude_code import ClaudeCodeOptions

from anonbench1_adapters.anonbench1_pi import ModelPrice
from anonbench1_adapters.observed_codex import ObservedCodexOptions


class QuotedCodexOptions(ObservedCodexOptions):
    price: ModelPrice | None = None


class QuotedClaudeCodeOptions(ClaudeCodeOptions):
    price: ModelPrice | None = None


def register_quoted_price(
    model_name: str | None, price: ModelPrice | None, logs_dir: Path
) -> None:
    """Register only the explicit route; conflicting quotes fail closed."""
    if price is None:
        return
    if not model_name or ":" not in model_name or model_name.endswith(":"):
        raise ValueError("Quoted pricing requires an explicit model and provider")
    route = model_name.removeprefix("openai/")
    row = {
        "input_cost_per_token": price.input / 1_000_000,
        "output_cost_per_token": price.output / 1_000_000,
        "cache_read_input_token_cost": price.cache_read / 1_000_000,
        "cache_creation_input_token_cost": price.cache_write / 1_000_000,
        "litellm_provider": "openai",
        "mode": "chat",
    }
    keys = {route, f"openai/{route}"}
    for key in keys:
        existing = litellm.model_cost.get(key)
        if existing and any(existing.get(k) != v for k, v in row.items()):
            raise ValueError("Quoted pricing conflicts with an existing model entry")
    litellm.register_model({key: dict(row) for key in keys})
    logs_dir.mkdir(parents=True, exist_ok=True)
    (logs_dir / "pricing.json").write_text(
        json.dumps(
            {
                "model": route,
                "usd_per_million_tokens": price.model_dump(),
                "cost_source": "explicit_quote_estimate",
            },
            indent=2,
        )
        + "\n"
    )
