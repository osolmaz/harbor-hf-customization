"""Check the selected versions through Harbor's native construction and setup."""

from contextlib import nullcontext
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import AsyncMock

import pytest
from harbor.agents.factory import AgentFactory
from harbor.agents.installed.base import BaseInstalledAgent
from harbor.models.task.config import NetworkMode
from harbor.models.trial.config import AgentConfig

from anonbench1_adapters import (
    Anonbench1ClaudeCode,
    Anonbench1Codex,
    Anonbench1Pi,
    attested,
)
from anonbench1_adapters.anonbench1_pins import NODE_VERSION

RUNTIMES = [
    (Anonbench1Pi, "1.3.0"),
    (Anonbench1Codex, "0.163.0"),
    (Anonbench1ClaudeCode, "2.1.292"),
]


def offline_env():
    """A protocol peer with no network calls or model requests."""
    return SimpleNamespace(
        exec=AsyncMock(),
        upload_file=AsyncMock(),
        network_policy=SimpleNamespace(network_mode=NetworkMode.PUBLIC),
        scoped_exec_env=lambda env: nullcontext(),
    )


def make_agent(cls, tmp_path: Path, **kwargs):
    env = {"OPENAI_API_KEY": "test-key", "OPENAI_BASE_URL": "https://example.com/v1"}
    if cls is Anonbench1ClaudeCode:
        env = {
            "ANTHROPIC_API_KEY": "test-key",
            "ANTHROPIC_BASE_URL": "https://example.com",
        }
    return cls(
        logs_dir=tmp_path,
        model_name="openai/example/model:provider",
        extra_env=env,
        **kwargs,
    )


@pytest.mark.parametrize(("cls", "version"), RUNTIMES)
def test_default_version_is_selected(cls, version, tmp_path):
    agent = make_agent(cls, tmp_path)
    assert agent._version == version


@pytest.mark.parametrize(("cls", "version"), RUNTIMES)
def test_other_versions_fail_before_setup(cls, version, tmp_path):
    with pytest.raises(ValueError, match=f"requires version {version}"):
        make_agent(cls, tmp_path, version="0.0.0")


@pytest.mark.parametrize(("cls", "version"), RUNTIMES)
def test_native_agent_factory_keeps_the_selected_adapter(cls, version, tmp_path):
    config = AgentConfig(
        import_path=f"anonbench1_adapters:{cls.__name__}",
        model_name="openai/example/model:provider",
        kwargs={"version": version},
    )
    AgentFactory.run_preflight(config)
    agent = AgentFactory.create_agent_from_config(config, logs_dir=tmp_path)
    assert isinstance(agent, cls)
    assert agent._version == version


@pytest.mark.parametrize(("cls", "version"), RUNTIMES)
async def test_setup_attests_after_native_setup(cls, version, tmp_path, monkeypatch):
    setup = AsyncMock()
    attestation = AsyncMock()
    monkeypatch.setattr(BaseInstalledAgent, "setup", setup)
    monkeypatch.setattr(attested, "attest_version", attestation)
    agent = make_agent(cls, tmp_path)
    env = offline_env()
    await agent.setup(env)
    setup.assert_awaited_once_with(env)
    attestation.assert_awaited_once()
    assert attestation.call_args.args[:2] == (agent, env)


def test_pi_uses_its_own_node_pin(tmp_path):
    agent = make_agent(Anonbench1Pi, tmp_path)
    assert NODE_VERSION == "26.11.1"
    assert "26.11.1" in agent.get_version_command()
    assert "1.3.0" in agent.get_version_command()


@pytest.mark.parametrize("name", ["Anonbench1OpenClaw"])
def test_held_harnesses_have_no_executable_export(name):
    with pytest.raises((ImportError, ValueError, AttributeError)):
        AgentFactory.run_preflight(
            AgentConfig(import_path=f"anonbench1_adapters:{name}")
        )


@pytest.mark.parametrize(
    "model", ["openai/example/model:provider", "example/model:provider"]
)
def test_claude_uses_native_hf_messages_without_model_fallback(model):
    from anonbench1_adapters.hf_connection import claude_environment

    original = {
        "OPENAI_BASE_URL": "https://router.huggingface.co/v1",
        "OPENAI_API_KEY": "test-key",
    }
    translated = claude_environment(original, model)
    assert translated["ANTHROPIC_BASE_URL"] == "https://router.huggingface.co"
    assert translated["ANTHROPIC_API_KEY"] == "test-key"
    assert translated["ANTHROPIC_AUTH_TOKEN"] == "test-key"
    for key in [
        "ANTHROPIC_DEFAULT_OPUS_MODEL",
        "ANTHROPIC_DEFAULT_SONNET_MODEL",
        "ANTHROPIC_DEFAULT_HAIKU_MODEL",
        "CLAUDE_CODE_SUBAGENT_MODEL",
    ]:
        assert translated[key] == "example/model:provider"
    assert "ANTHROPIC_API_KEY" not in original
    assert "OPENAI_API_KEY" not in translated


