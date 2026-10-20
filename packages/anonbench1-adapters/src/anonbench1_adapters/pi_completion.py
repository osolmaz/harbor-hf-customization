"""Read Pi's public JSON events without changing native messages or retries."""

import json
from pathlib import Path


class PiProviderError(RuntimeError):
    """Pi exhausted its own recovery without a completed provider response."""


def _failure(event: dict[str, object]) -> bool | None:
    if event.get("type") == "message_end":
        message = event.get("message")
        if isinstance(message, dict) and message.get("role") == "assistant":
            return message.get("stopReason") == "error"
    elif event.get("type") == "auto_retry_end":
        success = event.get("success")
        if isinstance(success, bool):
            return not success
    return None


def require_completed_response(output: Path) -> None:
    """Expose an unrecovered native error to Harbor instead of scoring empty work."""
    if not output.exists():
        # Setup or transport can fail before Pi starts. Preserve that exception.
        return
    failed = False
    with output.open(encoding="utf-8") as stream:
        for line in stream:
            try:
                event = json.loads(line)
            except json.JSONDecodeError:
                continue  # Native diagnostics share the captured output file.
            if not isinstance(event, dict):
                continue
            outcome = _failure(event)
            if outcome is not None:
                failed = outcome
    if failed:
        raise PiProviderError(
            "Pi ended with an unrecovered provider error; see native agent/pi.txt"
        )
