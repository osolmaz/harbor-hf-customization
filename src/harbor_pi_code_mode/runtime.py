"""Start only the runtime bundled in the locked wheel, with isolated settings."""

import json
import os
from pathlib import Path
from typing import Literal

from harbor_pi_code_mode.models import ROUTER

CodeMode = Literal["direct", "code"]
Launcher = Literal["pi", "localpi"]

PROVIDER = "hf-pinned"
LIMIT_KEYS = ("contextWindow", "maxTokens")


def command(
    model: dict[str, object],
    settings: Path,
    logs: Path,
    code_mode: CodeMode,
    max_provider_requests: int | None = None,
    launcher: Launcher = "pi",
    continuation_limit: int = 0,
) -> tuple[list[str], dict[str, str]]:
    _validate(code_mode, launcher, continuation_limit)
    payload = Path(__file__).parent / "payload"
    node = payload / "bin/node"
    pi = payload / "node_modules/@earendil-works/pi-coding-agent/dist/cli.js"
    _require_payload(payload, code_mode, launcher)
    settings.mkdir(parents=True, exist_ok=True)
    if code_mode == "code":
        config_dir = settings / "config/pi-code-mode"
        config_dir.mkdir(parents=True, exist_ok=True)
        (config_dir / "config.json").write_text(json.dumps({"mode": "codex"}))
    env: dict[str, str] = {
        **os.environ,
        "PI_OFFLINE": "1",
        "PI_TELEMETRY": "0",
        "XDG_CONFIG_HOME": str(settings / "config"),
        "PATH": f"{payload / 'bin'}:{os.environ.get('PATH', '')}",
    }
    forwarded = _forwarded_flags(payload, code_mode, max_provider_requests, env)
    if launcher == "localpi":
        return _localpi_command(
            model,
            settings,
            logs,
            node,
            pi,
            payload / "node_modules/localpi/dist/src/cli/main.js",
            forwarded,
            continuation_limit,
            env,
        )
    return _pi_command(model, settings, logs, node, pi, forwarded, env)


def _validate(code_mode: str, launcher: str, continuation_limit: int) -> None:
    if code_mode not in {"direct", "code"}:
        raise ValueError("Code mode must be direct or code")
    if launcher not in {"pi", "localpi"}:
        raise ValueError("Launcher must be pi or localpi")
    if continuation_limit < 0:
        raise ValueError("The continuation limit cannot be negative")


def _require_payload(payload: Path, code_mode: CodeMode, launcher: Launcher) -> None:
    required = [
        payload / "bin/node",
        payload / "node_modules/@earendil-works/pi-coding-agent/dist/cli.js",
    ]
    if code_mode == "code":
        required.append(payload / "node_modules/pi-code-mode/dist/extension/index.js")
    if launcher == "localpi":
        required.append(payload / "node_modules/localpi/dist/src/cli/main.js")
    if not all(path.is_file() for path in required):
        raise RuntimeError("The pinned runtime payload is missing")


def _forwarded_flags(
    payload: Path,
    code_mode: CodeMode,
    max_provider_requests: int | None,
    env: dict[str, str],
) -> list[str]:
    """Flags that reach Pi directly or through localpi."""
    forwarded = [
        "--no-extensions",
        "--no-skills",
        "--no-prompt-templates",
        "--no-themes",
    ]
    if code_mode == "code":
        forwarded.extend(
            ["-e", str(payload / "node_modules/pi-code-mode/dist/extension/index.js")]
        )
    env.pop("HARBOR_PI_MAX_PROVIDER_REQUESTS", None)
    if max_provider_requests is not None:
        if max_provider_requests < 1:
            raise ValueError("The provider request limit must be positive")
        env["HARBOR_PI_MAX_PROVIDER_REQUESTS"] = str(max_provider_requests)
        forwarded.extend(["-e", str(Path(__file__).with_name("request-limit.mjs"))])
    return forwarded


def _pi_command(
    model: dict[str, object],
    settings: Path,
    logs: Path,
    node: Path,
    pi: Path,
    forwarded: list[str],
    env: dict[str, str],
) -> tuple[list[str], dict[str, str]]:
    env["PI_CODING_AGENT_DIR"] = str(settings)
    (settings / "models.json").write_text(
        json.dumps(
            {
                "providers": {
                    PROVIDER: {
                        "baseUrl": ROUTER,
                        "api": "openai-completions",
                        "apiKey": "${OPENAI_API_KEY}",
                        "models": [model],
                    }
                }
            }
        )
    )
    # Values remain in the existing process environment. Only the variable name
    # is written to Pi's standard models.json; no auth store is created.
    (settings / "settings.json").write_text(
        json.dumps(
            {
                "retry": {"enabled": False},
                "compaction": {"enabled": True},
            }
        )
    )
    args = [
        str(node),
        str(pi),
        "--mode",
        "rpc",
        "--provider",
        PROVIDER,
        "--model",
        str(model["id"]),
        "--thinking",
        "high",
        "--session-dir",
        str(logs / "sessions"),
        "--no-approve",
        *forwarded,
    ]
    return args, env


def _localpi_command(
    model: dict[str, object],
    settings: Path,
    logs: Path,
    node: Path,
    pi: Path,
    localpi: Path,
    forwarded: list[str],
    continuation_limit: int,
    env: dict[str, str],
) -> tuple[list[str], dict[str, str]]:
    """Hand Pi's configuration to localpi, which owns it for this launcher."""
    profile_path = settings / "model-profile.json"
    profile_path.write_text(json.dumps(_model_profile(model)))
    args = [
        str(node),
        str(localpi),
        "--runtime",
        "openai-compatible",
        "--provider-id",
        PROVIDER,
        "--base-url",
        ROUTER,
        "--model",
        str(model["id"]),
        "--api-key",
        "${OPENAI_API_KEY}",
        "--model-profile",
        str(profile_path),
        "--state-dir",
        str(settings),
        "--session-dir",
        str(logs / "sessions"),
        "--pi-command",
        f"{node} {pi}",
        "--thinking",
        "high",
        "--no-approval",
        "--stats",
        "off",
    ]
    if continuation_limit > 0:
        args.extend(["--continue-on-truncation", str(continuation_limit)])
    args.extend(["--mode", "rpc", *forwarded])
    return args, env


def _model_profile(model: dict[str, object]) -> dict[str, object]:
    profile: dict[str, object] = {
        "id": PROVIDER,
        "model": str(model["id"]),
        "base_url": ROUTER,
        # The Hugging Face router rejects the vendor thinking fields that Pi
        # sends for Qwen and DeepSeek models, so the run declares no thinking
        # format and sends only standard chat-completions fields.
        "capabilities": {"reasoning": False},
    }
    limits = {key: _limit(model, key) for key in LIMIT_KEYS}
    if limits["contextWindow"] > 0 and limits["maxTokens"] > 0:
        profile["client"] = {
            "context_window": limits["contextWindow"],
            "max_tokens": limits["maxTokens"],
        }
    return profile


def _limit(model: dict[str, object], key: str) -> int:
    value = model.get(key)
    if isinstance(value, bool) or not isinstance(value, int) or value <= 0:
        return 0
    return value
