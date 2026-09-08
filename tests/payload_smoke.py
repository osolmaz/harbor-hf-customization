"""Exercise real Pi and Code Mode against a scripted peer, without inference."""

import asyncio
import json
import tempfile
from pathlib import Path
from unittest.mock import patch

from harbor_pi_code_mode import runtime
from harbor_pi_code_mode.rpc import PiRpc
from harbor_pi_code_mode.values import record


class ScriptedModel:
    def __init__(self) -> None:
        self.calls = 0

    async def respond(
        self, reader: asyncio.StreamReader, writer: asyncio.StreamWriter
    ) -> None:
        headers = (await reader.readuntil(b"\r\n\r\n")).decode().split("\r\n")
        length = next(
            int(line.split(":", 1)[1])
            for line in headers
            if line.lower().startswith("content-length:")
        )
        request = record(json.loads(await reader.readexactly(length)))
        tools = request.get("tools")
        assert isinstance(tools, list)
        assert {record(record(tool)["function"])["name"] for tool in tools} == {
            "exec",
            "wait",
        }
        self.calls += 1
        delta: dict[str, object] = {"role": "assistant"}
        reason = "stop"
        if self.calls == 1:
            code = (
                'text(await tools.exec_command({cmd: "printf code-mode-ok '
                '> result.txt", yield_time_ms: 1000}));'
            )
            delta["tool_calls"] = [
                {
                    "index": 0,
                    "id": "code-mode-smoke",
                    "type": "function",
                    "function": {
                        "name": "exec",
                        "arguments": json.dumps({"code": code}),
                    },
                }
            ]
            reason = "tool_calls"
        else:
            delta["content"] = "done"
        chunk = {
            "id": "scripted",
            "object": "chat.completion.chunk",
            "created": 1,
            "model": "scripted",
            "choices": [{"index": 0, "delta": delta, "finish_reason": reason}],
            "usage": {"prompt_tokens": 10, "completion_tokens": 10, "total_tokens": 20},
        }
        body = f"data: {json.dumps(chunk)}\n\ndata: [DONE]\n\n".encode()
        writer.write(
            b"HTTP/1.1 200 OK\r\nContent-Type: text/event-stream\r\n"
            b"Connection: close\r\n\r\n" + body
        )
        await writer.drain()
        writer.close()
        await writer.wait_closed()


async def smoke() -> None:
    peer = ScriptedModel()
    server = await asyncio.start_server(peer.respond, "127.0.0.1", 0)
    async with server, asyncio.timeout(90):
        with tempfile.TemporaryDirectory(prefix="code-mode-smoke-") as temporary:
            root = Path(temporary)
            router = f"http://127.0.0.1:{server.sockets[0].getsockname()[1]}/v1"
            model: dict[str, object] = {
                "id": "scripted",
                "name": "Scripted protocol peer",
                "api": "openai-completions",
                "reasoning": False,
                "input": ["text"],
                "contextWindow": 32000,
                "maxTokens": 128,
                "cost": {"input": 1, "output": 1, "cacheRead": 0, "cacheWrite": 0},
            }
            with patch.object(runtime, "ROUTER", router):
                args, env = runtime.command(model, root / "settings", root)
            env["OPENAI_API_KEY"] = "test-only-scripted-peer"
            rpc = PiRpc()
            try:
                await rpc.start(args, temporary, env, root / "pi-events.jsonl")
                await rpc.request("get_state")
                await rpc.request(
                    "prompt", message="Create result.txt using Code Mode, then finish."
                )
                while True:
                    event = await rpc.events.get()
                    assert event is not None, "Pi closed before completion"
                    assert not (
                        event.get("type") == "tool_execution_end"
                        and event.get("isError")
                    ), "Code Mode tool failed"
                    if event.get("type") == "agent_settled":
                        break
                assert (root / "result.txt").read_text() == "code-mode-ok"
                assert peer.calls == 2
                stats = await rpc.request("get_session_stats")
                assert record(stats["tokens"])["output"] == 20
            finally:
                await rpc.close()
            print(
                "Real Pi and Code Mode tool execution passed against "
                "a scripted peer; no inference."
            )


if __name__ == "__main__":
    asyncio.run(smoke())
