"""Transport for Pi's documented JSONL RPC protocol; no agent execution loop."""

import asyncio
import json
import os
import signal
from pathlib import Path
from uuid import uuid4

from harbor_pi_code_mode.values import record


class PiRpc:
    def __init__(self) -> None:
        self.process: asyncio.subprocess.Process | None = None
        self.reader: asyncio.Task[None] | None = None
        self.events: asyncio.Queue[dict[str, object] | None] = asyncio.Queue()
        self.pending: dict[str, asyncio.Future[dict[str, object]]] = {}
        self.failure: BaseException | None = None

    async def start(
        self, command: list[str], cwd: str, env: dict[str, str], log: Path
    ) -> None:
        with log.with_suffix(".stderr.log").open("w") as errors:
            self.process = await asyncio.create_subprocess_exec(
                *command,
                cwd=cwd,
                env=env,
                stdin=asyncio.subprocess.PIPE,
                stdout=asyncio.subprocess.PIPE,
                stderr=errors,
                limit=2 * 1024 * 1024,
                start_new_session=True,
            )
        self.reader = asyncio.create_task(self._read(log))

    async def _read(self, log: Path) -> None:
        assert self.process is not None and self.process.stdout is not None
        try:
            with log.open("w") as output:
                while line := await self.process.stdout.readline():
                    event = record(json.loads(line))
                    output.write(json.dumps(event, ensure_ascii=False) + "\n")
                    output.flush()
                    self._receive(event)
        except (ValueError, OSError) as error:
            self.failure = error
        finally:
            for future in self.pending.values():
                if not future.done():
                    future.set_exception(RuntimeError("Pi RPC closed unexpectedly"))
            self.events.put_nowait(None)

    def _receive(self, event: dict[str, object]) -> None:
        if event.get("type") == "response":
            future = self.pending.get(str(event.get("id")))
            if future is not None and not future.done():
                if event.get("success") is True:
                    future.set_result(record(event.get("data", {})))
                else:
                    future.set_exception(RuntimeError("Pi rejected an RPC command"))
        else:
            if self.events.qsize() >= 4096:
                raise ValueError("Pi event consumer exceeded its queue limit")
            self.events.put_nowait(event)

    async def request(self, command: str, **values: object) -> dict[str, object]:
        if (
            self.process is None
            or self.process.stdin is None
            or self.process.returncode is not None
        ):
            raise RuntimeError("Pi is not running")
        request_id = uuid4().hex
        future = asyncio.Future[dict[str, object]]()
        self.pending[request_id] = future
        try:
            self.process.stdin.write(
                (
                    json.dumps({"id": request_id, "type": command, **values}) + "\n"
                ).encode()
            )
            await self.process.stdin.drain()
            return await asyncio.wait_for(future, timeout=30)
        finally:
            self.pending.pop(request_id, None)

    async def close(self) -> None:
        if self.process is not None and self.process.returncode is None:
            os.killpg(self.process.pid, signal.SIGTERM)
            try:
                await asyncio.wait_for(self.process.wait(), 5)
            except TimeoutError:
                os.killpg(self.process.pid, signal.SIGKILL)
                await self.process.wait()
        if self.reader is not None:
            await self.reader
