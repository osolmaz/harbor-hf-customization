import asyncio
import sys
from pathlib import Path
from typing import cast

import pytest

from harbor_openclaw_native.process import OpenClawProcess


async def test_process_output(tmp_path: Path) -> None:
    process = OpenClawProcess()
    stdout = tmp_path / "stdout"
    stderr = tmp_path / "stderr"
    result = await process.run(
        [
            sys.executable,
            "-c",
            "import sys; print('ok'); print('note', file=sys.stderr)",
        ],
        str(tmp_path),
        {},
        stdout,
        stderr,
    )
    assert result == "ok\n"
    assert stdout.read_text() == "ok\n"
    assert stderr.read_text() == "note\n"
    await process.cancel()


async def test_process_failure_and_reentry(tmp_path: Path) -> None:
    process = OpenClawProcess()
    process.process = cast(asyncio.subprocess.Process, object())
    with pytest.raises(RuntimeError, match="already has an active process"):
        await process.run([], str(tmp_path), {}, tmp_path / "out", tmp_path / "err")
    process.process = None
    with pytest.raises(RuntimeError, match="exited with status 2"):
        await process.run(
            [sys.executable, "-c", "raise SystemExit(2)"],
            str(tmp_path),
            {},
            tmp_path / "out",
            tmp_path / "err",
        )
