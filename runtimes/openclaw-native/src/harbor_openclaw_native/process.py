"""Bounded process transport for one OpenClaw agent-exec turn."""

import asyncio
import os
import signal
from pathlib import Path

_MAX_STDOUT_BYTES = 64 * 1024 * 1024


class OpenClawProcess:
    def __init__(self) -> None:
        self.process: asyncio.subprocess.Process | None = None

    async def run(
        self,
        args: list[str],
        cwd: str,
        env: dict[str, str],
        stdout_path: Path,
        stderr_path: Path,
    ) -> str:
        if self.process is not None:
            raise RuntimeError("OpenClaw already has an active process")
        with stderr_path.open("wb") as errors:
            self.process = await asyncio.create_subprocess_exec(
                *args,
                cwd=cwd,
                env=env,
                stdout=asyncio.subprocess.PIPE,
                stderr=errors,
                start_new_session=True,
            )
            stdout, _ = await self.process.communicate()
        return_code = self.process.returncode
        self.process = None
        if len(stdout) > _MAX_STDOUT_BYTES:
            raise RuntimeError("OpenClaw output exceeded its limit")
        stdout_path.write_bytes(stdout)
        if return_code != 0:
            raise RuntimeError(f"OpenClaw exited with status {return_code}")
        try:
            return stdout.decode("utf-8")
        except UnicodeDecodeError as error:
            raise RuntimeError("OpenClaw output was not UTF-8") from error

    async def cancel(self) -> None:
        process = self.process
        if process is None or process.returncode is not None:
            return
        os.killpg(process.pid, signal.SIGTERM)
        try:
            await asyncio.wait_for(process.wait(), timeout=5)
        except TimeoutError:
            os.killpg(process.pid, signal.SIGKILL)
            await process.wait()
