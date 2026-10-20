"""Persistent native web/browser/TTS handlers inside one Harbor task family.

The finite endpoint publishes requests; this process alone dispatches them.
Its existing Linux supervisor owns cancellation and descendant settlement.
"""

import hashlib
import importlib.metadata
import importlib.util
import json
import os
import signal
import sys
import time
from pathlib import Path


def main():
    os.umask(0o077)
    sys.dont_write_bytecode = True
    boot = json.loads(sys.stdin.read(1_000_001))
    root = Path(boot["root"])
    worker = boot["worker"]
    slot = root / worker
    spec = importlib.util.spec_from_file_location("harbor_task_endpoint", boot["helper"])
    endpoint = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(endpoint)
    endpoint.generation(root, boot["generation"])
    if not endpoint.ID.fullmatch(worker) or not (slot / "accepted.json").exists():
        raise RuntimeError("Native worker has no owned family")
    sys.path.insert(0, boot["admission"])
    from anonbench1_adapters.hermes_source import UpstreamHermesSource, verify_source

    identity = UpstreamHermesSource.load(
        Path(boot["runtime"]["source_identity"]), boot["runtime"]["source_identity_sha256"],
    )
    source = Path(boot["runtime"]["source"])
    verify_source(source, identity)
    try:
        run_worker(boot, endpoint, identity)
    finally:
        verify_source(source, identity)


