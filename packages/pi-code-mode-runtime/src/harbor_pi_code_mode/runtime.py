"""Start only the runtime bundled in the locked wheel, with isolated settings."""

import json
import os
from pathlib import Path

from harbor_pi_code_mode.models import ROUTER


def command(
    model: dict[str, object], settings: Path, logs: Path
) -> tuple[list[str], dict[str, str]]:
    payload = Path(__file__).parent / "payload"
    node = payload / "bin/node"
    pi = payload / "node_modules/@earendil-works/pi-coding-agent/dist/cli.js"
    extension = payload / "node_modules/pi-code-mode/dist/extension/index.js"
    if not all(path.is_file() for path in (node, pi, extension)):
        raise RuntimeError("The pinned runtime payload is missing")
    settings.mkdir(parents=True, exist_ok=True)
    config_dir = settings / "config/pi-code-mode"
    config_dir.mkdir(parents=True, exist_ok=True)
    (config_dir / "config.json").write_text(json.dumps({"mode": "codex"}))
    (settings / "models.json").write_text(
        json.dumps(
            {
                "providers": {
                    "hf-pinned": {
                        "baseUrl": ROUTER,
                        "api": "openai-completions",
                        "apiKey": "OPENAI_API_KEY",
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
    env = {
        **os.environ,
        "PI_CODING_AGENT_DIR": str(settings),
        "PI_OFFLINE": "1",
        "PI_TELEMETRY": "0",
        "XDG_CONFIG_HOME": str(settings / "config"),
        "PATH": f"{payload / 'bin'}:{os.environ.get('PATH', '')}",
    }
    args = [
        str(node),
        str(pi),
        "--mode",
        "rpc",
        "--provider",
        "hf-pinned",
        "--model",
        str(model["id"]),
        "--thinking",
        "high",
        "--session-dir",
        str(logs / "sessions"),
        "--no-approve",
        "--no-extensions",
        "--no-skills",
        "--no-prompt-templates",
        "--no-themes",
        "-e",
        str(extension),
    ]
    return args, env
