"""Native Hermes terminal provider and middleware for a dedicated host process."""

import asyncio
import base64
import hashlib
import json
import os
import select
import shlex
import subprocess
import tempfile
import threading
import time
import uuid
import weakref
from contextlib import ExitStack, nullcontext
from contextvars import ContextVar
from copy import deepcopy
from dataclasses import dataclass
from pathlib import Path

from agent.terminal_env_provider import TerminalEnvironmentProvider
from anonbench1_adapters.offline_hermes_state import BoundaryAbort, current_trial
from anonbench1_adapters.offline_hermes_task import MAX_FULL_OUTPUT, MAX_OUTPUT
from tools.environments.base import BaseEnvironment, FileFetchError

_PROFILE_CONTEXT = ("session_id", "model", "provider", "base_url", "api_mode")
_output_capture: ContextVar[tuple[object, int] | None] = ContextVar("harbor_output_capture", default=None)


def _profile_context(agent):
    return tuple((getattr(agent, key, None) or "") if key == "session_id"
                 else getattr(agent, key, None) for key in _PROFILE_CONTEXT)


@dataclass(frozen=True)
class _PreparedHostProfile:
    agent: object
    boundary: object
    context: tuple
    canonical: str
    wire: str
    aliases: tuple
    request_model: str


def _install_host_profile_capture(ctx, boundary, native, mode):
    """Register one build observer; native conversion and dispatch stay inherited."""
    from functools import wraps

    from agent import transports
    from agent.message_sanitization import sanitize_outbound_kwargs
    from agent.subagent_lifecycle import get_active_subagent_parent

    original = native.build_kwargs
    # Pinned registry inspection avoids constructing a second transport merely
    # to inspect its class. The host sourceguard already admitted these modules.
    if transports._REGISTRY.get(mode) is not native:
        boundary.trip("Native transport registration differs from the admitted owner")
        raise BoundaryAbort("Native profile capture registration overlaps")
    observed = weakref.WeakSet()
    lease_lock = threading.Lock()
    released = False
    installed = False

    @wraps(original)
    def build(transport, model, messages, tools=None, **params):
        active = get_active_subagent_parent()
        selected = active is not None and getattr(active, "_transport_cache", {}).get(mode) is transport
        if not selected:
            # Native auxiliary instances are outside the active root/child's
            # cached transport. Do not capture or change their dispatch.
            return original(transport, model, messages, tools=tools, **params)
        with lease_lock:
            inactive = released
            if not inactive:
                transport._harbor_prepared_profile = None
                observed.add(transport)
        if inactive:
            boundary.trip("Native profile capture was unloaded")
            raise BoundaryAbort("Native profile capture is no longer registered")
        boundary.guard()
        context = _profile_context(active)
        if (
            current_trial() is not boundary or active.api_mode != mode or model != active.model
            or any(key in params and params[key] != getattr(active, key, None)
                   for key in ("session_id", "base_url", "provider"))
        ):
            boundary.trip("Native build inputs do not match the active selected profile")
            raise BoundaryAbort("Native host profile preparation context differs")
        # Entry precedes native conversion's shared nested parameters. Capture
        # the actual request selection on every build, including cache-plan
        # projections, forks and refreshes; agent.tools may be the broader list.
        canonical = json.dumps(tools or [], allow_nan=False)
        result = original(transport, model, messages, tools=tools, **params)
        if context != _profile_context(active):
            boundary.trip("Native context changed during request preparation")
            raise BoundaryAbort("Native host profile preparation context changed")
        aliases = dict(getattr(transport, "_last_wire_aliases", {})) if mode == "codex_responses" else {}
        expected = transport.convert_tools(json.loads(canonical)) or []
        inverse = {name: alias for alias, name in aliases.items()}
        for tool in expected:
            definition = tool if mode == "codex_responses" else tool["function"]
            definition["name"] = inverse.get(definition["name"], definition["name"])
        envelope = {"model": result["model"], "instructions": "Profile admission", "input": [], "tools": expected}
        sanitize_outbound_kwargs(active, envelope)
        if mode == "codex_responses":
            envelope = transport.preflight_kwargs(
                envelope, allow_stream=False,
                is_github_responses=params.get("is_github_responses", False),
                sanitize_harmony_tokens=params.get("is_codex_backend", False),
            )
        prepared = _PreparedHostProfile(
            weakref.ref(active), boundary, context, canonical,
            json.dumps(envelope.get("tools", []), allow_nan=False), tuple(aliases.items()), result["model"],
        )
        with lease_lock:
            inactive = released
            if not inactive:
                transport._harbor_prepared_profile = prepared
        if inactive:
            boundary.trip("Native profile capture was unloaded during preparation")
            raise BoundaryAbort("Native profile capture is no longer registered")
        return result

    observer = type("Harbor" + native.__name__, (native,), {"build_kwargs": build, "__module__": __name__})

    def release():
        nonlocal released
        with lease_lock:
            released = True
            for transport in observed:
                prepared = getattr(transport, "_harbor_prepared_profile", None)
                if prepared is not None and prepared.boundary is boundary:
                    transport._harbor_prepared_profile = None
            displaced = installed and transports._REGISTRY.get(mode) is not observer
            if installed and not displaced:
                transports.register_transport(mode, native)
        if displaced:
            # A foreign registry owner wins; never overwrite it or a live cache.
            boundary.trip("Native profile capture ownership changed before unload")

    # Native load timeout may refuse or dispose this handle before installation.
    # Serialize installation with its inverse; a late builder cannot republish.
    handle = ctx.on_unload(release)
    with lease_lock:
        inactive = handle is None or not handle.active or released or transports._REGISTRY.get(mode) is not native
        if not inactive:
            transports.register_transport(mode, observer)
            installed = True
    if inactive:
        boundary.trip("Native profile capture registration was refused")
        raise BoundaryAbort("Native profile capture is no longer registered")


