"""ACP transport adapter for the pinned Pi RPC process."""

import argparse
import asyncio
import os
import tempfile
from pathlib import Path
from typing import override
from uuid import uuid4

from acp import (
    Agent,
    InitializeResponse,
    NewSessionResponse,
    PromptResponse,
    RequestError,
    run_agent,
)
from acp.interfaces import Client
from acp.schema import (
    AcpMcpServer,
    AgentCapabilities,
    AgentMessageChunk,
    AgentThoughtChunk,
    AudioContentBlock,
    ClientCapabilities,
    Cost,
    EmbeddedResourceContentBlock,
    HttpMcpServer,
    ImageContentBlock,
    Implementation,
    McpServerStdio,
    ResourceContentBlock,
    SessionConfigOptionSelect,
    SessionConfigSelectOption,
    SetSessionConfigOptionResponse,
    SseMcpServer,
    TextContentBlock,
    ToolCallProgress,
    ToolCallStart,
    Usage,
    UsageUpdate,
)

from harbor_pi_code_mode.models import pinned_model
from harbor_pi_code_mode.rpc import PiRpc
from harbor_pi_code_mode.runtime import CodeMode, Launcher, command
from harbor_pi_code_mode.values import count, number, record

PromptBlock = (
    TextContentBlock
    | ImageContentBlock
    | AudioContentBlock
    | ResourceContentBlock
    | EmbeddedResourceContentBlock
)


