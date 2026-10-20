"""Dedicated host model process, using the public pinned Hermes AIAgent API."""

import json
import os
import signal
import sys
import threading
import time
from dataclasses import asdict
from pathlib import Path


def main():
    bootstrap = json.loads(sys.stdin.readline())
    mode = bootstrap.get("execution_mode")
    if mode not in ("qualification", "scored") or bootstrap.get("qualification_only") is not (mode == "qualification"):
        raise ValueError("An explicit consistent qualification or scored mode is required")
    sys.dont_write_bytecode = True
    adapter = Path(__file__).resolve().parent
    sys.path.insert(0, str(adapter))
    sys.path.insert(0, str(adapter.parent.parent))
    from anonbench1_adapters.anonbench1_pins import NODE_VERSION
    from anonbench1_adapters.hermes_source import (
        HostHermesRuntime,
        TaskHermesRuntime,
        UpstreamHermesSource,
        file_sha256,
        verify_source,
    )

    source = Path(bootstrap["source"])
    identity_path = Path(bootstrap["source_identity"])
    if not source.is_absolute() or not identity_path.is_absolute():
        raise ValueError("Upstream runtime admission requires explicit absolute source/identity paths")
    identity = UpstreamHermesSource.load(
        identity_path, bootstrap["source_identity_sha256"],
    )
    verify_source(source, identity)
    host_runtime = HostHermesRuntime(**bootstrap["host_runtime"])
    if (
        host_runtime.source != str(source) or host_runtime.source_identity != str(identity_path)
        or host_runtime.source_identity_sha256 != bootstrap["source_identity_sha256"]
        or file_sha256(Path(sys.executable)) != host_runtime.python_sha256
        or ".".join(map(str, sys.version_info[:3])) != host_runtime.python_version
        or sys.prefix == sys.base_prefix
    ):
        raise ValueError("Host process does not match the admitted Hermes venv/source identity")
    task_runtime = TaskHermesRuntime(**bootstrap["task_runtime"])
    if (
        task_runtime.source_identity_sha256 != bootstrap["source_identity_sha256"]
        or bootstrap["task_runtime_receipt"] != {
            "python_sha256": task_runtime.python_sha256,
            "python_version": task_runtime.python_version,
            "anydoc_version": "0.2.4",
            "node_sha256": task_runtime.node_sha256, "node_version": NODE_VERSION,
            "source_identity": asdict(identity),
        }
    ):
        raise ValueError("Task document runtime was not admitted against the host's reviewed source")
    try:
        run_host_agent(bootstrap, identity, task_runtime, host_runtime)
    finally:
        # Compare against the same admitted tree after native cleanup, including
        # constructor/run failures. An unchanged tree preserves the original error.
        verify_source(source, identity)