def run_worker(boot, endpoint, identity):
    from anonbench1_adapters.hermes_source import file_sha256

    root, worker = Path(boot["root"]), boot["worker"]
    slot = root / worker
    config_path = Path(boot["config_path"])
    if not config_path.is_absolute() or file_sha256(config_path) != boot["config_sha256"]:
        raise RuntimeError("Native task tool configuration was not admitted")
    import yaml

    config_bytes = config_path.read_bytes()
    config = yaml.safe_load(config_bytes)
    if config.get("terminal", {}).get("env") != "local":
        raise RuntimeError("The native worker requires task-local terminal configuration")
    # This is a tool-only profile, separately staged/reviewed by the installer.
    # Never copy the host model configuration, plugin or credentials into it.
    if any(key in config for key in ("model", "auxiliary", "provider", "providers", "mcp_servers")):
        raise RuntimeError("Task tool profile contains unadmitted inference/MCP configuration")
    home = slot / "home"
    home.mkdir(mode=0o700)
    (home / "config.yaml").write_bytes(config_bytes)
    os.environ["HERMES_HOME"] = str(home)
    os.environ["TERMINAL_ENV"] = "local"
    os.environ["TERMINAL_CWD"] = boot["cwd"]
    os.environ["HERMES_DISABLE_LAZY_INSTALLS"] = "1"
    os.environ.pop("HERMES_LAZY_INSTALL_TARGET", None)
    os.chdir(boot["cwd"])
    # Inputs name already-staged binaries. This path neither installs nor
    # changes the task's ordinary PATH/Python, and refuses an npx-only route.
    prerequisites = {}
    for name, item in boot["executables"].items():
        path = Path(item["path"])
        if not path.is_absolute() or file_sha256(path) != item["sha256"]:
            raise RuntimeError("Native task executable differs from its admitted bytes")
        prerequisites[name] = {"path": str(path), "sha256": item["sha256"]}
    names = frozenset(boot["names"])
    from tools.registry import discover_builtin_tools, registry
    from tools.terminal_scope import terminal_scope

    discover_builtin_tools()
    for name in names:
        entry = registry.get_entry(name)
        if entry is None or entry.toolset not in {"web", "browser", "browser-cdp", "browser-use", "tts"}:
            raise RuntimeError("Native request is outside the web/browser/TTS dispatch owner")
    requests, responses = slot / "requests", slot / "responses"
    requests.mkdir(mode=0o700, exist_ok=True)
    responses.mkdir(mode=0o700)
    seen = set()
    stopping = False

    def stop(signum, frame):
        nonlocal stopping
        stopping = True

    signal.signal(signal.SIGTERM, stop)
    scope = {"TERMINAL_ENV": "local", "TERMINAL_CWD": boot["cwd"]}
    try:
        with terminal_scope(scope):
            entries, browser_runtime, model_tools = native_tool_snapshot(
                names, prerequisites, toolsets=boot["toolsets"],
            )
            tts_runtime = admit_tts_runtime() if any(entries[name]["toolset"] == "tts" for name in names) else {}
            snapshot = {
                "worker": worker, "generation": boot["generation"],
                "config_sha256": boot["config_sha256"], "entries": entries,
                "executables": prerequisites, "source_tree": identity.source_tree,
                "terminal_env": "local", "browser_runtime": browser_runtime,
                "tts_runtime": tts_runtime,
                "toolsets": boot["toolsets"], "model_tools": model_tools,
                "native_budget_owner": "registry handlers and their unchanged arguments/configuration",
            }
            while not stopping and not (root / "closed").exists() and not (slot / "stop").exists():
                pending = [endpoint.read(path) for path in requests.glob("*.json") if path.stem not in seen]
                for item in sorted(pending, key=lambda value: value["sequence"]):
                    if stopping or (root / "closed").exists() or (slot / "stop").exists():
                        break
                    request = item["request"]
                    operation = request["operation"]
                    if request["worker"] != worker or not endpoint.ID.fullmatch(operation):
                        raise RuntimeError("Native dispatch identity mismatch")
                    seen.add(operation)
                    if request["kind"] == "snapshot":
                        value = snapshot
                    elif request["kind"] == "tool":
                        name = request["name"]
                        if name not in entries or not entries[name]["available"]:
                            raise RuntimeError("Requested native task capability is unqualified")
                        if entries[name]["gap"] or browser_runtime.get("missing"):
                            raise RuntimeError("Required native runtime/capability is unqualified")
                        if entries[name]["toolset"].startswith("browser"):
                            current = admit_browser_runtime(prerequisites)
                            if current != browser_runtime or current["missing"]:
                                raise RuntimeError("Admitted native browser runtime changed; no lazy repair is allowed")
                        if entries[name]["toolset"] == "tts":
                            current = admit_tts_runtime()
                            if current != tts_runtime or current["missing"]:
                                raise RuntimeError("Selected native TTS runtime changed or is missing")
                        # Whole handler owns URL safety, caching, rescue,
                        # browser state, artifacts and task-local file://.
                        value = registry.dispatch(name, request["args"], **request["context"])
                    else:
                        raise RuntimeError("Unknown native dispatch operation")
                    response = {"worker": worker, "operation": operation, "result": value}
                    if len(json.dumps(response).encode()) > endpoint.MAX_OUTPUT:
                        raise RuntimeError("Native result exceeds the admitted transport bound")
                    endpoint.publish(responses / (operation + ".json"), response)
                time.sleep(0.02)
    finally:
        # Native lifecycle is best effort; only the enclosing Harbor family
        # ledger can acknowledge that the worker and every descendant settled.
        from tools.browser_tool_lifecycle import cleanup_all_browsers

        cleanup_all_browsers()


def admit_tts_runtime():
    """Admit the selected native Edge closure without triggering lazy installation."""
    from tools.lazy_deps import _allow_lazy_installs
    from tools.tts_tool import (
        _get_provider,
        _load_tts_config,
        _resolve_command_provider_config,
        check_tts_requirements,
    )

    config = _load_tts_config()
    provider = _get_provider(config)
    missing = []
    lazy = _allow_lazy_installs()
    if lazy:
        missing.append("native lazy installation must be disabled")
    if provider != "edge" or _resolve_command_provider_config(provider, config) is not None:
        missing.append("selected TTS provider has no admitted campaign runtime")
    try:
        edge = importlib.metadata.version("edge-tts")
    except importlib.metadata.PackageNotFoundError:
        edge = None
    if edge != "7.2.7":
        missing.append("edge-tts==7.2.7")
    # A missing default is an admission gap, never evidence of intentional
    # filtering or permission to switch to NeuTTS/another provider.
    if not missing and not check_tts_requirements():
        missing.append("native TTS availability check failed")
    return {"provider": provider, "edge_tts_version": edge, "lazy_installs": lazy,
            "config_sha256": hashlib.sha256(json.dumps(config, sort_keys=True).encode()).hexdigest(),
            "missing": missing}


