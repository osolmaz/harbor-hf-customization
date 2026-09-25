"""ACP adapter for one pinned OpenClaw native agent-exec turn."""

import argparse
import asyncio
import os
import tempfile
from pathlib import Path
from typing import Literal, override
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
    Usage,
    UsageUpdate,
)

from harbor_openclaw_native.models import NIM_ENDPOINT, endpoint_base_url, pinned_model
from harbor_openclaw_native.process import OpenClawProcess
from harbor_openclaw_native.runtime import command, install, run, write_config
from harbor_openclaw_native.values import count, number, record

PromptBlock = (
    TextContentBlock
    | ImageContentBlock
    | AudioContentBlock
    | ResourceContentBlock
    | EmbeddedResourceContentBlock
)
CodeMode = Literal["direct", "code"]


class OpenClawNativeAgent(Agent):
    def __init__(self, code_mode: CodeMode, logs: Path | None = None) -> None:
        self.code_mode = code_mode
        self.logs = logs or Path("/logs/agent/openclaw-native")
        self.conn: Client | None = None
        self.process = OpenClawProcess()
        self.session_id: str | None = None
        self.model_id = os.environ.get("HARBOR_ACP_REQUESTED_MODEL", "")
        self.settings: tempfile.TemporaryDirectory[str] | None = None
        self.workspace: str | None = None
        self.model: dict[str, object] | None = None
        self.entrypoint: Path | None = None
        self.active = False
        self.cancelled = asyncio.Event()

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
            agent_info=Implementation(name="openclaw-native", version="0.1.0rc6"),
        )

    def model_option(self) -> SessionConfigOptionSelect:
        selected = self.model_id
        return SessionConfigOptionSelect(
            id="model",
            name="Model",
            category="model",
            type="select",
            current_value=selected,
            options=[SessionConfigSelectOption(value=selected, name=selected)],
        )

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
        requested, model = await asyncio.to_thread(pinned_model, self.model_id)
        self.settings = tempfile.TemporaryDirectory(prefix="openclaw-native-")
        root = Path(self.settings.name)
        self.logs.mkdir(parents=True, exist_ok=True)
        (root / "state").mkdir()
        config = root / "openclaw.json"
        write_config(
            config,
            requested_model=requested,
            model=model,
            code_mode=self.code_mode,
        )
        self.entrypoint = await install(root, self.logs)
        self.workspace = cwd
        self.model = model
        self.session_id = uuid4().hex
        return NewSessionResponse(
            session_id=self.session_id,
            config_options=[self.model_option()],
        )

    def require_session(self, session_id: str) -> None:
        if session_id != self.session_id:
            raise RequestError.invalid_params()

    @override
    async def set_config_option(
        self, config_id: str, session_id: str, value: str | bool, **kwargs: object
    ) -> SetSessionConfigOptionResponse:
        self.require_session(session_id)
        if (config_id, value) != ("model", self.model_id):
            raise RequestError.invalid_params()
        return SetSessionConfigOptionResponse(config_options=[self.model_option()])

    async def report_usage(self, envelope: dict[str, object]) -> Usage:
        assert self.conn is not None and self.session_id is not None
        assert self.model is not None
        raw_usage = record(envelope.get("usage"))
        input_tokens = count(raw_usage.get("input", 0))
        output_tokens = count(raw_usage.get("output", 0))
        cached_read = count(raw_usage.get("cacheRead", 0))
        cached_write = count(raw_usage.get("cacheWrite", 0))
        total = count(
            raw_usage.get(
                "totalTokens",
                raw_usage.get(
                    "total", input_tokens + output_tokens + cached_read + cached_write
                ),
            )
        )
        # NVIDIA does not publish a rate for this private endpoint. OpenClaw's
        # zero-priced metadata is not evidence of a zero-dollar vendor charge.
        cost = (
            None
            if endpoint_base_url() == NIM_ENDPOINT
            else number(envelope.get("costUsd"))
        )
        context = count(self.model.get("contextWindow"))
        await self.conn.session_update(
            session_id=self.session_id,
            update=UsageUpdate(
                session_update="usage_update",
                used=min(context, input_tokens + cached_read + cached_write),
                size=context,
                cost=Cost(amount=cost, currency="USD") if cost is not None else None,
            ),
        )
        return Usage(
            total_tokens=total,
            input_tokens=input_tokens + cached_read + cached_write,
            output_tokens=output_tokens,
            cached_read_tokens=cached_read,
            cached_write_tokens=cached_write,
        )

    def attest(self, envelope: dict[str, object]) -> None:
        _, expected_model = self.model_id.split("/", 1)
        if (
            envelope.get("provider") != "openai"
            or envelope.get("model") != expected_model
        ):
            raise RuntimeError("OpenClaw selected a different model or provider")
        engaged = envelope.get("codeModeEngaged")
        if self.code_mode == "code" and engaged is not True:
            raise RuntimeError("OpenClaw did not engage forced Code Mode")
        if self.code_mode == "direct" and engaged is True:
            raise RuntimeError("OpenClaw engaged Code Mode in the direct arm")

    @override
    async def prompt(
        self, session_id: str, prompt: list[PromptBlock], **kwargs: object
    ) -> PromptResponse:
        self.require_session(session_id)
        if self.active or any(
            not isinstance(block, TextContentBlock) for block in prompt
        ):
            raise RequestError.invalid_params()
        assert self.settings is not None and self.workspace is not None
        assert self.entrypoint is not None and self.conn is not None
        assert self.session_id is not None
        self.active = True
        self.cancelled.clear()
        try:
            root = Path(self.settings.name)
            instruction = root / "instruction.txt"
            instruction.write_text(
                "\n".join(
                    block.text
                    for block in prompt
                    if isinstance(block, TextContentBlock)
                )
            )
            args, env = command(
                entrypoint=self.entrypoint,
                config=root / "openclaw.json",
                state=root / "state",
                workspace=self.workspace,
                instruction=instruction,
                requested_model=self.model_id,
                code_mode=self.code_mode,
            )
            envelope = await run(
                self.process,
                args=args,
                workspace=self.workspace,
                env=env,
                logs=self.logs,
            )
            self.attest(envelope)
            usage = await self.report_usage(envelope)
            final = envelope.get("final")
            if isinstance(final, str) and final:
                await self.conn.session_update(
                    session_id=self.session_id,
                    update=AgentMessageChunk(
                        session_update="agent_message_chunk",
                        content=TextContentBlock(type="text", text=final),
                    ),
                )
            if envelope.get("ok") is not True or envelope.get("status") != "ok":
                error = record(envelope.get("error", {}))
                message = error.get("message", "OpenClaw reported an agent failure")
                raise RuntimeError(str(message))
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
        await self.process.cancel()

    async def close(self) -> None:
        await self.process.cancel()
        if self.settings is not None:
            self.settings.cleanup()


async def serve(code_mode: CodeMode) -> None:
    agent = OpenClawNativeAgent(code_mode)
    try:
        await run_agent(agent)
    finally:
        await agent.close()


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--code-mode", choices=("direct", "code"), required=True)
    args = parser.parse_args()
    asyncio.run(serve(args.code_mode))
