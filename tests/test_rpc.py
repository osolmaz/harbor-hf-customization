import asyncio
import os
import sys
from pathlib import Path

import pytest
from harbor_pi_code_mode.rpc import PiRpc


async def start(tmp_path: Path) -> PiRpc:
    rpc = PiRpc()
    await rpc.start(
        [sys.executable, "-u", str(Path(__file__).with_name("fake_pi.py"))],
        str(tmp_path),
        dict(os.environ),
        tmp_path / "events.jsonl",
    )
    return rpc


async def test_rpc_framing_response_and_cleanup(tmp_path: Path) -> None:
    rpc = await start(tmp_path)
    try:
        assert await rpc.request("get_state") == {"value": "a\u2028b"}
        await rpc.request("prompt")
        assert await rpc.events.get() == {"type": "agent_settled"}
        with pytest.raises(RuntimeError, match="rejected"):
            await rpc.request("reject")
        assert not rpc.pending
    finally:
        await rpc.close()
    assert rpc.process is not None and rpc.process.returncode is not None
    await rpc.close()
    with pytest.raises(RuntimeError, match="not running"):
        await rpc.request("get_state")


@pytest.mark.parametrize("command", ["exit", "malformed"])
async def test_failure_unblocks_pending_requests(tmp_path: Path, command: str) -> None:
    rpc = await start(tmp_path)
    try:
        with pytest.raises(RuntimeError, match="closed"):
            await rpc.request(command)
    finally:
        await rpc.close()
    assert not rpc.pending


async def test_bounded_queue_and_ignored_response() -> None:
    rpc = PiRpc()
    rpc._receive({"type": "response", "id": "unknown"})
    for _ in range(4096):
        rpc._receive({"type": "test"})
    with pytest.raises(ValueError, match="queue"):
        rpc._receive({"type": "test"})
    await rpc.close()
    with pytest.raises(RuntimeError):
        await rpc.request("test")


async def test_cancelled_caller_does_not_break_reader(tmp_path: Path) -> None:
    rpc = await start(tmp_path)
    request = asyncio.create_task(rpc.request("get_state"))
    request.cancel()
    with pytest.raises(asyncio.CancelledError):
        await request
    assert await rpc.request("get_state") == {"value": "a\u2028b"}
    await rpc.close()
