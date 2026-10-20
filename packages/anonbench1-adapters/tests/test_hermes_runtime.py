"""Native Hermes packaging checks with no model requests."""

from contextlib import nullcontext
from types import SimpleNamespace
from unittest.mock import AsyncMock

import pytest
from harbor.agents.factory import AgentFactory
from harbor.models.task.config import NetworkMode
from harbor.models.trial.config import AgentConfig

from anonbench1_adapters import Anonbench1Hermes, attested
from anonbench1_adapters.anonbench1_hermes import Anonbench1Hermes as CopiedHermes


def options():
    return {
        "version": "v2026.9.24",
        "model_api": "openai-completions",
        "reasoning_effort": "medium",
        "reasoning_support": {
            "model_id": "example/model:provider",
            "provider_efforts": ["low", "medium", "high", "xhigh", "max"],
            "source": "synthetic protocol fixture",
        },
    }


def agent(tmp_path, **overrides):
    return Anonbench1Hermes(
        logs_dir=tmp_path,
        model_name="openai/example/model:provider",
        extra_env={
            "OPENAI_API_KEY": "test-key",
            "OPENAI_BASE_URL": "https://example.com/v1",
        },
        **{**options(), **overrides},
    )


def test_hermes_factory_and_native_custom_endpoint(tmp_path):
    config = AgentConfig(
        import_path="anonbench1_adapters:Anonbench1Hermes",
        model_name="openai/example/model:provider",
        kwargs=options(),
    )
    AgentFactory.run_preflight(config)
    assert isinstance(
        AgentFactory.create_agent_from_config(config, logs_dir=tmp_path),
        Anonbench1Hermes,
    )
    selected = agent(tmp_path).build_config()
    assert selected["model"]["provider"] == "custom"
    assert selected["model"]["api_mode"] == "chat_completions"
    assert selected["model"]["default"] == "example/model:provider"
    assert selected["agent"]["reasoning_effort"] == {
        "enabled": True,
        "effort": "medium",
    }


def test_hermes_rejects_different_version_and_offline_scoring(tmp_path):
    with pytest.raises(ValueError, match="requires version v2026.9.24"):
        agent(tmp_path, version="v2026.9.20")
    selected = agent(tmp_path)
    environment = SimpleNamespace(
        network_policy=SimpleNamespace(network_mode=NetworkMode.NO_NETWORK)
    )
    with pytest.raises(RuntimeError, match="NO_NETWORK"):
        selected._require_execution_route(environment)
    environment.network_policy.network_mode = NetworkMode.PUBLIC
    selected._require_execution_route(environment)


async def test_hermes_setup_attests_after_native_setup(tmp_path, monkeypatch):
    setup, attest = AsyncMock(), AsyncMock()
    monkeypatch.setattr(CopiedHermes, "setup", setup)
    monkeypatch.setattr(attested, "attest_version", attest)
    selected = agent(tmp_path)
    environment = SimpleNamespace(scoped_exec_env=lambda env: nullcontext())
    await selected.setup(environment)
    setup.assert_awaited_once_with(environment)
    attest.assert_awaited_once_with(selected, environment, "hermes")


def test_hermes_installer_and_reasoning_payload_are_packaged(tmp_path):
    from pathlib import Path

    import anonbench1_adapters.anonbench1_hermes as copied

    selected = agent(tmp_path)
    command = selected.get_version_command()
    assert "f97608f178d1ffeca59860195ab7da295f7c8e5f" in command
    assert "26.11.1" in command
    plugin = Path(copied.__file__).parent / "hermes_harbor"
    assert (plugin / "plugin.yaml").is_file()
    assert (plugin / "reasoning.py").is_file()


def test_uploaded_plugin_uses_hermes_assigned_package_name(tmp_path, monkeypatch):
    import importlib.util
    import sys
    from pathlib import Path
    from types import ModuleType
    from unittest.mock import Mock

    import anonbench1_adapters.anonbench1_hermes as copied

    directory = Path(copied.__file__).parent / "hermes_harbor"
    spec = importlib.util.spec_from_file_location(
        "hermes_native_plugin",
        directory / "__init__.py",
        submodule_search_locations=[str(directory)],
    )
    assert spec is not None and spec.loader is not None
    plugin = importlib.util.module_from_spec(spec)
    monkeypatch.setitem(sys.modules, spec.name, plugin)
    reasoning = ModuleType(spec.name + ".reasoning")
    register = Mock(return_value=False)
    monkeypatch.setattr(reasoning, "register_reasoning", register, raising=False)
    monkeypatch.setitem(sys.modules, reasoning.__name__, reasoning)
    spec.loader.exec_module(plugin)
    context = object()
    plugin.register(context)
    register.assert_called_once_with(context)
