"""Native catalog admission guards use fake captures, never model calls."""

import copy
import hashlib
import json

import pytest

from anonbench1_adapters.openclaw_policy import admit_policy


def fixture():
    row = {
        "provider": "openai",
        "id": "example/model:provider",
        "api": "openai-completions",
    }
    raw = (json.dumps([row]) + "\n").encode()
    capture = {
        "schema_version": 1,
        "owner": "api.runtime.agent.resolveThinkingPolicy",
        "runtime": "openclaw",
        "model_name": "openai/example/model:provider",
        "catalog_sha256": hashlib.sha256(raw).hexdigest(),
        "catalog_entry": row,
        "policy": {
            "levels": [{"id": "off", "label": "Off"}, {"id": "high", "label": "High"}]
        },
    }
    return capture, raw


def test_preserves_complete_native_policy_without_claiming_provider_effort():
    capture, raw = fixture()
    result = admit_policy(
        capture, raw, capture["model_name"], "openai-completions", "high"
    )
    assert result["native_policy"] == capture
    assert result["provider_effective_thinking"] is None


@pytest.mark.parametrize(
    "field,value",
    [
        ("schema_version", 0),
        ("owner", "other"),
        ("runtime", "codex"),
        ("model_name", "openai/other"),
        ("catalog_sha256", "0" * 64),
    ],
)
def test_rejects_identity_or_digest_drift(field, value):
    capture, raw = fixture()
    capture[field] = value
    with pytest.raises(ValueError, match="identity or catalog digest"):
        admit_policy(
            capture, raw, "openai/example/model:provider", "openai-completions", "high"
        )


@pytest.mark.parametrize(
    "variant", ["missing", "duplicate", "row_changed", "wrong_api", "wrong_runtime"]
)
def test_rejects_catalog_row_drift(variant):
    capture, raw = fixture()
    rows = json.loads(raw)
    if variant == "missing":
        rows = []
    if variant == "duplicate":
        rows *= 2
    if variant == "row_changed":
        capture["catalog_entry"] = {**capture["catalog_entry"], "id": "changed"}
    if variant in {"wrong_api", "wrong_runtime"}:
        key = "api" if variant == "wrong_api" else "nativeRuntime"
        rows[0][key] = "other"
        capture["catalog_entry"] = copy.deepcopy(rows[0])
    raw = json.dumps(rows).encode()
    capture["catalog_sha256"] = hashlib.sha256(raw).hexdigest()
    with pytest.raises(ValueError):
        admit_policy(capture, raw, capture["model_name"], "openai-completions", "high")


@pytest.mark.parametrize(
    "levels",
    [
        [],
        [{"id": "off", "label": "Off"}],
        [{"id": "high", "label": "High"}] * 2,
        [{"id": "high ", "label": "High"}],
        [{"id": "high"}],
    ],
)
def test_rejects_unsupported_or_malformed_native_levels(levels):
    capture, raw = fixture()
    capture["policy"]["levels"] = levels
    with pytest.raises(ValueError, match="Requested thinking"):
        admit_policy(capture, raw, capture["model_name"], "openai-completions", "high")