def admit_browser_runtime(prerequisites):
    """Bind dependencies to the native selected mode, not the catalog union."""
    from hermes_constants import agent_browser_runnable
    from anonbench1_adapters.hermes_source import file_sha256
    from tools.browser_camofox import is_camofox_mode
    from tools.browser_lightpanda import find_lightpanda_binary
    from tools.browser_tool_cdp import _get_cdp_override_raw
    from tools.browser_tool_cloud import _get_cloud_provider
    from tools.browser_tool_install import _find_agent_browser
    from tools.browser_tool_lightpanda_fallback import _using_lightpanda_engine
    from tools.browser_use_cli import _find_cli, get_browser_backend, is_browser_use_cli_mode

    browser_use = is_browser_use_cli_mode()
    camofox = is_camofox_mode()
    cdp = bool(_get_cdp_override_raw() or (
        browser_use and (os.environ.get("BU_CDP_WS") or os.environ.get("BU_CDP_URL"))
    ))
    cloud = not camofox and not cdp and _get_cloud_provider() is not None
    required = [] if camofox else ["browser-use" if browser_use else "agent-browser"]
    # The frozen installer provisions Browser Use as the default. Its native
    # missing-install fallback is not an admitted campaign profile change.
    if not camofox and not get_browser_backend():
        required.append("browser-use")
    # Native Browser Use can attach directly to configured task CDP. Its
    # managed local Chrome route does use agent-browser; Lightpanda does not.
    if not camofox and not cdp and not cloud:
        if _using_lightpanda_engine():
            required.append("lightpanda")
        else:
            required.extend(["agent-browser", "chromium"])
    required = sorted(set(required))
    missing = []
    for name in required:
        item = prerequisites.get(name)
        path = Path(item["path"]) if item else None
        if not item or not path.is_file() or not os.access(path, os.X_OK):
            missing.append(name)
            continue
        if file_sha256(path) != item["sha256"]:
            missing.append(name)
            continue
        if name == "chromium":
            os.environ["AGENT_BROWSER_EXECUTABLE_PATH"] = str(path)
        else:
            # Reuse the pinned native lookup order, including managed bins.
            # Admission rejects uvx/npx fallback without executing it.
            if name == "browser-use":
                command = _find_cli()
                found = command[0] if command and len(command) == 1 else None
            elif name == "lightpanda":
                found = find_lightpanda_binary()
            else:
                try:
                    found = _find_agent_browser(validate=False)
                except FileNotFoundError:
                    found = None
            if not found or Path(found).resolve() != path.resolve():
                missing.append(name)
                continue
            if name == "agent-browser" and not agent_browser_runnable(found):
                missing.append(name)
    return {
        "mode": "camofox" if camofox else "browser-use" if browser_use else "conventional",
        "cloud_configured": cloud,
        "cdp_configured": cdp, "required": required, "missing": missing,
    }


def native_tool_snapshot(names, prerequisites, *, toolsets):
    """Native checks/dynamic definitions decide the surface; missing runtime is separate."""
    from tools.registry import registry

    has_browser = any(registry.get_entry(name).toolset.startswith("browser") for name in names)
    runtime = admit_browser_runtime(prerequisites) if has_browser else {}
    tts = admit_tts_runtime() if any(registry.get_entry(name).toolset == "tts" for name in names) else {}
    entries = {}
    for name in sorted(names):
        entry = registry.get_entry(name)
        definitions = registry.get_definitions({name}, quiet=True)
        available = bool(definitions)
        gap = "Task-side auxiliary vision is unqualified" if name == "browser_vision" and available else None
        if entry.toolset == "tts" and tts["missing"]:
            gap = "Selected native TTS runtime is unqualified"
        entries[name] = {
            "schema": entry.schema, "toolset": entry.toolset,
            "available": available, "gap": gap, "definitions": definitions,
            "filter": None if available else "native_check_fn",
        }
    from model_tools import get_tool_definitions

    # Full-profile native finalization owns cross-tool rewrites, sanitization
    # and tool search. Raw capability evidence above remains independent.
    model_tools = {
        tool["function"]["name"]: tool
        for tool in get_tool_definitions(enabled_toolsets=toolsets, quiet_mode=True)
    }
    return entries, runtime, model_tools


if __name__ == "__main__":
    main()
