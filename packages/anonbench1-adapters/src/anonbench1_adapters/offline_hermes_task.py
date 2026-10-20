"""Finite Harbor exec endpoint and Linux supervisors for one task generation.

This endpoint receives commands, never model configuration or host environment
variables. Each shell has a subreaper so detached children
remain owned after the shell returns. A result is distinct from family settlement.
"""

import base64
import ctypes
import fcntl
import hashlib
import json
import os
import re
import select
import signal
import subprocess
import sys
import time
from pathlib import Path

MAX_REQUEST = 1_000_000
MAX_OUTPUT = 5_000_000
# A native 50 MiB file needs 70,824,872 bytes of GNU-wrapped base64, plus
# command-wrapper markers. Foreground terminal and native-result bounds stay lower.
MAX_FULL_OUTPUT = 72 * 1024 * 1024
MAX_OPERATIONS = 4096
ID = re.compile(r"[a-f0-9]{32}\Z")


def _pidfd_result(result, _function, _arguments):
    if result == -1:
        raise OSError(ctypes.get_errno(), "Task pidfd operation failed")
    return result


def pidfd_api():
    # The admitted Python build may omit its optional pidfd wrappers. Resolve
    # the libc descriptor API and exercise both operations before accepting work.
    libc = ctypes.CDLL(None, use_errno=True)
    libc.pidfd_open.argtypes = [ctypes.c_int, ctypes.c_uint]
    libc.pidfd_open.restype = ctypes.c_int
    libc.pidfd_open.errcheck = _pidfd_result
    libc.pidfd_send_signal.argtypes = [ctypes.c_int, ctypes.c_int, ctypes.c_void_p, ctypes.c_uint]
    libc.pidfd_send_signal.restype = ctypes.c_int
    libc.pidfd_send_signal.errcheck = _pidfd_result
    fd = libc.pidfd_open(os.getpid(), 0)
    try:
        libc.pidfd_send_signal(fd, 0, None, 0)
    finally:
        os.close(fd)
    return libc


def publish(path, value):
    temporary = path.with_name(path.name + ".tmp")
    data = json.dumps(value, separators=(",", ":")).encode()
    with temporary.open("xb") as stream:
        stream.write(data)
        stream.flush()
        os.fsync(stream.fileno())
    os.replace(temporary, path)


def read(path):
    return json.loads(path.read_text())


def mark(path):
    try:
        fd = os.open(path, os.O_CREAT | os.O_EXCL | os.O_WRONLY, 0o600)
    except FileExistsError:
        return
    os.close(fd)


def generation(path, token):
    if not ID.fullmatch(token) or path != Path("/tmp") / ("harbor-hermes-" + token):
        raise ValueError("Invalid task generation")
    if path.is_symlink() or path.stat().st_uid != os.getuid():
        raise ValueError("Task generation ownership mismatch")
    if read(path / "identity.json") != {"generation": token}:
        raise ValueError("Task generation identity mismatch")


def children(pid):
    # Linux's subreaper adopts even setsid/double-fork descendants. The direct
    # shell remains Popen-owned; never reap it with waitpid(-1).
    result = []
    for item in Path("/proc").iterdir():
        if not item.name.isdecimal():
            continue
        try:
            fields = (item / "stat").read_text().rsplit(")", 1)[1].split()
            if int(fields[1]) == pid:
                result.append(int(item.name))
        except (FileNotFoundError, ProcessLookupError):
            pass
    return result


def kill_descendants(pid, libc):
    for child in children(pid):
        try:
            fd = libc.pidfd_open(child, 0)
        except ProcessLookupError:
            continue
        try:
            fields = Path(f"/proc/{child}/stat").read_text().rsplit(")", 1)[1].split()
            if int(fields[1]) != pid:
                continue
            kill_descendants(child, libc)
            libc.pidfd_send_signal(fd, signal.SIGKILL, None, 0)
        except (FileNotFoundError, ProcessLookupError):
            pass
        finally:
            os.close(fd)


def reap_adopted(shell_pid):
    for child in children(os.getpid()):
        if child != shell_pid:
            try:
                os.waitpid(child, os.WNOHANG)
            except ChildProcessError:
                pass


