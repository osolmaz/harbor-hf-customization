"""Pinned bundle and native Harbor transport checks; no inference or task data."""

import hashlib
import json
from types import SimpleNamespace
from unittest.mock import AsyncMock

import pytest
from harbor.agents.installed.openclaw import OpenClaw

from anonbench1_adapters import native_openclaw as module


def make_agent(tmp_path, **kwargs):
    return module.Anonbench1OpenClaw(
        logs_dir=tmp_path,
        model_name="openai/example/model:provider",
        runtime="openclaw",
        extra_env={
            "OPENAI_BASE_URL": "https://example.com/v1",
            "OPENAI_API_KEY": "test-key",
        },
        **kwargs,
    )


def test_rejects_other_versions(tmp_path):
    with pytest.raises(ValueError, match="requires version"):
        make_agent(tmp_path, version="0.0.0")


def test_config_keeps_model_for_all_tools_and_code_mode(tmp_path):
    agent = make_agent(tmp_path, model_api="openai-completions", thinking="high")
    cfg = agent._build_full_openclaw_config()
    defaults = cfg["agents"]["defaults"]
    model = agent.model_name
    for field in ("model", "pdfModel", "imageModel"):
        assert defaults[field] == {"primary": model}
    assert defaults["utilityModel"] == model
    assert defaults["skipBootstrap"] is True
    assert defaults["models"][model] == {
        "agentRuntime": {"id": "openclaw"},
        "codeMode": True,
    }
    endpoint = cfg["models"]["providers"]["openai"]
    assert endpoint["baseUrl"] == "https://example.com/v1"
    assert endpoint["models"][0]["id"] == "example/model:provider"
    assert endpoint["models"][0]["reasoning"] is True
    assert endpoint["models"][0]["compat"]["supportsUsageInStreaming"] is True


def test_preserves_explicit_config_and_price(tmp_path):
    price = {"input": 1, "output": 2, "cache_read": 0.1, "cache_write": 0.2}
    agent = make_agent(
        tmp_path,
        model_api="openai-completions",
        price=price,
        context_window=10000,
        max_output_tokens=1000,
        thinking="off",
        code_mode=False,
        openclaw_config={"tools": {"profile": "full"}},
    )
    endpoint = {"models": [{"id": "other"}, {"id": agent.model_name}]}
    agent._register_endpoint_model(endpoint, "example/model:provider")
    entry = endpoint["models"][0]
    assert entry["reasoning"] is False
    assert entry["cost"] == {
        "input": 1,
        "output": 2,
        "cacheRead": 0.1,
        "cacheWrite": 0.2,
    }
    assert entry["contextWindow"] == 10000
    assert entry["maxTokens"] == 1000
    assert endpoint["models"][1:] == [{"id": "other"}]
    assert agent._build_full_openclaw_config()["tools"]["profile"] == "full"


def test_requires_wire_api_for_custom_endpoint(tmp_path):
    with pytest.raises(ValueError, match="needs the model_api"):
        make_agent(tmp_path)._build_full_openclaw_config()


def test_builtin_provider_does_not_add_custom_endpoint(tmp_path):
    agent = module.Anonbench1OpenClaw(
        logs_dir=tmp_path,
        model_name="anthropic/example",
        runtime="openclaw",
    )
    cfg = agent._build_full_openclaw_config()
    assert not cfg.get("models", {}).get("providers")


@pytest.mark.parametrize("prefix", ["", ". ~/.nvm/nvm.sh && nvm use 24 >/dev/null && "])
async def test_translates_only_native_runtime_selection(tmp_path, monkeypatch, prefix):
    execute = AsyncMock(return_value="result")
    monkeypatch.setattr(OpenClaw, "exec_as_agent", execute)
    environment = object()
    result = await make_agent(tmp_path).exec_as_agent(
        environment,
        prefix + "openclaw --version",
        timeout_sec=7,
    )
    assert result == "result"
    expected = (
        module.SELECT + " && openclaw --version" if prefix else "openclaw --version"
    )
    execute.assert_awaited_once_with(
        environment, command=expected, env=None, cwd=None, timeout_sec=7
    )


async def test_setup_attests_and_keeps_native_lifecycle(tmp_path, monkeypatch):
    setup, attest = AsyncMock(), AsyncMock()
    monkeypatch.setattr(OpenClaw, "setup", setup)
    monkeypatch.setattr(module, "attest_version", attest)
    agent, env = make_agent(tmp_path), object()
    await agent.setup(env)
    setup.assert_awaited_once_with(env)
    attest.assert_awaited_once_with(agent, env, "openclaw")
    assert agent._SETUP_CLI.endswith(" --skip-bootstrap")
    assert agent.run.__func__ is OpenClaw.run
    assert "v24.19.0" in agent.get_version_command()
    assert "1b58cb9a1b4f4353e1add63c8506db4d890962e8" in agent.get_version_command()


@pytest.mark.parametrize("valid", [True, False])
async def test_bundle_integrity_before_upload(tmp_path, monkeypatch, valid):
    bundle = tmp_path / "runtime.tar.gz"
    bundle.write_bytes(b"fake bundle: not executed")
    digest = (
        hashlib.sha256(bundle.read_bytes()).hexdigest() if valid else "bad-checksum"
    )
    bundle.with_suffix(".gz.sha256").write_text(digest)
    metadata = {"openclaw": "2026.10.5", "node": "24.19.0"}
    bundle.with_name("runtime.json").write_text(json.dumps(metadata))
    monkeypatch.setattr(module, "BUNDLE", bundle)
    env = SimpleNamespace(upload_file=AsyncMock())
    agent = make_agent(tmp_path)
    agent.exec_as_agent = AsyncMock()
    if not valid:
        with pytest.raises(ValueError, match="checksum mismatch"):
            await agent.install(env)
        env.upload_file.assert_not_awaited()
        return
    await agent.install(env)
    env.upload_file.assert_awaited_once_with(
        bundle, "/tmp/harbor-openclaw-runtime.tar.gz"
    )
    command = agent.exec_as_agent.call_args.kwargs["command"]
    assert digest in command
    assert "npm install" not in command
    assert "sha256sum -c -" in command
    assert json.loads((tmp_path / "runtime-bundle.json").read_text()) == metadata
