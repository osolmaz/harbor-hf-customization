"""Admit a model through OpenClaw's public native thinking-policy capture."""

import hashlib
import json
from typing import Any


def admit_policy(
    capture: dict[str, Any],
    catalog_bytes: bytes,
    model: str,
    api: str | None,
    thinking: str | None,
) -> dict[str, Any]:
    """Preserve the complete native row and policy; never invent supported levels."""
    if (
        capture.get("schema_version") != 1
        or capture.get("owner") != "api.runtime.agent.resolveThinkingPolicy"
        or capture.get("runtime") != "openclaw"
        or capture.get("model_name") != model
        or capture.get("catalog_sha256") != hashlib.sha256(catalog_bytes).hexdigest()
    ):
        raise ValueError("OpenClaw native policy identity or catalog digest differs")
    rows = json.loads(catalog_bytes)
    matches = [row for row in rows if f"{row.get('provider')}/{row.get('id')}" == model]
    if len(matches) != 1 or matches[0] != capture.get("catalog_entry"):
        raise ValueError("OpenClaw native policy requires one exact catalog row")
    row = matches[0]
    if row.get("api") != api or row.get("nativeRuntime") not in (None, "openclaw"):
        raise ValueError("OpenClaw native catalog API or runtime differs")
    levels = capture.get("policy", {}).get("levels", [])
    ids = [level.get("id") for level in levels]
    if (
        not levels
        or any(not level or level != level.strip() for level in ids)
        or len(ids) != len(set(ids))
        or any(not level.get("label") for level in levels)
        or thinking not in ids
    ):
        raise ValueError("Requested thinking is absent from the native OpenClaw policy")
    return {
        "model_name": model,
        "requested_thinking": thinking,
        "native_policy": capture,
        "provider_effective_thinking": None,
    }
