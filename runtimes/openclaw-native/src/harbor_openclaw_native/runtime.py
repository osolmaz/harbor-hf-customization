"""Install and start only the OpenClaw runtime bundled in the locked wheel."""

import asyncio
import json
import os
import shutil
from pathlib import Path

from harbor_openclaw_native.models import NIM_ENDPOINT, endpoint_base_url
from harbor_openclaw_native.process import OpenClawProcess
from harbor_openclaw_native.values import record

SOURCE_COMMIT = "ec9c1a13db8938e5a3eaa51fca2e981cde2395a9"
OPENCLAW_VERSION = "2026.9.5"


def payload_root() -> Path:
    return Path(__file__).parent / "payload"


def validate_payload() -> dict[str, object]:
    payload = payload_root()
    required = (
        payload / "bin/node",
        payload / "lib/node_modules/npm/bin/npm-cli.js",
        payload / "openclaw.tgz",
        payload / "package.json",
        payload / "package-lock.json",
        payload / "provenance.json",
    )
    if not all(path.is_file() for path in required):
        raise RuntimeError("The pinned OpenClaw runtime payload is missing")
    provenance = record(json.loads((payload / "provenance.json").read_text()))
    if (
        provenance.get("sourceCommit") != SOURCE_COMMIT
        or provenance.get("openclawVersion") != OPENCLAW_VERSION
        or provenance.get("nodeVersion") != "24.18.0"
    ):
        raise RuntimeError("The OpenClaw runtime provenance does not match the adapter")
    return provenance


async def install(settings: Path, logs: Path) -> Path:
    provenance = validate_payload()
    payload = payload_root()
    runtime = settings / "runtime"
    runtime.mkdir(parents=True, exist_ok=True)
    for name in ("openclaw.tgz", "package.json", "package-lock.json"):
        shutil.copy2(payload / name, runtime / name)
    node = payload / "bin/node"
    npm = payload / "lib/node_modules/npm/bin/npm-cli.js"
    process = await asyncio.create_subprocess_exec(
        str(node),
        str(npm),
        "ci",
        "--omit=dev",
        "--no-audit",
        "--no-fund",
        cwd=runtime,
        env={**os.environ, "PATH": f"{payload / 'bin'}:{os.environ.get('PATH', '')}"},
        stdout=asyncio.subprocess.PIPE,
        stderr=asyncio.subprocess.STDOUT,
    )
    output, _ = await process.communicate()
    (logs / "npm-install.log").write_bytes(output)
    if process.returncode != 0:
        raise RuntimeError(
            f"Pinned OpenClaw dependency install failed ({process.returncode})"
        )
    entrypoint = runtime / "node_modules/openclaw/openclaw.mjs"
    if not entrypoint.is_file():
        raise RuntimeError(
            "The pinned OpenClaw entrypoint is missing after installation"
        )
    (logs / "openclaw-source.json").write_text(
        json.dumps(provenance, sort_keys=True) + "\n"
    )
    return entrypoint


def write_config(
    path: Path,
    *,
    requested_model: str,
    model: dict[str, object],
    code_mode: str,
    mcp_servers: dict[str, dict[str, object]] | None = None,
) -> None:
    enabled = code_mode == "code"
    nim = endpoint_base_url() == NIM_ENDPOINT
    config = {
        "env": {"shellEnv": {"enabled": False}},
        "models": {
            "mode": "merge",
            "providers": {
                "openai": {
                    "baseUrl": endpoint_base_url(),
                    "api": "openai-completions",
                    "models": [model],
                }
            },
        },
        "agents": {
            "defaults": {
                "model": {"primary": requested_model},
                "models": {
                    requested_model: {
                        "agentRuntime": {"id": "openclaw"},
                        "codeMode": enabled,
                        **(
                            {"params": {"extra_body": {"reasoning_budget": 16384}}}
                            if nim
                            else {}
                        ),
                    }
                },
                "sandbox": {"mode": "off"},
                **({"experimental": {"localModelLean": True}} if nim else {}),
            }
        },
        "tools": {
            "profile": "coding",
            "fs": {"workspaceOnly": True},
            "exec": {"mode": "full"},
            "codeMode": {"enabled": enabled},
        },
        **({"mcp": {"servers": mcp_servers}} if mcp_servers else {}),
    }
    path.write_text(json.dumps(config, sort_keys=True) + "\n")


def command(
    *,
    entrypoint: Path,
    config: Path,
    state: Path,
    workspace: str,
    instruction: Path,
    requested_model: str,
    code_mode: str,
) -> tuple[list[str], dict[str, str]]:
    payload = payload_root()
    nim = endpoint_base_url() == NIM_ENDPOINT
    nim_flags: list[str] = ["--local-model-lean"] if nim else []
    args = [
        str(payload / "bin/node"),
        str(entrypoint),
        "agent",
        "exec",
        "--message-file",
        str(instruction),
        "--cwd",
        workspace,
        "--state-dir",
        str(state),
        "--config",
        str(config),
        "--model",
        requested_model,
        "--code-mode",
        code_mode,
        "--thinking",
        "high",
        *nim_flags,
        "--timeout",
        "0",
        "--json",
    ]
    env = {
        **os.environ,
        "PATH": f"{payload / 'bin'}:{os.environ.get('PATH', '')}",
        "OPENCLAW_TELEMETRY_DISABLED": "1",
        # Long tool-heavy runs register many resources; the default 10s cleanup
        # budget expires before settlement and OpenClaw then discards a completed
        # successful envelope as an error. Give cleanup room to settle.
        "OPENCLAW_AGENT_CLEANUP_TIMEOUT_MS": "120000",
    }
    return args, env


async def run(
    process: OpenClawProcess,
    *,
    args: list[str],
    workspace: str,
    env: dict[str, str],
    logs: Path,
) -> dict[str, object]:
    stdout = logs / "openclaw-envelope.json"
    stderr = logs / "openclaw.stderr.log"
    raw = await process.run(args, workspace, env, stdout, stderr)
    try:
        return record(json.loads(raw))
    except (json.JSONDecodeError, ValueError) as error:
        raise RuntimeError(
            "OpenClaw did not return its stable JSON envelope"
        ) from error