def run_host_agent(bootstrap, identity, task_runtime, host_runtime):
    # Preserve a private IPC descriptor, then move all native stdout (including
    # inherited subprocess stdout) to the host transcript instead of the wire.
    writer = os.fdopen(os.dup(1), "w", buffering=1)
    os.dup2(2, 1)
    sys.stdout = sys.stderr
    print(
        f"Hermes {identity.package_version}; upstream commit {identity.source_commit}; "
        f"source tree {identity.source_tree}; archive {identity.archive_sha256}",
        flush=True,
    )

    from anonbench1_adapters.offline_hermes_state import BoundaryAbort, TrialBoundary, construct_root, trial_context

    boundary = TrialBoundary(
        bootstrap["generation"], qualified_tools=frozenset(bootstrap.get("qualified_tools", ())),
    )
    boundary.toolsets = bootstrap["config"]["toolsets"]
    failure = None
    result = None
    cleanup_errors = []
    with trial_context(boundary):
        # No native import or constructor can run before trial authority exists.
        from anonbench1_adapters.hermes_source import task_runtime_receipt

        receipt = task_runtime_receipt(Path(bootstrap["source"]), identity, host_runtime.node)
        if receipt["node_sha256"] != host_runtime.node_sha256:
            raise ValueError("Host native Node bytes differ from their admitted identity")
        import hermes_cli

        if (
            hermes_cli.__version__ != identity.package_version
            or not Path(hermes_cli.__file__).resolve().is_relative_to(Path(bootstrap["source"]).resolve())
        ):
            raise RuntimeError("Imported Hermes does not match the admitted upstream source")
        from offline_hermes_native import HostTaskClient

        client = HostTaskClient(
            boundary, sys.stdin, writer, bootstrap["cwd"], task_runtime, bootstrap.get("native_snapshot"),
        )
        boundary.task_client = client

        def interrupt(signum, frame):
            boundary.trip("Host campaign interruption")
            raise BoundaryAbort("Host campaign interruption")

        signal.signal(signal.SIGTERM, interrupt)
        session_db = None
        try:
            from gateway.session_context import declare_stateless_channel
            from hermes_cli.plugins import discover_plugins
            from hermes_constants import resolve_reasoning_config
            from hermes_state_registry import acquire
            from run_agent import AIAgent

            discover_plugins()
            if not getattr(boundary, "plugin_ready", False):
                boundary.trip("Required Harbor plugin did not register")
                raise BoundaryAbort("Required Harbor plugin did not register")
            from tools.terminal_tool import register_container_alias, register_task_env_overrides

            # Constructor hints use session_id; startup prompt discovery uses
            # the turn's explicit task_id. Both select this same task owner.
            for task_id in (bootstrap["session_id"], bootstrap["task_id"]):
                register_task_env_overrides(task_id, {"env_type": "harbor_task", "cwd": bootstrap["cwd"]})
            register_container_alias(bootstrap["task_id"], bootstrap["session_id"])
            declare_stateless_channel()
            session_db = acquire()
            config = bootstrap["config"]
            model = config["model"]
            remaining = bootstrap["deadline"] - time.monotonic()
            if remaining <= 0:
                raise TimeoutError("Qualification allowance expired before native construction")
            root = construct_root(boundary, lambda: AIAgent(
                api_key=os.environ[model["key_env"]], base_url=model["base_url"],
                provider=model["provider"], requested_provider=model["provider"],
                api_mode=model["api_mode"], model=model["default"],
                enabled_toolsets=config["toolsets"],
                max_iterations=config.get("agent", {}).get("max_turns", sys.maxsize),
                reasoning_config=resolve_reasoning_config(config, model["default"]),
                quiet_mode=True, platform="cli", session_db=session_db,
                session_id=bootstrap["session_id"], cwd=bootstrap["cwd"],
                run_budget_seconds=remaining,
                checkpoints_enabled=False,
                clarify_callback=lambda question, **kwargs: "No user is available in this trial.",
            ))
            result = root.run_conversation(bootstrap["instruction"], task_id=bootstrap["task_id"])
            boundary.guard()
            if not result.get("final_response"):
                raise RuntimeError("Hermes did not produce a final response")
            print(result["final_response"], flush=True)
        # Retain execution/cancellation failure until owned cleanup completes.
        except BaseException as exc:  # noqa: BLE001
            failure = exc
            boundary.trip("Hermes host execution failed")
        finally:
            boundary.close_admission()

            def finalize():
                with boundary.owned_cleanup():
                    try:
                        if boundary.root is not None:
                            boundary.root.close()
                            boundary.mark_root_closed()
                    # Collect root-close failure while remaining owned cleanup continues.
                    except BaseException as exc:  # noqa: BLE001
                        cleanup_errors.append(exc)
                    try:
                        client.close()
                    # Collect task-close failure without abandoning session cleanup.
                    except BaseException as exc:  # noqa: BLE001
                        cleanup_errors.append(exc)
                    try:
                        if session_db is not None:
                            try:
                                # This is Hermes's native bounded wait, not a
                                # verdict that every provider worker settled.
                                from agent.title_generator import wait_for_title_upgrades

                                wait_for_title_upgrades(timeout=10)
                                flushed = session_db.flush_token_counts(timeout=5)
                                native = session_db.export_session_lineage(
                                    bootstrap["session_id"], include_compacted=True,
                                )
                                if native is not None:
                                    sessions = session_db.export_all(include_compacted=True)
                                    native["native_sessions"] = sessions
                                    native["task_tool_capabilities"] = client.native_snapshot
                                    native["supplied_tool_names"] = sorted(boundary.supplied_tools)
                                    native["supplied_tool_schemas"] = boundary.supplied_schemas
                                    native["observed_auxiliary_usage_by_session"] = {
                                        row["id"]: session_db.auxiliary_usage_by_task(row["id"])
                                        for row in sessions
                                    }
                                    # Retain the native observations, including
                                    # delegate/compression history. They are not
                                    # final totals and must never be summed as
                                    # though overlapping lineages were disjoint.
                                    native["harbor_execution"] = {
                                        "generation": bootstrap["generation"],
                                        "session_id": bootstrap["session_id"],
                                        "task_id": bootstrap["task_id"],
                                        "qualification_only": bootstrap["qualification_only"],
                                        "execution_mode": bootstrap["execution_mode"],
                                        "main_completed": bool(result and result.get("completed")),
                                        "fatal": boundary.fatal,
                                        "token_queue_flushed": flushed,
                                        "auxiliary_usage_complete": False,
                                        "usage_status": "unknown",
                                        "research_qualified": False,
                                    }
                                    path = Path(bootstrap["session_export"])
                                    descriptor = os.open(
                                        path, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600,
                                    )
                                    with os.fdopen(descriptor, "w", encoding="utf-8") as output:
                                        json.dump(native, output, ensure_ascii=False)
                                        output.write("\n")
                                # A flush timeout leaves accounting incomplete.
                                # Native DB close, boundary drain and the parent's
                                # whole process-group/task settlement still run.
                            # Retain export failure even if native DB close also fails.
                            except BaseException as exc:  # noqa: BLE001
                                cleanup_errors.append(exc)
                            finally:
                                session_db.close()
                    # Collect DB-close failure without abandoning accepted operations.
                    except BaseException as exc:  # noqa: BLE001
                        cleanup_errors.append(exc)
                    try:
                        boundary.drain(30)
                    # Drain failure remains a hard error after the cleanup owner joins.
                    except BaseException as exc:  # noqa: BLE001
                        cleanup_errors.append(exc)

            # Hard interrupts can arrive repeatedly while the root or a native
            # batch/RPC worker unwinds. One owner retains the whole cleanup.
            cleanup = threading.Thread(target=finalize)
            cleanup.start()
            while cleanup.is_alive():
                try:
                    cleanup.join(timeout=0.2)
                # Repeated interruption must not abandon the cleanup thread.
                except BaseException:  # noqa: BLE001
                    boundary.trip("Interrupted during owned cleanup")
            if cleanup_errors:
                raise RuntimeError("Hermes cleanup did not confirm settlement") from cleanup_errors[0]
            try:
                boundary.finish()
            # Keep the completion refusal authoritative and retain its execution cause.
            except BaseException as exc:
                if failure is not None:
                    raise exc from failure
                raise
        if failure is not None:
            raise RuntimeError("Hermes execution failed") from failure


if __name__ == "__main__":
    main()