def register_host_profiles(ctx, boundary):
    from agent.subagent_lifecycle import get_active_subagent_parent
    from agent.transports import registered_api_modes
    from agent.transports.chat_completions import ChatCompletionsTransport
    from agent.transports.codex import ResponsesApiTransport

    # run_host_agent discovers this plugin before constructing/binding its root.
    # Late registration never purges or replaces an already cached transport.
    if boundary.root is not None or get_active_subagent_parent() is not None:
        boundary.trip("Native profile capture must precede root construction")
        raise BoundaryAbort("Native profile capture registration is late")
    registered_api_modes()
    for mode, native in (("chat_completions", ChatCompletionsTransport), ("codex_responses", ResponsesApiTransport)):
        _install_host_profile_capture(ctx, boundary, native, mode)


# Each response fits the selected task's 5 MB operation spool. File identity and
# a full digest bind separate read operations; no persistent transfer service.
_FETCH_CHUNK = 1024 * 1024
_FETCH_READ = r'''
import base64, hashlib, json, os, stat, sys
request = json.loads(sys.argv[1])
def identity(info):
    return [info.st_dev, info.st_ino, info.st_size, info.st_mtime_ns, info.st_ctime_ns]
try:
    fd = os.open(request['path'], os.O_RDONLY | os.O_NONBLOCK)
    with os.fdopen(fd, 'rb') as source:
        before = os.fstat(source.fileno())
        if not stat.S_ISREG(before.st_mode) or before.st_size > request['max_bytes']:
            raise ValueError('file is not regular or exceeds the delivery limit')
        stamp = identity(before)
        if request['identity'] is None:
            digest = hashlib.sha256()
            remaining = request['max_bytes'] + 1
            total = 0
            while remaining:
                part = source.read(min(1048576, remaining))
                if not part: break
                total += len(part); remaining -= len(part); digest.update(part)
            if total != before.st_size:
                raise ValueError('file size changed during admission')
            reply = {'identity': stamp, 'sha256': digest.hexdigest()}
        else:
            if stamp != request['identity']:
                raise ValueError('file changed during transfer')
            source.seek(request['offset'])
            data = source.read(request['count'])
            if len(data) != request['count']:
                raise ValueError('file changed during transfer')
            reply = {'offset': request['offset'], 'data': base64.b64encode(data).decode('ascii')}
        if identity(os.fstat(source.fileno())) != stamp:
            raise ValueError('file changed during transfer')
except (OSError, ValueError):
    reply = {'error': 'file_unavailable'}
print(request['marker'] + json.dumps(reply, separators=(',', ':')))
'''