def supervise(root, operation):
    slot = root / operation
    request = read(slot / "request.json")
    output_limit = read(slot / "accepted.json")["output_limit"]
    libc = pidfd_api()
    if libc.prctl(36, 1, 0, 0, 0) != 0:  # PR_SET_CHILD_SUBREAPER
        raise OSError(ctypes.get_errno(), "Cannot own task descendants")
    proc = None
    try:
        # Start and generation closure serialize on the same lock. A delayed
        # supervisor cannot launch after close has revoked this generation.
        with (root / "lock").open("r+") as lock:
            fcntl.flock(lock, fcntl.LOCK_EX)
            if (root / "closed").exists() or (slot / "cancel").exists():
                publish(slot / "result.json", {"returncode": 130, "cancelled": True})
                publish(slot / "settled.json", {"operation": operation})
                return
            data = base64.b64decode(request["stdin"], validate=True)
            with (slot / "stdin").open("xb") as input_file:
                input_file.write(data)
            with (slot / "stdin").open("rb") as input_file:
                proc = subprocess.Popen(
                    ["bash", "-lc" if request["login"] else "-c", request["command"]],
                    cwd=request["cwd"], stdin=input_file, stdout=subprocess.PIPE,
                    stderr=subprocess.STDOUT, close_fds=True,
                )
            publish(slot / "started.json", {"operation": operation, "supervisor": os.getpid()})
        assert proc.stdout is not None
        os.set_blocking(proc.stdout.fileno(), False)
        deadline = time.monotonic() + request["timeout"]
        shell_returned = None
        output_size = 0
        cancelled = False
        overflow = False
        result_written = False
        output_eof = False
        with (slot / "output").open("xb", buffering=0) as output:
            while True:
                code = proc.poll()
                if code is not None and shell_returned is None:
                    shell_returned = time.monotonic()
                cancel_now = (root / "closed").exists() or (slot / "cancel").exists()
                timed_out = code is None and time.monotonic() >= deadline
                if cancel_now or timed_out or overflow:
                    cancelled = True
                    kill_descendants(os.getpid(), libc)
                if output_eof:
                    time.sleep(0.02)
                    ready = []
                else:
                    ready, _, _ = select.select([proc.stdout], [], [], 0.02)
                if ready:
                    chunk = os.read(proc.stdout.fileno(), 65536)
                    if chunk:
                        remaining = output_limit - output_size
                        output.write(chunk[:remaining])
                        output_size += min(len(chunk), remaining)
                        overflow |= len(chunk) > remaining
                        if overflow:
                            mark(slot / "failed")
                    else:
                        output_eof = True
                reap_adopted(proc.pid)
                # Match the native backend's post-shell output drain without
                # confusing that shell's exit with descendant settlement.
                if (
                    code is not None and not result_written
                    and (output_eof or (not ready and time.monotonic() - shell_returned >= 0.3))
                ):
                    publish(slot / "result.json", {
                        "returncode": code, "cancelled": cancelled,
                        "overflow": overflow,
                    })
                    result_written = True
                if code is not None and output_eof and not children(os.getpid()):
                    if not result_written:
                        publish(slot / "result.json", {
                            "returncode": code, "cancelled": cancelled,
                            "overflow": overflow,
                        })
                    publish(slot / "settled.json", {"operation": operation})
                    return
    finally:
        # Startup publication, pipe setup and output creation are inside the
        # same custody boundary as the wait loop. Failure never claims settled.
        if proc is not None:
            kill_descendants(os.getpid(), libc)
            proc.wait()
            if proc.stdout is not None:
                proc.stdout.close()


def start(root, operation, request):
    if not ID.fullmatch(operation):
        raise ValueError("Invalid operation identity")
    if not isinstance(request.get("command"), str) or not request["command"]:
        raise ValueError("Missing command")
    if not isinstance(request.get("cwd"), str) or not request["cwd"].startswith("/"):
        raise ValueError("An absolute task cwd is required")
    if type(request.get("timeout")) not in (int, float) or not 0 < request["timeout"] <= 86400:
        raise ValueError("Invalid command timeout")
    if type(request.get("login")) is not bool:
        raise ValueError("Invalid login-shell setting")
    output_limit = request.get("output_limit")
    if type(output_limit) is not int or output_limit not in (MAX_OUTPUT, MAX_FULL_OUTPUT):
        raise ValueError("Invalid output capture allowance")
    base64.b64decode(request["stdin"], validate=True)
    digest = hashlib.sha256(json.dumps(request, sort_keys=True).encode()).hexdigest()
    acceptance = {"digest": digest, "output_limit": output_limit}
    slot = root / operation
    with (root / "lock").open("r+") as lock:
        fcntl.flock(lock, fcntl.LOCK_EX)
        if (root / "closed").exists():
            raise RuntimeError("Task generation admission is closed")
        if slot.exists():
            if read(slot / "accepted.json") != acceptance:
                raise RuntimeError("Operation identity reused with different input")
            # A caller may reconcile this receipt, but never relaunch it.
            return {"accepted": operation}
        if sum(p.is_dir() for p in root.iterdir()) >= MAX_OPERATIONS:
            raise RuntimeError("Task generation operation capacity exceeded")
        slot.mkdir(mode=0o700)
        publish(slot / "request.json", request)
        publish(slot / "accepted.json", acceptance)
        with (slot / "supervisor.log").open("xb") as log:
            subprocess.Popen(
                [sys.executable, "-I", __file__, "serve", str(root), operation],
                stdin=subprocess.DEVNULL, stdout=log, stderr=log,
                start_new_session=True, close_fds=True,
            )
    return {"accepted": operation}


