"""Use the HF router's native Messages endpoint with Claude Code."""


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
