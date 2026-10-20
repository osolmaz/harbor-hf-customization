"""Native errors must remain errors, including when Pi exits with status zero."""

import json
from pathlib import Path

import pytest

from anonbench1_adapters.pi_completion import (
    PiProviderError,
    require_completed_response,
)


def transcript(tmp_path: Path, events: list[object]) -> Path:
    path = tmp_path / "pi.txt"
    path.write_text("native diagnostic\n" + "\n".join(map(json.dumps, events)))
    return path


def message(reason: str) -> dict[str, object]:
    return {
        "type": "message_end",
        "message": {"role": "assistant", "stopReason": reason},
    }


@pytest.mark.parametrize(
    "events",
    [
        [message("error")],
        [message("error"), {"type": "auto_retry_end", "success": False}],
        [message("stop"), message("error")],
        [message("error"), {"type": "auto_retry_end"}],
    ],
)
def test_unrecovered_provider_failure_is_not_a_scored_answer(
    tmp_path: Path, events: list[object]
) -> None:
    with pytest.raises(PiProviderError, match="unrecovered provider error"):
        require_completed_response(transcript(tmp_path, events))


@pytest.mark.parametrize(
    "events",
    [
        [message("stop")],
        [message("error"), message("stop")],
        [message("error"), {"type": "auto_retry_end", "success": True}],
        [["diagnostic"], {"type": "message_end", "message": None}],
        [{"type": "message_end", "message": {"role": "user"}}],
    ],
)
def test_native_recovery_and_other_events_keep_their_outcome(
    tmp_path: Path, events: list[object]
) -> None:
    require_completed_response(transcript(tmp_path, events))


def test_setup_without_a_transcript_preserves_the_original_error(
    tmp_path: Path,
) -> None:
    require_completed_response(tmp_path / "missing.txt")


def test_public_post_run_hook_keeps_accounting_before_exposing_provider_failure(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    from harbor.models.agent.context import AgentContext

    from anonbench1_adapters import Anonbench1Pi, attested

    def populate(_self: object, context: AgentContext) -> None:
        context.n_input_tokens = 17
        context.metadata = {"native_accounting_retained": True}

    monkeypatch.setattr(attested._CopiedPi, "populate_context_post_run", populate)
    transcript(tmp_path, [message("error")])
    agent = Anonbench1Pi(logs_dir=tmp_path, model_name="openai/example")
    context = AgentContext()
    with pytest.raises(PiProviderError):
        agent.populate_context_post_run(context)
    assert context.n_input_tokens == 17
    assert context.metadata == {"native_accounting_retained": True}
