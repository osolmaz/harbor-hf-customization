"""Exercise real Pi modes against a scripted peer, without inference."""

import asyncio
import json
import tempfile
from pathlib import Path
from unittest.mock import patch

from harbor_pi_code_mode import runtime
from harbor_pi_code_mode.rpc import PiRpc
from harbor_pi_code_mode.values import record


class ScriptedModel:
    def __init__(self, code_mode: runtime.CodeMode) -> None:
        self.code_mode = code_mode
        self.calls = 0

    async def respond(
        self, reader: asyncio.StreamReader, writer: asyncio.StreamWriter
    ) -> None:
        headers = (await reader.readuntil(b"\r\n\r\n")).decode().split("\r\n")
        authorization = next(
            line.split(":", 1)[1].strip()
            for line in headers
            if line.lower().startswith("authorization:")
        )
        assert authorization == "Bearer test-only-scripted-peer"
        length = next(
            int(line.split(":", 1)[1])
            for line in headers
            if line.lower().startswith("content-length:")
        )
        request = record(json.loads(await reader.readexactly(length)))
        tools = request.get("tools")
        assert isinstance(tools, list)
        names = {record(record(tool)["function"])["name"] for tool in tools}
        expected = (
            {"exec", "wait"}
            if self.code_mode == "code"
            else {"bash", "read", "edit", "write"}
        )
        assert names == expected
        self.calls += 1
        delta: dict[str, object] = {"role": "assistant"}
        reason = "stop"
        if self.calls == 1:
            if self.code_mode == "code":
                code = (
                    'text(await tools.exec_command({cmd: "printf code-mode-ok '
                    '> result.txt", yield_time_ms: 1000}));'
                )
                name = "exec"
                arguments = {"code": code}
            else:
                name = "write"
                arguments = {"path": "result.txt", "content": "direct-ok"}
            delta["tool_calls"] = [
                {
                    "index": 0,
                    "id": f"{self.code_mode}-smoke",
                    "type": "function",
                    "function": {
                        "name": name,
                        "arguments": json.dumps(arguments),
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


class ScriptedEndpoint:
    def __init__(self) -> None:
        self.requests: list[dict[str, object]] = []

    async def respond(
        self, reader: asyncio.StreamReader, writer: asyncio.StreamWriter
    ) -> None:
        headers = (await reader.readuntil(b"\r\n\r\n")).decode().split("\r\n")
        assert any(
            line.lower() == "authorization: bearer test-only-scripted-peer"
            for line in headers
        )
        length = next(
            int(line.split(":", 1)[1])
            for line in headers
            if line.lower().startswith("content-length:")
        )
        request = record(json.loads(await reader.readexactly(length)))
        self.requests.append(request)
        first = len(self.requests) == 1
        chunk = {
            "id": "scripted-endpoint",
            "object": "chat.completion.chunk",
            "created": 1,
            "model": "scripted",
            "choices": [
                {
                    "index": 0,
                    "delta": {
                        "role": "assistant",
                        **(
                            {"reasoning_content": "I need to think."}
                            if first
                            else {"content": "The answer is 42."}
                        ),
                    },
                    "finish_reason": "length" if first else "stop",
                }
            ],
            "usage": {"prompt_tokens": 10, "completion_tokens": 10, "total_tokens": 20},
        }
        writer.write(
            b"HTTP/1.1 200 OK\r\nContent-Type: text/event-stream\r\n"
            b"Connection: close\r\n\r\n"
            + f"data: {json.dumps(chunk)}\n\ndata: [DONE]\n\n".encode()
        )
        await writer.drain()
        writer.close()
        await writer.wait_closed()


async def smoke_endpoint(
    max_output_tokens: int, thinking_cap: int, expected_phase_limit: int
) -> None:
    peer = ScriptedEndpoint()
    server = await asyncio.start_server(peer.respond, "127.0.0.1", 0)
    async with server, asyncio.timeout(90):
        with tempfile.TemporaryDirectory(prefix="pi-endpoint-smoke-") as temporary:
            root = Path(temporary)
            url = f"http://127.0.0.1:{server.sockets[0].getsockname()[1]}/v1"
            model: dict[str, object] = {
                "id": "scripted",
                "api": "openai-completions",
                "reasoning": True,
                "input": ["text"],
                "contextWindow": 32000,
                "maxTokens": 128,
                "cost": {"input": 0, "output": 0, "cacheRead": 0, "cacheWrite": 0},
            }
            args, env = runtime.command(
                model,
                root / "settings",
                root,
                "direct",
                max_provider_requests=2,
                launcher="localpi",
                max_output_tokens=max_output_tokens,
                thinking_format="qwen-chat-template",
                endpoint_engine="vllm",
                endpoint_base_url=url,
                thinking_budget=thinking_cap,
            )
            env["OPENAI_API_KEY"] = "test-only-scripted-peer"
            rpc = PiRpc()
            try:
                await rpc.start(args, temporary, env, root / "pi-events.jsonl")
                state = record(await rpc.request("get_state"))
                assert record(state["model"])["provider"] == "vllm"
                await rpc.request("prompt", message="What is 6 times 7?")
                settled = 0
                while settled < 2:
                    event = await rpc.events.get()
                    assert event is not None, "Pi closed before the answer"
                    if event.get("type") == "agent_settled":
                        settled += 1
                assert len(peer.requests) == 2, peer.requests
                first, second = peer.requests
                limits = (first.get("max_tokens"), first.get("max_completion_tokens"))
                answer_limits = (
                    second.get("max_tokens"),
                    second.get("max_completion_tokens"),
                )
                assert expected_phase_limit in limits, limits
                assert max_output_tokens - expected_phase_limit in answer_limits, (
                    answer_limits
                )
                assert (
                    record(second.get("chat_template_kwargs"))["enable_thinking"]
                    is False
                )
            finally:
                await rpc.close()
            print(
                "Real Pi endpoint cap and answer continuation passed against a "
                "scripted peer; no inference."
            )


async def smoke(
    code_mode: runtime.CodeMode, max_provider_requests: int | None = None
) -> None:
    peer = ScriptedModel(code_mode)
    server = await asyncio.start_server(peer.respond, "127.0.0.1", 0)
    async with server, asyncio.timeout(90):
        with tempfile.TemporaryDirectory(prefix=f"pi-{code_mode}-smoke-") as temporary:
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
                args, env = runtime.command(
                    model,
                    root / "settings",
                    root,
                    code_mode,
                    max_provider_requests,
                )
            env["OPENAI_API_KEY"] = "test-only-scripted-peer"
            rpc = PiRpc()
            try:
                await rpc.start(args, temporary, env, root / "pi-events.jsonl")
                await rpc.request("get_state")
                await rpc.request("prompt", message="Create result.txt, then finish.")
                while True:
                    event = await rpc.events.get()
                    assert event is not None, "Pi closed before completion"
                    assert not (
                        event.get("type") == "tool_execution_end"
                        and event.get("isError")
                    ), f"Pi {code_mode} tool failed"
                    if event.get("type") == "agent_settled":
                        break
                expected = "code-mode-ok" if code_mode == "code" else "direct-ok"
                assert (root / "result.txt").read_text() == expected
                expected_calls = 1 if max_provider_requests == 1 else 2
                assert peer.calls == expected_calls, (
                    f"Expected {expected_calls} HTTP requests, observed {peer.calls}"
                )
                stats = await rpc.request("get_session_stats")
                assert record(stats["tokens"])["output"] == expected_calls * 10
            finally:
                await rpc.close()
            print(
                f"Real Pi {code_mode} tool execution passed against a scripted peer; "
                f"request bound={max_provider_requests}; no inference."
            )


if __name__ == "__main__":
    asyncio.run(smoke("direct"))
    asyncio.run(smoke("code"))
    asyncio.run(smoke("code", max_provider_requests=1))
    asyncio.run(smoke_endpoint(128, 32, 32))
    asyncio.run(smoke_endpoint(96, 64, 48))