class HostTaskClient:
    def __init__(self, boundary, reader, writer, cwd, task_runtime, native_snapshot=None):
        self.boundary = boundary
        self.reader = reader
        self.writer = writer
        self.cwd = cwd
        self.task_runtime = task_runtime
        self.native_snapshot = native_snapshot
        self.native_routes = frozenset()
        self._lock = threading.RLock()
        self._pending = {}
        self._families = {}
        self._processes = []
        self.closing = threading.Event()
        self._next = 0
        self._done = False
        self.finished = False
        self._reader_thread = threading.Thread(target=self._receive, daemon=True)
        self._reader_thread.start()

    def _receive(self):
        try:
            for line in self.reader:
                message = json.loads(line)
                if message.get("fatal"):
                    self.boundary.trip("Harbor transport revoked this trial")
                with self._lock:
                    pending = self._pending.pop(message["id"], None)
                if pending is None:
                    raise RuntimeError("Unknown Harbor response identity")
                pending["message"] = message
                if message.get("fatal"):
                    self.boundary.uncertain(pending["ticket"], "Task operation has no settlement receipt")
                else:
                    self.boundary.settle(pending["ticket"], remote_confirmed=True)
                pending["ready"].set()
            if not self.finished:
                self.boundary.trip("Host task channel ended before settlement")
        # Receiver failure revokes authority and wakes every pending caller.
        except BaseException:  # noqa: BLE001
            self.boundary.trip("Host task channel failed")
        finally:
            with self._lock:
                self._done = True
                for pending in self._pending.values():
                    pending["message"] = {"fatal": True}
                    pending["ready"].set()

    def call(self, action, payload, family=None):
        cleanup = self.boundary.cleanup_for(family) if family is not None else nullcontext()
        with cleanup:
            ticket = self.boundary.admit("task-ipc:" + action)
        self.boundary.dispatch(ticket)
        with self._lock:
            if self._done:
                self.boundary.settle(ticket)
                self.boundary.trip("Host task channel is closed")
                raise BoundaryAbort("Host task channel is closed")
            self._next += 1
            number = self._next
            pending = {"ticket": ticket, "ready": threading.Event()}
            self._pending[number] = pending
            try:
                self.writer.write(json.dumps({"id": number, "action": action, "payload": payload}) + "\n")
                self.writer.flush()
            except BaseException:
                self.boundary.uncertain(ticket, "Task request publication is uncertain")
                raise
        # The receiver retains the ticket if this thread is interrupted. A
        # worker's local unwind cannot acknowledge the remote operation.
        pending["ready"].wait()
        message = pending["message"]
        if message.get("fatal"):
            raise BoundaryAbort("Harbor task operation failed")
        return message["result"]

    def spawn(self, command, login, timeout, stdin_data, *, output_limit=MAX_OUTPUT):
        ticket = self.boundary.admit("task-family")
        self.boundary.dispatch(ticket)
        operation = uuid.uuid4().hex
        with self._lock:
            self._families[operation] = ticket
        request = {
            "operation": operation,
            "request": {
                "command": command, "cwd": self.cwd, "login": login, "timeout": timeout,
                "output_limit": output_limit,
                "stdin": base64.b64encode((stdin_data or "").encode("utf-8", "surrogateescape")).decode(),
            },
        }
        try:
            receipt = self.call("start", request)
            if receipt != {"accepted": operation}:
                raise RuntimeError("Task acceptance identity mismatch")
        except BaseException:
            self.boundary.uncertain(ticket, "Task family acceptance is uncertain")
            raise
        process = RemoteProcess(self, operation, ticket, output_limit)
        with self._lock:
            self._processes.append(process)
        return process

    def native_dispatch(self, name, args, context):
        if name not in self.native_routes:
            self.boundary.trip("Native task route is not admitted")
            raise BoundaryAbort("Native task route is not admitted")
        # These are the native registry's serializable call-context fields.
        # Never forward parent_agent, clients, credentials or a host environment.
        call_context = {key: context[key] for key in ("task_id", "session_id", "user_task") if key in context}
        return self.call("native", {"kind": "tool", "name": name, "args": args, "context": call_context})

    def require_task_environment(self, task_id):
        """Bind cold native tool access to the selected task environment."""
        from tools.terminal_tool import _get_env_config
        from tools.terminal_tool_lifecycle import ensure_task_env, get_active_env

        self.boundary.guard()
        if not isinstance(task_id, str) or not task_id:
            self.boundary.trip("Native tool dispatch has no task environment identity")
            raise BoundaryAbort("Task environment identity is required")
        if _get_env_config()["env_type"] != "harbor_task":
            self.boundary.trip("Native tool selected a different environment provider")
            raise BoundaryAbort("Task environment provider differs from the admitted Harbor owner")
        # The native owner resolves shared aliases and separately isolated
        # children. Never create/register an environment in a parallel map.
        environment = ensure_task_env(task_id)
        if (
            not isinstance(environment, HarborEnvironment) or environment.client is not self
            or get_active_env(task_id) is not environment
        ):
            self.boundary.trip("Native task image environment binding is absent or mismatched")
            raise BoundaryAbort("Required task image environment was not admitted")
        self.boundary.guard()

    def validate_model_profile(self, request, *, api_mode=None, context=None):
        """Validate wire definitions before returning canonical route identities."""
        if not self.native_snapshot:
            return {}
        entries = self.native_snapshot["entries"]
        mode = self.native_snapshot.get("browser_runtime", {}).get("mode")
        if "browser_vision" in entries and mode != "browser-use":
            from tools.vision_tools import check_vision_requirements

            # The task intentionally has no main/aux model configuration. Its
            # false vision check cannot justify removing a host-selected tool.
            if check_vision_requirements():
                self.boundary.trip("Model-selected conventional browser vision has no admitted task route")
                raise BoundaryAbort("Conventional browser vision remains unqualified")
        from agent.subagent_lifecycle import get_active_subagent_parent

        active = get_active_subagent_parent()
        transport = getattr(active, "_transport_cache", {}).get(api_mode)
        prepared = getattr(transport, "_harbor_prepared_profile", None)
        # A preparation belongs to one native request, including one child.
        # Never reconstruct the expected schema from live tools or the request.
        if transport is not None:
            transport._harbor_prepared_profile = None
        if (
            not isinstance(prepared, _PreparedHostProfile) or prepared.agent() is not active
            or prepared.boundary is not self.boundary or request.get("model") != prepared.request_model
            or not context or prepared.context != _profile_context(active)
            or tuple(context.get(key) for key in _PROFILE_CONTEXT) != prepared.context
        ):
            self.boundary.trip("Native request has no matching prepared host profile")
            raise BoundaryAbort("Native host profile preparation is absent or stale")
        model_tools = {}
        for tool in json.loads(prepared.wire):
            definition = tool if api_mode == "codex_responses" else tool["function"]
            model_tools[definition["name"]] = tool
        aliases = {}
        if api_mode == "codex_responses":
            from agent.transports.codex import ResponsesApiTransport

            # Pinned native seam: the active root/child already built this
            # request. Do not create a transport or reconstruct its alias map.
            active = get_active_subagent_parent()
            fields = ("session_id", "model", "provider", "base_url", "api_mode")
            active_context = {key: getattr(active, key, None) for key in fields}
            active_context["session_id"] = active_context["session_id"] or ""
            if active is None or not context or any(
                key not in context or context[key] != active_context[key] for key in fields
            ):
                self.boundary.trip("Responses request has no matching active native agent")
                raise BoundaryAbort("Responses request context is not bound to its native agent")
            transport = getattr(active, "_transport_cache", {}).get(api_mode)
            emitted = getattr(transport, "_last_wire_aliases", None)
            if (
                not isinstance(transport, ResponsesApiTransport) or not isinstance(emitted, dict)
                or tuple(emitted.items()) != prepared.aliases
            ):
                self.boundary.trip("Responses request has no emitted native alias map")
                raise BoundaryAbort("Responses request alias provenance is absent")
            aliases = dict(emitted)
            wire_names = {tool.get("name") for tool in request.get("tools", ())}
            if (
                any(not isinstance(alias, str) or not isinstance(name, str) or alias == name
                    for alias, name in aliases.items())
                or len(set(aliases.values())) != len(aliases)
                or not aliases.keys() <= wire_names
            ):
                self.boundary.trip("Responses alias map does not match the supplied request")
                raise BoundaryAbort("Responses request alias provenance differs")
        validated = {}
        for tool in request.get("tools", ()):
            definition = tool.get("function", tool)
            name = definition.get("name")
            canonical = aliases.get(name, name)
            observed = entries.get(canonical)
            model_tool = model_tools.get(name)
            wire_tool = tool
            schema_equal = model_tool == wire_tool
            available = bool(observed["available"]) if observed is not None else model_tool is not None
            gap = bool(observed["gap"]) if observed is not None else False
            if not available or gap or not schema_equal:
                self.boundary.trip("Model-selected native schema differs from its task capability admission")
                raise BoundaryAbort(
                    f"Actual native profile is unqualified (tool={name}; "
                    f"available={available}; gap={gap}; "
                    f"schema_equal={schema_equal})"
                )
            validated[name] = canonical
        if len(validated) != len(request.get("tools", ())) or validated.keys() != model_tools.keys():
            self.boundary.trip("Supplied tool selection differs from the prepared native profile")
            raise BoundaryAbort("Native host profile tool selection differs")
        return validated

    def settled(self, operation, ticket):
        with self._lock:
            if self._families.pop(operation, None) is ticket:
                self.boundary.settle(ticket, remote_confirmed=True)

    def close(self):
        with self.boundary.owned_cleanup():
            self.closing.set()
            with self._lock:
                processes = tuple(self._processes)
            deadline = time.monotonic() + 15
            for process in processes:
                process._worker_thread.join(timeout=max(0, deadline - time.monotonic()))
                if process._worker_thread.is_alive():
                    self.boundary.trip("Task receiver did not drain")
            receipt = self.call("close", {})
            if receipt.get("generation") != self.boundary.generation:
                self.boundary.trip("Wrong generation settlement receipt")
                raise BoundaryAbort("Wrong generation settlement receipt")
            with self._lock:
                families = dict(self._families)
            if not families.keys() <= set(receipt["settled"]):
                self.boundary.trip("Task family settlement is incomplete")
                raise BoundaryAbort("Task family settlement is incomplete")
            for operation, ticket in families.items():
                self.settled(operation, ticket)
            for process in processes:
                process._family_done.set()
            self.finished = True
            if receipt.get("failed"):
                self.boundary.trip("Task transport limits were exceeded")