def status(root, operation, offset):
    if not ID.fullmatch(operation):
        raise ValueError("Invalid operation identity")
    slot = root / operation
    if not (slot / "accepted.json").exists():
        raise RuntimeError("No acceptance receipt for operation")
    output_limit = read(slot / "accepted.json")["output_limit"]
    if type(offset) is not int or not 0 <= offset <= output_limit:
        raise ValueError("Invalid output cursor")
    output = b""
    if (slot / "output").exists():
        with (slot / "output").open("rb") as stream:
            stream.seek(offset)
            output = stream.read(min(65536, output_limit - offset))
    result = read(slot / "result.json") if (slot / "result.json").exists() else None
    settled = read(slot / "settled.json") if (slot / "settled.json").exists() else None
    return {
        "operation": operation, "output": base64.b64encode(output).decode(),
        "offset": offset + len(output), "result": result, "settled": settled,
    }


def native_request(root, request, *, submit):
    """Finite publication/read for the one persistent native-handler family."""
    worker, operation = request["worker"], request["operation"]
    if not ID.fullmatch(worker) or not ID.fullmatch(operation):
        raise ValueError("Invalid native operation identity")
    slot = root / worker
    if not (slot / "accepted.json").exists():
        raise RuntimeError("Native worker has no accepted family")
    requests = slot / "requests"
    with (root / "lock").open("r+") as lock:
        fcntl.flock(lock, fcntl.LOCK_EX)
        if submit:
            if any((path).exists() for path in (root / "closed", slot / "stop", slot / "result.json")):
                raise RuntimeError("Native worker admission is closed")
            requests.mkdir(mode=0o700, exist_ok=True)
            path = requests / (operation + ".json")
            if path.exists():
                if read(path)["request"] != request:
                    raise RuntimeError("Native request identity was reused")
            else:
                count = sum(1 for _ in requests.iterdir())
                if count >= MAX_OPERATIONS:
                    raise RuntimeError("Native request capacity exceeded")
                publish(path, {"sequence": count, "request": request})
            return {"accepted": operation, "worker": worker}
        response = slot / "responses" / (operation + ".json")
        if response.exists():
            if response.stat().st_size > MAX_OUTPUT:
                raise RuntimeError("Native result exceeded its transport bound")
            return read(response)
        if (slot / "result.json").exists():
            raise RuntimeError("Native worker exited without a result")
        return {"pending": operation, "worker": worker}


def main():
    os.umask(0o077)
    action, root_text = sys.argv[1:3]
    root = Path(root_text)
    if action == "serve":
        supervise(root, sys.argv[3])
        return
    token = sys.argv[3]
    if action == "init":
        if not ID.fullmatch(token) or root != Path("/tmp") / ("harbor-hermes-" + token):
            raise ValueError("Invalid task generation")
        pidfd_api()
        root.mkdir(mode=0o700)
        publish(root / "identity.json", {"generation": token})
        mark(root / "lock")
        result = {"generation": token}
    else:
        generation(root, token)
        if action in {"start", "native_submit"}:
            operation = sys.argv[4]
            if not ID.fullmatch(operation):
                raise ValueError("Invalid operation identity")
            with (root / ("input-" + operation + ".json")).open("rb") as stream:
                raw = stream.read(MAX_REQUEST + 1)
        else:
            raw = base64.b64decode(sys.argv[4], validate=True)
        if len(raw) > MAX_REQUEST:
            raise ValueError("Task request is too large")
        request = json.loads(raw)
        if action in {"start", "native_submit"}:
            if request["operation"] != operation:
                raise ValueError("Published request identity mismatch")
            result = (start(root, operation, request["request"]) if action == "start"
                      else native_request(root, request, submit=True))
        elif action == "native_poll":
            result = native_request(root, request, submit=False)
        elif action == "native_stop":
            if not ID.fullmatch(request["worker"]):
                raise ValueError("Invalid native worker identity")
            slot = root / request["worker"]
            if (slot / "accepted.json").exists():
                with (root / "lock").open("r+") as lock:
                    fcntl.flock(lock, fcntl.LOCK_EX)
                    mark(slot / "stop")
            result = {"stop_requested": request["worker"]}
        elif action == "poll":
            result = status(root, request["operation"], request["offset"])
        elif action == "cancel":
            if not ID.fullmatch(request["operation"]):
                raise ValueError("Invalid operation identity")
            mark(root / request["operation"] / "cancel")
            result = {"cancellation_requested": request["operation"]}
        elif action == "close":
            with (root / "lock").open("r+") as lock:
                fcntl.flock(lock, fcntl.LOCK_EX)
                mark(root / "closed")
            result = {"closed": token}
        elif action == "settlement":
            slots = [p for p in root.iterdir() if p.is_dir()]
            result = {
                "generation": token, "closed": (root / "closed").exists(),
                "accepted": sorted(p.name for p in slots),
                "settled": sorted(p.name for p in slots if (p / "settled.json").exists()),
                "failed": sorted(p.name for p in slots if (p / "failed").exists()),
            }
        else:
            raise ValueError("Unknown task operation")
    print(json.dumps(result, separators=(",", ":")))


if __name__ == "__main__":
    main()
