"""Harbor-owned finite exec transport; inference never enters this channel."""

import asyncio
import base64
import hashlib
import json
import math
import os
import shlex
import signal
import tempfile
import uuid
from dataclasses import asdict
from pathlib import Path

from anonbench1_adapters.anonbench1_pins import NODE_VERSION
from anonbench1_adapters.hermes_source import TaskHermesRuntime, UpstreamHermesSource
from anonbench1_adapters.offline_hermes_state import UnsettledOperations
from anonbench1_adapters.offline_hermes_task import MAX_OUTPUT
from anonbench1_adapters.resource_telemetry import observe_process


async def settle_owned(task):
    """Repeated caller cancellation cannot abandon an already accepted operation."""
    cancelled = False
    while not task.done():
        try:
            await asyncio.shield(task)
        except asyncio.CancelledError:
            cancelled = True
    value = task.result()
    if cancelled:
        raise asyncio.CancelledError
    return value


class HarborHermesTransport:
    def __init__(self, environment, task_runtime: TaskHermesRuntime, source_identity: UpstreamHermesSource):
        self.environment = environment
        self.task_runtime = task_runtime
        self.source_identity = source_identity
        self.task_python = task_runtime.python
        self.runtime_receipt = None
        self.generation = uuid.uuid4().hex
        self.root = "/tmp/harbor-hermes-" + self.generation
        self.helper = self.root + ".py"
        self.accepting = False
        self.fatal = False
        self._attempted: set[str] = set()
        self._inflight: set[asyncio.Task] = set()
        self._closed = False
        self._preparation_started = False
        self._native_worker = None
        self._native_calls = set()
        self._native_deadline = None

    async def prepare(self):
        if self._preparation_started:
            raise RuntimeError("Task preparation cannot be replayed")
        self._preparation_started = True
        try:
            await self._admit_runtime()
            await self._prepare_helper()
        except BaseException:
            self.poison()
            raise

    async def _admit_runtime(self):
        # Only reviewed verifier code enters this private directory. Source and
        # venv provisioning belong to the installer; this path never installs.
        directory = self.root + "-admission"
        result = await self.environment.exec(
            command="mkdir -m 700 -- " + shlex.quote(directory), timeout_sec=10,
        )
        if result.return_code:
            raise RuntimeError("Task runtime admission directory was not created")
        adapter = Path(__file__).parent
        files = [adapter / "hermes_source.py", adapter.parent.parent / "campaign_pins.py"]
        expected = []
        for source in files:
            target = directory + "/" + source.name
            await self.environment.upload_file(source, target)
            expected.append(hashlib.sha256(source.read_bytes()).hexdigest() + "  " + target)
        result = await self.environment.exec(
            command="sha256sum -- " + shlex.join([directory + "/" + p.name for p in files]),
            timeout_sec=10,
        )
        if result.return_code or (result.stdout or "").splitlines() != expected:
            raise RuntimeError("Task runtime verifier bytes were not admitted")
        runtime = self.task_runtime
        loader = "import sys; sys.path.insert(0, " + repr(directory) + "); from anonbench1_adapters.hermes_source import main; main()"
        result = await self.environment.exec(command=shlex.join([
            runtime.python, "-I", "-B", "-c", loader, "verify", "--task-runtime",
            "--source", runtime.source, "--identity", runtime.source_identity,
            "--identity-sha256", runtime.source_identity_sha256,
            "--node", runtime.node,
        ]), timeout_sec=60)
        if result.return_code:
            raise RuntimeError("Task Hermes interpreter/source/document dependency admission failed")
        receipt = json.loads(result.stdout or "")
        expected_receipt = {
            "python_sha256": runtime.python_sha256,
            "python_version": runtime.python_version,
            "anydoc_version": "0.2.4",
            "node_sha256": runtime.node_sha256, "node_version": NODE_VERSION,
            "source_identity": asdict(self.source_identity),
        }
        if receipt != expected_receipt:
            raise RuntimeError("Task runtime differs from the reviewed interpreter/source identity")
        self.runtime_receipt = receipt

    async def _prepare_helper(self):
        source = Path(__file__).with_name("offline_hermes_task.py")
        await self.environment.upload_file(source, self.helper)
        digest = hashlib.sha256(source.read_bytes()).hexdigest()
        verification = await self.environment.exec(
            command="sha256sum " + shlex.quote(self.helper), timeout_sec=10,
        )
        if verification.return_code or not (verification.stdout or "").startswith(digest + " "):
            raise RuntimeError("Task helper bytes were not admitted")
        native = source.with_name("offline_hermes_web.py")
        self.native_helper = self.root + "-web.py"
        await self.environment.upload_file(native, self.native_helper)
        verification = await self.environment.exec(
            command="sha256sum " + shlex.quote(self.native_helper), timeout_sec=10,
        )
        if verification.return_code or (verification.stdout or "").split() != [
            hashlib.sha256(native.read_bytes()).hexdigest(), self.native_helper,
        ]:
            raise RuntimeError("Native web/browser worker bytes were not admitted")
        result = await self._rpc("init", None)
        if result != {"generation": self.generation}:
            raise RuntimeError("Task generation did not initialize")
        self.accepting = True

    async def _rpc(self, action, payload):
        args = [self.task_python, "-I", self.helper, action, self.root, self.generation]
        if payload is not None:
            raw = json.dumps(payload, separators=(",", ":")).encode()
            if len(raw) > 1_000_000:
                raise ValueError("Task request exceeds its bounded transport")
            if action in {"start", "native_submit"}:
                operation = payload["operation"]
                if not isinstance(operation, str) or len(operation) != 32 or any(
                    char not in "0123456789abcdef" for char in operation
                ):
                    raise ValueError("Invalid task operation identity")
                # Full shell/file payloads do not fit Linux's per-argument
                # limit. Harbor upload publishes data; start alone executes it.
                with tempfile.NamedTemporaryFile() as upload:
                    upload.write(raw)
                    upload.flush()
                    await self.environment.upload_file(
                        Path(upload.name), self.root + "/input-" + operation + ".json",
                    )
                args.append(operation)
            else:
                args.append(base64.b64encode(raw).decode())
        # exec is Harbor's public finite operation; NO_NETWORK needs no SSH,
        # streaming connection, Docker internals, or forwarded credentials.
        result = await self.environment.exec(command=shlex.join(args), timeout_sec=10)
        if result.return_code != 0:
            raise RuntimeError("Task transport operation failed")
        try:
            response = json.loads(result.stdout or "")
        except (ValueError, TypeError) as exc:
            raise RuntimeError("Task transport lost its receipt") from exc
        if not isinstance(response, dict):
            # Invalid peer payloads retain the transport RuntimeError contract.
            raise RuntimeError("Task transport returned an invalid receipt")  # noqa: TRY004
        return response

    def poison(self):
        self.accepting = False
        self.fatal = True

    async def prepare_native_tools(self, *, cwd, configuration, deadline=None):
        """One persistent task family; no model config or host env is staged."""
        if set(configuration) != {"config_path", "config_sha256", "names", "executables", "toolsets", "timeout"}:
            raise ValueError("Native task bootstrap must contain only its explicit tool-runtime inputs")
        if not isinstance(configuration["toolsets"], list) or not configuration["toolsets"] or any(
            not isinstance(name, str) or not name for name in configuration["toolsets"]
        ):
            raise ValueError("Native task snapshot requires the complete selected toolsets")
        if self._native_worker is not None:
            raise RuntimeError("Native worker launch cannot be replayed")
        allowance = configuration["timeout"]
        if type(allowance) not in (int, float) or not math.isfinite(allowance) or not 0 < allowance <= 86400:
            raise ValueError("Native worker requires a finite admitted remaining trial allowance")
        # One absolute campaign allowance covers startup, every serial queue
        # wait and native call. Inner tool timers still belong to Hermes.
        self._native_deadline = asyncio.get_running_loop().time() + allowance
        if deadline is not None:
            if type(deadline) not in (int, float) or not math.isfinite(deadline):
                raise ValueError("Native worker requires a finite admitted deadline")
            self._native_deadline = min(self._native_deadline, deadline)
            allowance = self._native_deadline - asyncio.get_running_loop().time()
            if allowance <= 0:
                raise TimeoutError("Native trial allowance expired before worker launch")
        operation = uuid.uuid4().hex
        self._native_worker = operation
        boot = {
            **configuration, "runtime": asdict(self.task_runtime), "cwd": cwd,
            "root": self.root, "generation": self.generation, "worker": operation,
            "helper": self.helper, "admission": self.root + "-admission",
        }
        request = {
            "operation": operation,
            "request": {
                "command": shlex.join([
                    "env", "-u", "PYTHONPATH", "-u", "PYTHONHOME",
                    self.task_python, "-I", "-B", self.native_helper,
                ]),
                "cwd": cwd, "login": False, "timeout": allowance, "output_limit": MAX_OUTPUT,
                "stdin": base64.b64encode(json.dumps(boot).encode()).decode(),
            },
        }
        try:
            if await self.call("start", request) != {"accepted": operation}:
                raise RuntimeError("Native worker acceptance mismatch")
            snapshot = await self.native_call({"kind": "snapshot"})
            if snapshot.get("source_tree") != self.source_identity.source_tree:
                raise RuntimeError("Native task tools imported a different source tree")
            self.native_snapshot = snapshot
            return snapshot
        except BaseException:
            self.poison()
            await self.close()
            raise

    async def native_call(self, payload):
        if not self.accepting or self.fatal or self._native_worker is None:
            raise RuntimeError("Native task dispatch admission is closed")
        operation = uuid.uuid4().hex
        if len(self._native_calls) >= 4096:
            self.poison()
            raise RuntimeError("Native task operation capacity exceeded")
        self._native_calls.add(operation)
        request = {**payload, "operation": operation, "worker": self._native_worker}

        async def owned_rpc(action):
            # Trial expiry revokes tool authority, but cannot abandon a finite
            # upload/exec that might already have published a request.
            return await settle_owned(asyncio.create_task(self._rpc(action, request)))

        async def invoke():
            receipt = await owned_rpc("native_submit")
            if receipt != {"accepted": operation, "worker": self._native_worker}:
                raise RuntimeError("Native task publication receipt is uncertain")
            while self.accepting and not self.fatal:
                response = await owned_rpc("native_poll")
                if response.get("worker") != self._native_worker:
                    raise RuntimeError("Native result belongs to a different worker")
                if response.get("operation") == operation and "result" in response:
                    return response["result"]
                if response.get("pending") != operation:
                    raise RuntimeError("Native operation receipt mismatch")
                await asyncio.sleep(0.05)
            raise UnsettledOperations("Native task was revoked while a handler was accepted")

        async def within_trial():
            if self._native_deadline is None:
                raise RuntimeError("Native task has no admitted absolute lifetime")
            if asyncio.get_running_loop().time() >= self._native_deadline:
                raise TimeoutError("Admitted native trial lifetime expired before publication")
            async with asyncio.timeout_at(self._native_deadline):
                return await invoke()

        task = asyncio.create_task(within_trial())
        self._inflight.add(task)
        try:
            return await settle_owned(task)
        except BaseException:
            self.poison()
            # Settlement starts only after this invocation has stopped. The
            # generation supervisor owns any accepted handler/browser writes.
            if task.done():
                self._inflight.discard(task)
            await self.close()
            raise
        finally:
            if task.done():
                self._inflight.discard(task)

    async def call(self, action, payload):
        if self._closed or (not self.accepting and action not in {"cancel", "poll"}):
            raise RuntimeError("Harbor task admission is closed")
        operation = payload.get("operation")
        if action == "start":
            if not self.accepting or operation in self._attempted:
                raise RuntimeError("A task start cannot be replayed")
            if len(self._attempted) >= 4096:
                self.poison()
                raise RuntimeError("Task operation capacity exceeded")
            self._attempted.add(operation)
        elif action not in {"poll", "cancel"} or operation not in self._attempted:
            self.poison()
            raise RuntimeError("Task operation does not belong to this trial")
        # Membership is recorded before yielding, including uncertain starts.
        task = asyncio.create_task(self._rpc(action, payload))
        self._inflight.add(task)
        try:
            return await settle_owned(task)
        except BaseException:
            self.poison()
            raise
        finally:
            if task.done():
                self._inflight.discard(task)

    async def _close(self):
        self.accepting = False
        if self._inflight:
            await asyncio.gather(*tuple(self._inflight), return_exceptions=True)
        if self._native_worker is not None:
            try:
                await self._rpc("native_stop", {"worker": self._native_worker})
                deadline = asyncio.get_running_loop().time() + 3
                while asyncio.get_running_loop().time() < deadline:
                    state = await self._rpc("poll", {"operation": self._native_worker, "offset": 0})
                    if state.get("settled") == {"operation": self._native_worker}:
                        break
                    await asyncio.sleep(0.05)
            # Lost native stop observation must not skip whole-generation closure.
            except Exception:  # noqa: BLE001
                # A lost worker-start receipt may mean no family exists. The
                # authoritative whole-generation close must still run below.
                self.poison()
        # This serializes with remote start admission. A missing start receipt
        # can only settle after this closure and the complete generation ledger.
        await self._rpc("close", {})
        deadline = asyncio.get_running_loop().time() + 30
        while True:
            state = await self._rpc("settlement", {})
            accepted = set(state.get("accepted", []))
            settled = set(state.get("settled", []))
            if (
                state.get("generation") != self.generation
                or state.get("closed") is not True
                or not accepted <= self._attempted
                or not settled <= accepted
            ):
                raise UnsettledOperations("Task settlement ledger does not match this generation")
            if accepted == settled:
                self._closed = True
                failed = state.get("failed", [])
                if failed:
                    self.poison()
                return {"generation": self.generation, "settled": sorted(self._attempted), "failed": failed}
            if asyncio.get_running_loop().time() >= deadline:
                # Preserve the last received ledger before memoized close
                # failures form an exception cycle. No further task RPC runs.
                ledger = {"generation": state["generation"], "closed": state["closed"]}
                for name, identifiers in (
                    ("accepted", accepted), ("settled", settled),
                    ("failed", set(state.get("failed", []))), ("outstanding", accepted - settled),
                ):
                    ordered = sorted(identifiers)
                    ledger[name] = {"count": len(ordered), "ids": ordered[:16],
                                    "truncated": len(ordered) > 16}
                error = UnsettledOperations("Task families remain unsettled; Harbor teardown is required")
                # Native IDs are 32 hex characters; four capped sets keep this
                # note below 3000 ASCII bytes, within the existing stream bound.
                error.add_note("last_task_settlement=" + json.dumps(ledger, separators=(",", ":")))
                raise error
            await asyncio.sleep(0.05)

    async def close(self):
        # One finalization owner covers admission, outstanding finite calls,
        # closure publication, reconciliation and confirmed remote settlement.
        if not hasattr(self, "_closing"):
            self._closing = asyncio.create_task(self._close())
        try:
            return await settle_owned(self._closing)
        except BaseException:
            self.poison()
            raise

    async def run_host(self, argv, *, env, cwd, bootstrap, log_path, qualification_only=False, execution_mode=None):
        """Own the prepared generation even when no host child can launch."""
        active = set()
        writer_lock = asyncio.Lock()
        channel_failure = asyncio.get_running_loop().create_future()
        log = launch = proc = receiver = waiter = None
        settlement = None
        telemetry = None

        def responded(task):
            active.discard(task)
            if not task.cancelled() and task.exception() is not None:
                self.poison()
                if not channel_failure.done():
                    channel_failure.set_result(task.exception())

        async def respond(request):
            try:
                if request["action"] == "close":
                    result = await self.close()
                elif request["action"] == "native":
                    result = await self.native_call(request["payload"])
                else:
                    result = await self.call(request["action"], request["payload"])
                message = {"id": request["id"], "result": result}
            # Cancellation and transport failure must close IPC admission fatally.
            except BaseException as exc:  # noqa: BLE001
                self.poison()
                # A native close may itself be waiting on an auxiliary
                # worker. The parent must enter bounded teardown without
                # depending on the child's receipt or subsequent exit.
                if not channel_failure.done():
                    channel_failure.set_result(exc)
                message = {"id": request["id"], "fatal": True}
            async with writer_lock:
                proc.stdin.write((json.dumps(message) + "\n").encode())
                await proc.stdin.drain()

        async def receive():
            seen = set()
            while line := await proc.stdout.readline():
                request = json.loads(line)
                number = request["id"]
                if type(number) is not int or number in seen or len(active) >= 256:
                    self.poison()
                    raise RuntimeError("Host task IPC admission failed")
                seen.add(number)
                task = asyncio.create_task(respond(request))
                active.add(task)
                task.add_done_callback(responded)
            if active:
                await asyncio.gather(*tuple(active))
        async def finalize_child():
            nonlocal proc, waiter
            if launch is None:
                return
            # The launch task retains ambiguous spawn custody across repeated
            # cancellation. A rejected spawn has no returned child handle.
            proc = await launch
            if waiter is None:
                waiter = asyncio.create_task(proc.wait())
            proc.stdin.close()
            try:
                os.killpg(proc.pid, signal.SIGTERM)
            except ProcessLookupError:
                pass
            try:
                await asyncio.wait_for(asyncio.shield(waiter), 10)
            except TimeoutError:
                try:
                    os.killpg(proc.pid, signal.SIGKILL)
                except ProcessLookupError:
                    pass
                try:
                    await asyncio.wait_for(asyncio.shield(waiter), 5)
                except TimeoutError as exc:
                    raise UnsettledOperations("Owned host child did not reap after SIGKILL") from exc
            # A reaped group leader does not prove its children exited.
            # This group was created exclusively by our launch above.
            try:
                os.killpg(proc.pid, signal.SIGKILL)
            except ProcessLookupError:
                pass
            deadline = asyncio.get_running_loop().time() + 5
            while True:
                try:
                    os.killpg(proc.pid, 0)
                except ProcessLookupError:
                    break
                if asyncio.get_running_loop().time() >= deadline:
                    raise UnsettledOperations("Owned host process group remains unsettled")
                await asyncio.sleep(0.05)
            if receiver is not None:
                try:
                    await asyncio.wait_for(
                        asyncio.gather(receiver, *tuple(active), return_exceptions=True), 5,
                    )
                except TimeoutError as exc:
                    raise UnsettledOperations("Owned host IPC did not drain") from exc
        async def finalize():
            nonlocal settlement
            try:
                settlement = await self.close()
            finally:
                try:
                    await finalize_child()
                finally:
                    if log is not None:
                        log.close()

        try:
            mode = execution_mode or ("qualification" if qualification_only is True else None)
            if mode not in ("qualification", "scored") or qualification_only is not (mode == "qualification"):
                raise RuntimeError("An explicit consistent qualification or scored mode is required")
            if not self.accepting or self.fatal or self.runtime_receipt is None:
                raise RuntimeError("Host launch requires an admitted task runtime and open generation")
            if bootstrap["generation"] != self.generation:
                raise RuntimeError("Host bootstrap does not own this task generation")
            bootstrap = {
                **bootstrap, "qualification_only": qualification_only, "execution_mode": mode,
                "task_runtime": asdict(self.task_runtime),
                "task_runtime_receipt": self.runtime_receipt,
                "session_export": str(log_path.parent / "hermes-session.jsonl"),
                "native_snapshot": getattr(self, "native_snapshot", None),
            }
            log = log_path.open("xb")
            launch = asyncio.create_task(asyncio.create_subprocess_exec(
                *argv, env=env, cwd=cwd, stdin=asyncio.subprocess.PIPE,
                stdout=asyncio.subprocess.PIPE, stderr=log, start_new_session=True,
                limit=2_000_000,
            ))
            proc = await asyncio.shield(launch)
            telemetry = observe_process(proc.pid)
            telemetry.__enter__()
            assert proc.stdin is not None and proc.stdout is not None
            proc.stdin.write((json.dumps(bootstrap) + "\n").encode())
            await proc.stdin.drain()
            receiver = asyncio.create_task(receive())
            waiter = asyncio.create_task(proc.wait())
            done, _ = await asyncio.wait(
                {waiter, receiver, channel_failure}, return_when=asyncio.FIRST_COMPLETED,
            )
            if channel_failure in done:
                raise RuntimeError("Host task IPC failed") from channel_failure.result()
            if receiver in done:
                receiver.result()
            code = await waiter
            await receiver
            if code != 0 or self.fatal:
                raise RuntimeError("Hermes host run failed; completion is unqualified")
        except BaseException:
            self.poison()
            raise
        finally:
            try:
                await settle_owned(asyncio.create_task(finalize()))
            except BaseException:
                self.poison()
                raise
            finally:
                if telemetry is not None:
                    telemetry.__exit__(None, None, None)
        # Close may poison without raising. A zero host exit before finalization
        # cannot acknowledge that late failure or undrained IPC.
        if (
            self.fatal or self.accepting or not self._closed
            or channel_failure.done() or not isinstance(settlement, dict)
            or settlement.get("generation") != self.generation
            or settlement.get("failed") != []
        ):
            self.poison()
            raise UnsettledOperations("Owned runner/task finalization did not confirm clean settlement")
        return {"generation": self.generation, "confirmed_closed": True,
                "qualification_only": qualification_only, "execution_mode": mode,
                "usage_status": "unknown", "research_qualified": False}