class PiCodeModeAgent(Agent):
    def __init__(
        self,
        code_mode: CodeMode,
        logs: Path | None = None,
        max_provider_requests: int | None = None,
        launcher: Launcher = "pi",
        continuation_limit: int = 0,
        max_output_tokens: int | None = None,
    ) -> None:
        self.code_mode = code_mode
        self.max_provider_requests = max_provider_requests
        self.launcher = launcher
        self.continuation_limit = continuation_limit
        self.max_output_tokens = max_output_tokens
        name = "pi-code-mode" if code_mode == "code" else "pi-direct"
        self.logs = logs or Path(f"/logs/agent/{name}")
        self.conn: Client | None = None
        self.rpc = PiRpc()
        self.session_id: str | None = None
        self.model_id = os.environ.get("HARBOR_ACP_REQUESTED_MODEL", "")
        self.settings: tempfile.TemporaryDirectory[str] | None = None
        self.active = False
        self.cancelled = asyncio.Event()
        self.failed = False

    @override
    def on_connect(self, conn: Client) -> None:
        self.conn = conn

    @override
    async def initialize(
        self,
        protocol_version: int,
        client_capabilities: ClientCapabilities | None = None,
        client_info: Implementation | None = None,
        **kwargs: object,
    ) -> InitializeResponse:
        return InitializeResponse(
            protocol_version=protocol_version,
            agent_capabilities=AgentCapabilities(),
            agent_info=Implementation(
                name="pi-code-mode" if self.code_mode == "code" else "pi-direct",
                version="0.1.0rc6",
            ),
        )

    def options(self) -> list[SessionConfigOptionSelect]:
        return [
            SessionConfigOptionSelect(
                id="model",
                name="Model",
                category="model",
                type="select",
                current_value=self.model_id,
                options=[
                    SessionConfigSelectOption(value=self.model_id, name=self.model_id)
                ],
            )
        ]

    @override
    async def new_session(
        self,
        cwd: str,
        additional_directories: list[str] | None = None,
        mcp_servers: list[HttpMcpServer | SseMcpServer | AcpMcpServer | McpServerStdio]
        | None = None,
        **kwargs: object,
    ) -> NewSessionResponse:
        if self.session_id or mcp_servers or additional_directories:
            raise RequestError.invalid_params()
        if not os.environ.get("OPENAI_API_KEY"):
            raise RuntimeError("The inference credential is not configured")
        model = await asyncio.to_thread(pinned_model, self.model_id)
        self.settings = tempfile.TemporaryDirectory(prefix=f"pi-{self.code_mode}-")
        self.logs.mkdir(parents=True, exist_ok=True)
        args, env = command(
            model,
            Path(self.settings.name),
            self.logs,
            self.code_mode,
            self.max_provider_requests,
            self.launcher,
            self.continuation_limit,
            self.max_output_tokens,
        )
        await self.rpc.start(args, cwd, env, self.logs / "pi-events.jsonl")
        state = await self.rpc.request("get_state")
        selected = record(state.get("model"))
        if selected.get("id") != model["id"] or selected.get("provider") != "hf-pinned":
            raise RuntimeError("Pi selected a different model or provider")
        self.session_id = uuid4().hex
        return NewSessionResponse(
            session_id=self.session_id, config_options=self.options()
        )

    def require_session(self, session_id: str) -> None:
        if self.session_id != session_id:
            raise RequestError.invalid_params()

    @override
    async def set_config_option(
        self, config_id: str, session_id: str, value: str | bool, **kwargs: object
    ) -> SetSessionConfigOptionResponse:
        self.require_session(session_id)
        if config_id != "model" or value != self.model_id:
            raise RequestError.invalid_params()
        return SetSessionConfigOptionResponse(config_options=self.options())

    async def report_usage(self) -> Usage:
        assert self.conn is not None and self.session_id is not None
        stats = await self.rpc.request("get_session_stats")
        tokens = record(stats.get("tokens"))
        context = record(stats.get("contextUsage"))
        await self.conn.session_update(
            session_id=self.session_id,
            update=UsageUpdate(
                session_update="usage_update",
                used=count(context.get("tokens")),
                size=count(context.get("contextWindow")),
                cost=Cost(amount=number(stats.get("cost")), currency="USD"),
            ),
        )
        cached_read = count(tokens.get("cacheRead"))
        cached_write = count(tokens.get("cacheWrite"))
        return Usage(
            total_tokens=count(tokens.get("total")),
            input_tokens=count(tokens.get("input")) + cached_read + cached_write,
            output_tokens=count(tokens.get("output")),
            cached_read_tokens=cached_read,
            cached_write_tokens=cached_write,
        )

    async def message(self, event: dict[str, object]) -> None:
        assert self.conn is not None and self.session_id is not None
        message = record(event.get("message"))
        if message.get("role") != "assistant":
            return
        if message.get("stopReason") == "error":
            self.failed = True
        contents = message.get("content", [])
        if not isinstance(contents, list):
            raise ValueError("Invalid Pi message content")
        for value in contents:
            block = record(value)
            if block.get("type") == "text":
                await self.conn.session_update(
                    session_id=self.session_id,
                    update=AgentMessageChunk(
                        session_update="agent_message_chunk",
                        content=TextContentBlock(
                            type="text", text=str(block.get("text", ""))
                        ),
                    ),
                )
            elif block.get("type") == "thinking":
                await self.conn.session_update(
                    session_id=self.session_id,
                    update=AgentThoughtChunk(
                        session_update="agent_thought_chunk",
                        content=TextContentBlock(
                            type="text", text=str(block.get("thinking", ""))
                        ),
                    ),
                )
        await self.report_usage()

    async def event(self, event: dict[str, object]) -> None:
        assert self.conn is not None and self.session_id is not None
        kind = event.get("type")
        if kind == "message_end":
            await self.message(event)
        elif kind == "tool_execution_start":
            await self.conn.session_update(
                session_id=self.session_id,
                update=ToolCallStart(
                    session_update="tool_call",
                    tool_call_id=str(event["toolCallId"]),
                    title=str(event["toolName"]),
                    status="in_progress",
                    raw_input=event.get("args"),
                ),
            )
        elif kind == "tool_execution_end":
            await self.conn.session_update(
                session_id=self.session_id,
                update=ToolCallProgress(
                    session_update="tool_call_update",
                    tool_call_id=str(event["toolCallId"]),
                    status="failed" if event.get("isError") else "completed",
                    raw_output=event.get("result"),
                ),
            )
        elif kind == "compaction_end" and not event.get("aborted"):
            # A later message or final stats includes compaction cost. Do not
            # invent context usage while Pi reports it as unknown after compaction.
            return

    @override
    async def prompt(
        self, session_id: str, prompt: list[PromptBlock], **kwargs: object
    ) -> PromptResponse:
        self.require_session(session_id)
        if self.active or any(
            not isinstance(block, TextContentBlock) for block in prompt
        ):
            raise RequestError.invalid_params()
        self.active, self.failed = True, False
        self.cancelled.clear()
        try:
            await self.rpc.request(
                "prompt",
                message="\n".join(
                    block.text
                    for block in prompt
                    if isinstance(block, TextContentBlock)
                ),
            )
            while True:
                event = await self.rpc.events.get()
                if event is None:
                    raise RuntimeError("Pi stopped before the prompt settled")
                if event.get("type") == "agent_settled":
                    break
                await self.event(event)
            usage = await self.report_usage()
            if self.failed:
                raise RuntimeError(
                    "Pi reported an inference failure; usage was retained"
                )
            return PromptResponse(
                stop_reason="cancelled" if self.cancelled.is_set() else "end_turn",
                usage=usage,
            )
        finally:
            self.active = False

    @override
    async def cancel(self, session_id: str, **kwargs: object) -> None:
        self.require_session(session_id)
        self.cancelled.set()
        await self.rpc.request("abort")

    async def close(self) -> None:
        await self.rpc.close()
        if self.settings is not None:
            self.settings.cleanup()


async def serve(
    code_mode: CodeMode,
    max_provider_requests: int | None = None,
    launcher: Launcher = "pi",
    continuation_limit: int = 0,
    max_output_tokens: int | None = None,
) -> None:
    agent = PiCodeModeAgent(
        code_mode=code_mode,
        max_provider_requests=max_provider_requests,
        launcher=launcher,
        continuation_limit=continuation_limit,
        max_output_tokens=max_output_tokens,
    )
    try:
        await run_agent(agent)
    finally:
        await agent.close()


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--code-mode", choices=("direct", "code"), required=True)
    parser.add_argument("--max-provider-requests", type=int)
    parser.add_argument("--launcher", choices=("pi", "localpi"), default="pi")
    parser.add_argument("--continue-on-truncation", type=int, default=0)
    parser.add_argument("--max-output-tokens", type=int)
    args = parser.parse_args()
    if args.max_provider_requests is not None and args.max_provider_requests < 1:
        parser.error("--max-provider-requests must be positive")
    if args.continue_on_truncation < 0:
        parser.error("--continue-on-truncation cannot be negative")
    if args.max_output_tokens is not None and args.max_output_tokens < 1:
        parser.error("--max-output-tokens must be positive")
    asyncio.run(
        serve(
            args.code_mode,
            args.max_provider_requests,
            args.launcher,
            args.continue_on_truncation,
            args.max_output_tokens,
        )
    )