@pytest.mark.parametrize(
    ("env", "model", "error"),
    [
        (
            {"OPENAI_BASE_URL": "https://example.com/v1", "OPENAI_API_KEY": "test-key"},
            "example/model:provider",
            "reviewed HF endpoint",
        ),
        (
            {"OPENAI_BASE_URL": "https://router.huggingface.co/v1"},
            "example/model:provider",
            "inference credential",
        ),
        (
            {
                "OPENAI_BASE_URL": "https://router.huggingface.co/v1",
                "OPENAI_API_KEY": "test-key",
            },
            "example/model",
            "explicit provider",
        ),
    ],
)
def test_claude_router_rejects_unbound_inputs(env, model, error):
    from anonbench1_adapters.hf_connection import claude_environment

    with pytest.raises(ValueError, match=error):
        claude_environment(env, model)


def test_claude_constructor_preserves_the_explicit_provider(tmp_path):
    agent = Anonbench1ClaudeCode(
        logs_dir=tmp_path,
        model_name="openai/example/model:provider",
        extra_env={
            "OPENAI_BASE_URL": "https://router.huggingface.co/v1",
            "OPENAI_API_KEY": "test-key",
        },
    )
    assert agent._resolved_model_name() == "example/model:provider"
    assert agent.extra_env["ANTHROPIC_BASE_URL"] == "https://router.huggingface.co"


def test_quoted_prices_feed_native_cost_accounting(tmp_path, monkeypatch):
    import litellm
    from harbor.models.trajectories import Metrics, Step

    monkeypatch.setattr(litellm, "model_cost", dict(litellm.model_cost))
    quote = {"input": 1, "output": 2, "cache_read": 1, "cache_write": 1}
    codex = make_agent(Anonbench1Codex, tmp_path / "codex", price=quote)
    assert codex._compute_cost_from_pricing(1000, 300, 0, 0) == pytest.approx(0.0016)
    claude = make_agent(Anonbench1ClaudeCode, tmp_path / "claude", price=quote)
    step = Step(
        step_id=1,
        source="agent",
        message="test",
        model_name="example/model:provider",
        metrics=Metrics(prompt_tokens=1000, completion_tokens=300),
    )
    assert claude._estimate_step_costs([step]) == pytest.approx(0.0016)
    assert (tmp_path / "codex/pricing.json").is_file()
    assert (tmp_path / "claude/pricing.json").is_file()


def test_quoted_prices_reject_conflicts_and_implicit_providers(tmp_path, monkeypatch):
    import litellm

    from anonbench1_adapters.anonbench1_pi import ModelPrice
    from anonbench1_adapters.pricing import register_quoted_price

    monkeypatch.setattr(litellm, "model_cost", {})
    price = ModelPrice(input=1, output=2, cache_read=1, cache_write=1)
    with pytest.raises(ValueError, match="explicit model and provider"):
        register_quoted_price("example/model", price, tmp_path)
    register_quoted_price("example/model:provider", price, tmp_path)
    register_quoted_price("example/model:provider", price, tmp_path)
    with pytest.raises(ValueError, match="conflicts"):
        register_quoted_price(
            "example/model:provider", price.model_copy(update={"input": 2}), tmp_path
        )


async def test_native_codex_command_keeps_the_full_hf_model_id(tmp_path, monkeypatch):
    from harbor.models.agent.context import AgentContext

    from anonbench1_adapters.observed_codex import ObservedCodex

    peer = AsyncMock()
    monkeypatch.setattr(ObservedCodex, "exec_as_agent", peer)
    agent = Anonbench1Codex(
        logs_dir=tmp_path,
        model_name="openai/example/model:provider",
        extra_env={
            "OPENAI_BASE_URL": "https://router.huggingface.co/v1",
            "OPENAI_API_KEY": "test-key",
        },
    )
    env = offline_env()
    env.default_user = None
    await agent.run(
        "Keep the literal --model model:provider in this instruction",
        env,
        AgentContext(),
    )
    command = next(
        c.kwargs["command"]
        for c in peer.call_args_list
        if "codex exec " in c.kwargs.get("command", "")
    )
    flags, separator, instruction = command.partition(" -- ")
    assert separator
    assert "--model example/model:provider " in flags
    assert "literal --model model:provider" in instruction


@pytest.mark.parametrize("command", ["codex --version", "mkdir -p /tmp/example"])
def test_codex_connection_leaves_other_native_commands_intact(command):
    from anonbench1_adapters.hf_connection import codex_command

    assert codex_command(command, None) == command


@pytest.mark.parametrize(
    ("suffix", "model", "message"),
    [
        ("--model model -- text", "example/model", "explicit provider"),
        ("--json -- text", "example/model:provider", "command shape changed"),
    ],
)
def test_codex_connection_rejects_changed_command_shapes(suffix, model, message):
    from anonbench1_adapters.hf_connection import codex_command

    prefix = "if [ -s ~/.nvm/nvm.sh ]; then . ~/.nvm/nvm.sh; fi; codex exec "
    with pytest.raises(ValueError, match=message):
        codex_command(prefix + suffix, model)