class RemoteProcess:
    """Hermes ProcessHandle; its PID is never a task PID interpreted on the host."""

    pid = None

    def __init__(self, client, operation, ticket, output_limit):
        self.client = client
        self.operation = operation
        self.ticket = ticket
        self.output_limit = output_limit
        self._returncode = None
        self._done = threading.Event()
        self._family_done = threading.Event()
        self._stdout_read, self._stdout_write = os.pipe()
        os.set_blocking(self._stdout_write, False)
        self.stdout = os.fdopen(self._stdout_read, "r", encoding="utf-8", errors="replace")
        self._worker_thread = threading.Thread(target=self._collect, daemon=True)
        self._worker_thread.start()

    def _collect(self):
        offset = 0
        output_open = True
        try:
            while True:
                if self.client.closing.is_set():
                    return
                state = self.client.call("poll", {"operation": self.operation, "offset": offset}, self.ticket)
                if state.get("operation") != self.operation:
                    raise RuntimeError("Task output identity mismatch")
                output = base64.b64decode(state["output"], validate=True)
                if (len(output) > 65536 or type(state["offset"]) is not int
                        or state["offset"] != offset + len(output)
                        or state["offset"] > self.output_limit):
                    raise RuntimeError("Task output cursor mismatch")
                offset = state["offset"]
                if output and output_open:
                    try:
                        view = memoryview(output)
                        while view:
                            if self.client.closing.is_set():
                                return
                            _, writable, _ = select.select([], [self._stdout_write], [], 0.05)
                            if writable:
                                try:
                                    count = os.write(self._stdout_write, view)
                                    view = view[count:]
                                except BlockingIOError:
                                    continue
                    except BrokenPipeError:
                        os.close(self._stdout_write)
                        output_open = False
                result = state.get("result")
                if result is not None and not output and not self._done.is_set():
                    if result.get("overflow"):
                        raise RuntimeError("Task output exceeded the admitted transport bound")
                    self._returncode = result["returncode"]
                    if output_open:
                        os.close(self._stdout_write)
                        output_open = False
                    self._done.set()
                if state.get("settled") == {"operation": self.operation}:
                    if output:
                        continue
                    self.client.settled(self.operation, self.ticket)
                    self._family_done.set()
                    return
                time.sleep(0.05)
        # Lost observation must poison authority without fabricating settlement.
        except BaseException:  # noqa: BLE001
            # Never fabricate exit 0/1 or settlement after losing the receiver.
            self.client.boundary.trip("Task process observation failed")
        finally:
            if output_open:
                os.close(self._stdout_write)

    @property
    def returncode(self):
        return self._returncode

    def poll(self):
        return self._returncode if self._done.is_set() else None

    def wait(self, timeout=None):
        if not self._done.wait(timeout):
            raise subprocess.TimeoutExpired("Harbor task process", timeout)
        return self._returncode

    def kill(self):
        self.client.call("cancel", {"operation": self.operation}, self.ticket)
        if not self._family_done.wait(30):
            self.client.boundary.trip("Cancelled task family has not settled")
            raise BoundaryAbort("Cancelled task family has not settled")


