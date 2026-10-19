"""Preserve explicit HF routes in the native installed harnesses."""

import shlex


def claude_environment(env: dict[str, str], model_name: str) -> dict[str, str]:
    """Translate the reviewed router connection into native Claude variables."""
    if "OPENAI_BASE_URL" not in env:
        return dict(env)
    if env["OPENAI_BASE_URL"] != "https://router.huggingface.co/v1":
        raise ValueError("Claude router translation requires the reviewed HF endpoint")
    key = env.get("OPENAI_API_KEY")
    if not key:
        raise ValueError("Claude router translation requires the inference credential")
    model = model_name.removeprefix("openai/")
    if ":" not in model or model.endswith(":"):
        raise ValueError("Claude router translation requires an explicit provider")
    translated = {
        k: v for k, v in env.items() if k not in {"OPENAI_BASE_URL", "OPENAI_API_KEY"}
    }
    translated.update(
        ANTHROPIC_BASE_URL="https://router.huggingface.co",
        ANTHROPIC_API_KEY=key,
        ANTHROPIC_AUTH_TOKEN=key,
        ANTHROPIC_DEFAULT_OPUS_MODEL=model,
        ANTHROPIC_DEFAULT_SONNET_MODEL=model,
        ANTHROPIC_DEFAULT_HAIKU_MODEL=model,
        CLAUDE_CODE_SUBAGENT_MODEL=model,
    )
    return translated


_CODEX_PREFIX = "if [ -s ~/.nvm/nvm.sh ]; then . ~/.nvm/nvm.sh; fi; codex exec "


def codex_command(command: str, model_name: str | None) -> str:
    """Keep the HF namespace lost by the pinned native Codex command builder."""
    if not command.startswith(_CODEX_PREFIX):
        return command
    if not model_name or ":" not in model_name or model_name.endswith(":"):
        raise ValueError("Codex router execution requires an explicit provider")
    flags, separator, instruction = command.partition(" -- ")
    needle = f"--model {model_name.rsplit('/', 1)[-1]} "
    if not separator or flags.count(needle) != 1:
        raise ValueError("The pinned native Codex command shape changed")
    model = shlex.quote(model_name.removeprefix("openai/"))
    return flags.replace(needle, f"--model {model} ", 1) + separator + instruction