class HarborEnvironment(BaseEnvironment):
    def __init__(self, client, cwd, timeout):
        self.client = client
        super().__init__(cwd=cwd, timeout=timeout, env={})
        self.init_session()

    def execute(self, command, cwd="", *, bounded_capture=False, **kwargs):
        # Native deadline workers copy ContextVars. Bind the allowance to this
        # environment so concurrent calls and nested environment views cannot leak it.
        limit = MAX_OUTPUT if bounded_capture else MAX_FULL_OUTPUT
        token = _output_capture.set((self, limit))
        try:
            return super().execute(command, cwd, bounded_capture=bounded_capture, **kwargs)
        finally:
            _output_capture.reset(token)

    def _run_bash(self, cmd_string, *, login=False, timeout=120, stdin_data=None):
        capture = _output_capture.get()
        limit = capture[1] if capture is not None and capture[0] is self else MAX_OUTPUT
        return self.client.spawn(cmd_string, login, timeout, stdin_data, output_limit=limit)

    def fetch_file(self, remote_path: str, local_dest: Path, *, max_bytes: int) -> None:
        """Native file extraction over bounded selected-task operations."""
        if not isinstance(remote_path, str) or not remote_path or "\0" in remote_path:
            raise FileFetchError("Task file path is invalid")
        if type(max_bytes) is not int or max_bytes < 0:
            raise FileFetchError("Task file delivery requires a nonnegative byte bound")
        # Keep the pinned native fetch allowance across the whole transfer,
        # rather than granting it again for each chunk. Trial cancellation and
        # the shared fatal latch remain authoritative over every operation.
        deadline = time.monotonic() + 300
        marker = "HARBOR_FETCH_" + uuid.uuid4().hex + ":"

        def invalid_reply():
            self.client.boundary.trip("Task file transfer returned invalid evidence")
            raise BoundaryAbort("Task file transfer returned invalid evidence")

        def read(offset=0, count=0, identity=None):
            self.client.boundary.guard()
            remaining = deadline - time.monotonic()
            if remaining <= 0:
                raise FileFetchError("Task file transfer exceeded the native allowance")
            request = {"path": remote_path, "max_bytes": max_bytes, "marker": marker,
                       "identity": identity, "offset": offset, "count": count}
            result = self.execute(shlex.join([
                "env", "-u", "PYTHONPATH", "-u", "PYTHONHOME", self.client.task_runtime.python,
                "-I", "-B", "-c", _FETCH_READ, json.dumps(request),
            ]), timeout=remaining, rewrite_compound_background=False)
            self.client.boundary.guard()
            if result.get("returncode") != 0 or result.get("stdin_error"):
                raise FileFetchError("Task file read did not complete")
            lines = [line[len(marker):] for line in result.get("output", "").splitlines()
                     if line.startswith(marker)]
            try:
                reply = json.loads(lines[0]) if len(lines) == 1 else None
            except ValueError:
                invalid_reply()
            if not isinstance(reply, dict):
                invalid_reply()
            if reply == {"error": "file_unavailable"}:
                raise FileFetchError("Task file is missing, unreadable, oversized or changed")
            return reply

        metadata = read()
        stamp, expected = metadata.get("identity"), metadata.get("sha256")
        if (set(metadata) != {"identity", "sha256"} or not isinstance(stamp, list) or len(stamp) != 5
                or any(type(value) is not int for value in stamp) or not 0 <= stamp[2] <= max_bytes
                or not isinstance(expected, str) or len(expected) != 64
                or any(value not in "0123456789abcdef" for value in expected)):
            invalid_reply()
        destination = Path(local_dest)
        # The destination is host-owned output storage. Failed/cancelled reads
        # remove this private partial file and preserve any prior destination.
        with ExitStack() as cleanup:
            with tempfile.NamedTemporaryFile(
                dir=destination.parent, prefix=".harbor-fetch-", delete=False,
            ) as partial:
                cleanup.callback(Path(partial.name).unlink, missing_ok=True)
                digest = hashlib.sha256()
                for offset in range(0, stamp[2], _FETCH_CHUNK):
                    count = min(_FETCH_CHUNK, stamp[2] - offset)
                    reply = read(offset, count, stamp)
                    if (set(reply) != {"offset", "data"} or type(reply.get("offset")) is not int
                            or reply["offset"] != offset or not isinstance(reply.get("data"), str)
                            or len(reply["data"]) != 4 * ((count + 2) // 3)):
                        invalid_reply()
                    try:
                        data = base64.b64decode(reply["data"], validate=True)
                    except ValueError:
                        invalid_reply()
                    if len(data) != count:
                        invalid_reply()
                    partial.write(data)
                    digest.update(data)
                if digest.hexdigest() != expected:
                    raise FileFetchError("Task file changed during transfer")
                if time.monotonic() >= deadline:
                    raise FileFetchError("Task file transfer exceeded the native allowance")
                self.client.boundary.guard()
                partial.flush()
            self.client.boundary.guard()
            os.replace(partial.name, destination)

    def cleanup(self):
        # Native child agents can release their view, but only the campaign
        # owner closes the shared Harbor generation after the root has drained.
        pass


class HarborTerminalProvider(TerminalEnvironmentProvider):
    name = "harbor_task"
    is_remote = True
    is_container = True

    def __init__(self, client):
        self.client = client

    def is_available(self):
        return not self.client.boundary.fatal

    def create_environment(self, *, cwd, timeout, task_id="default", **kwargs):
        self.client.boundary.guard()
        return HarborEnvironment(self.client, cwd, timeout)


def register(ctx):
    boundary = current_trial()
    client = boundary.task_client
    register_native_tools(ctx, boundary, client)
    register_host_profiles(ctx, boundary)
    provider = HarborTerminalProvider(client)
    ctx.register_terminal_environment_provider(provider)
    ctx.register_middleware("llm_execution", lambda **kwargs: boundary.middleware("model", **kwargs))
    ctx.register_middleware("tool_execution", lambda **kwargs: boundary.middleware("tool", **kwargs))
    from agent.terminal_env_registry import get_provider

    if get_provider(provider.name) is not provider:
        boundary.trip("Native terminal provider registration failed")
        raise BoundaryAbort("Native terminal provider registration failed")
    boundary.plugin_ready = True


def register_native_tools(ctx, boundary, client):
    from tools.registry import discover_builtin_tools, registry
    from toolsets import resolve_multiple_toolsets

    discover_builtin_tools()
    requested = resolve_multiple_toolsets(boundary.toolsets)
    families = {"web", "browser", "browser-cdp", "browser-use", "tts"}
    names = {name for name in requested if registry.get_toolset_for_tool(name) in families}
    snapshot = client.native_snapshot
    if not names:
        return
    if not snapshot or snapshot.get("generation") != boundary.generation or snapshot.get("terminal_env") != "local":
        boundary.trip("Native task capability snapshot is absent")
        raise BoundaryAbort("Native task capability snapshot is absent")
    if snapshot.get("toolsets") != boundary.toolsets or not isinstance(snapshot.get("model_tools"), dict):
        boundary.trip("Native task finalized profile is absent or selects different toolsets")
        raise BoundaryAbort("Native task finalized profile was not admitted")
    entries = snapshot["entries"]
    if snapshot.get("browser_runtime", {}).get("missing"):
        boundary.trip("Selected native browser runtime is missing")
        raise BoundaryAbort("Selected native browser runtime is missing")
    if any(registry.get_toolset_for_tool(name) == "tts" for name in names):
        from offline_hermes_web import admit_tts_runtime

        tts = admit_tts_runtime()
        if tts["missing"] or tts != snapshot.get("tts_runtime"):
            boundary.trip("Selected native TTS runtime/configuration differs from the task")
            raise BoundaryAbort("Selected native TTS runtime/configuration is unqualified")
    enabled = set()
    for name in sorted(names):
        original = registry.get_entry(name)
        observed = entries.get(name)
        if (
            not observed or type(observed.get("available")) is not bool
            or observed.get("schema") != original.schema
            or observed.get("toolset") != original.toolset or observed.get("gap")
        ):
            boundary.trip("Required native task capability or schema is unqualified")
            raise BoundaryAbort("Unqualified native task capability: " + name)
        definitions = observed["definitions"]
        if observed["available"]:
            if len(definitions) != 1 or definitions[0].get("function", {}).get("name") != name:
                boundary.trip("Native selected schema is malformed")
                raise BoundaryAbort("Native selected schema is malformed")
            # Keep native model-dependent schema computation on the host. The
            # model middleware compares its actual result with task admission.
            schema = original.schema
            enabled.add(name)
        else:
            if definitions or observed.get("filter") != "native_check_fn":
                boundary.trip("Native filtered-tool evidence is missing")
                raise BoundaryAbort("Native filtered-tool evidence is missing")
            schema = original.schema

        def check(tool=name):
            boundary.guard()
            return entries[tool]["available"] is True

        def handle(args, _tool=name, **kwargs):
            # Also covers a direct registry caller that skips outer middleware.
            return boundary.middleware("tool", tool_name=_tool, args=args, next_call=None, **kwargs)

        async def handle_async(args, _handle=handle, **kwargs):
            return await asyncio.to_thread(_handle, args, **kwargs)

        handle_used = handle_async if original.is_async else handle
        registration = ctx.register_tool(
            name=name, toolset=original.toolset, schema=schema,
            handler=handle_used, check_fn=check, requires_env=original.requires_env,
            is_async=original.is_async, description=original.description,
            emoji=original.emoji, override=True,
        )
        replacement = registry.get_entry(name)
        if registration is None or replacement.handler is not handle_used:
            boundary.trip("Native task tool registration was refused")
            raise BoundaryAbort("Native task tool registration was refused")
        # Keep the public ToolEntry's result budget: PluginContext's narrow
        # registration signature does not expose this native metadata field.
        replacement.max_result_size_chars = original.max_result_size_chars
        replacement.dynamic_schema_overrides = original.dynamic_schema_overrides
        if original.toolset == "tts" and observed["available"]:
            # Native TTS's sole dynamic field describes its ACTIVE output home.
            # Keep native schema computation, replacing only that task-owned
            # path hint from this generation's actual native definition.
            task_hint = definitions[0]["function"]["parameters"]["properties"]["output_path"]["description"]

            def task_schema(native=original.dynamic_schema_overrides, hint=task_hint):
                boundary.guard()
                overrides = deepcopy(native())
                overrides["parameters"]["properties"]["output_path"]["description"] = hint
                return overrides

            replacement.dynamic_schema_overrides = task_schema
    client.native_routes = frozenset(enabled)
    boundary.admit_native_routes(enabled)
