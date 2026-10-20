"""One trial's admission, fatal state, and settlement accounting.

The host process is dedicated to one Harbor trial. Native Hermes workers may
contain an exception or lose a ContextVar; neither gives them fresh authority.
The provider and middleware retain this same object for their entire lifetime.
"""

import threading
import time
from collections.abc import Callable, Iterator
from contextlib import contextmanager
from contextvars import ContextVar
from copy import deepcopy
from dataclasses import dataclass
from typing import Any


class BoundaryAbort(BaseException):
    """Local unwinding only; the shared latch determines the trial outcome."""


class UnsettledOperations(RuntimeError):
    """Harbor must retain custody; a local return did not prove settlement."""


@dataclass(eq=False)
class Operation:
    number: int
    kind: str
    cleanup: bool
    dispatched: bool = False
    uncertain: bool = False


_current: ContextVar["TrialBoundary | None"] = ContextVar("harbor_hermes_trial", default=None)
_cleanup: ContextVar[object | None] = ContextVar("harbor_hermes_cleanup", default=None)
_process_lock = threading.Lock()
_process_boundary: "TrialBoundary | None" = None


class TrialBoundary:
    def __init__(
        self, generation: str, *, qualified_tools: frozenset[str] = frozenset(), max_pending: int = 256,
    ):
        self.generation = generation
        self._condition = threading.Condition(threading.RLock())
        self._accepting = True
        self._closed = False
        self._fatal: str | None = None
        self._root: Any = None
        self._interrupt_started = False
        self._interrupt_failure: BaseException | None = None
        self._pending: dict[int, Operation] = {}
        self._next = 0
        self._max_pending = max_pending
        self._cleanup_key = object()
        self._root_closed = False
        self._supplied_tools: set[str] = set()
        self._supplied_schemas: dict[str, dict] = {}
        self._qualified_tools = qualified_tools

    @property
    def fatal(self) -> bool:
        with self._condition:
            return self._fatal is not None

    @property
    def pending(self) -> int:
        with self._condition:
            return len(self._pending)

    @property
    def supplied_tools(self) -> frozenset[str]:
        with self._condition:
            return frozenset(self._supplied_tools)

    @property
    def supplied_schemas(self) -> dict[str, dict]:
        with self._condition:
            return deepcopy(self._supplied_schemas)

    @property
    def root(self) -> Any:
        with self._condition:
            return self._root

    def _interrupt_root(self) -> None:
        with self._condition:
            if self._root is None or self._interrupt_started:
                return
            self._interrupt_started = True
            root = self._root
        try:
            root.interrupt(hard_cancel=True)
        # Record hard-interrupt failure for the independent completion gate.
        except BaseException as exc:  # noqa: BLE001
            with self._condition:
                self._interrupt_failure = exc
                self._condition.notify_all()

    def trip(self, reason: str) -> None:
        # Revoke admission before invoking Hermes: interruption can reenter tool
        # cleanup or wake another worker. No callback executes under this lock.
        with self._condition:
            self._accepting = False
            if self._fatal is None:
                self._fatal = reason
            self._condition.notify_all()
        self._interrupt_root()

    def bind_root(self, root: Any) -> None:
        """Only a fully initialized root may receive the real hard interrupt."""
        with self._condition:
            if self._root is not None:
                raise RuntimeError("A trial already owns its root agent")
            self._root = root
            revoked = not self._accepting
        if revoked:
            self.trip("Admission closed while the root was initializing")
            raise BoundaryAbort("Root initialized after trial admission closed")

    def guard(self) -> None:
        with self._condition:
            if self._fatal is not None or not self._accepting:
                raise BoundaryAbort("Harbor trial admission is closed")

    def admit(self, kind: str) -> Operation:
        cleanup = _cleanup.get() is self._cleanup_key
        with self._condition:
            if self._closed or (not cleanup and not self._accepting):
                raise BoundaryAbort("Harbor trial admission is closed")
            if len(self._pending) >= self._max_pending:
                exhausted = True
            else:
                exhausted = False
                self._next += 1
                operation = Operation(self._next, kind, cleanup)
                self._pending[operation.number] = operation
        if exhausted:
            self.trip("Trial operation capacity exceeded")
            raise BoundaryAbort("Trial operation capacity exceeded")
        return operation

    def dispatch(self, operation: Operation) -> None:
        """Commit one accepted operation; pending work cannot cross revocation."""
        with self._condition:
            self._require_owned(operation)
            if operation.dispatched:
                raise RuntimeError("An operation cannot dispatch twice")
            if self._closed or (not operation.cleanup and not self._accepting):
                del self._pending[operation.number]
                self._condition.notify_all()
                raise BoundaryAbort("Accepted operation was revoked before dispatch")
            operation.dispatched = True

    def _require_owned(self, operation: Operation) -> None:
        if self._pending.get(operation.number) is not operation:
            raise RuntimeError("Operation does not belong to this trial's pending set")

    def settle(self, operation: Operation, *, remote_confirmed: bool = False) -> None:
        with self._condition:
            self._require_owned(operation)
            if operation.uncertain and not remote_confirmed:
                raise UnsettledOperations("Uncertain remote operation has no settlement receipt")
            del self._pending[operation.number]
            self._condition.notify_all()

    def uncertain(self, operation: Operation, reason: str) -> None:
        with self._condition:
            self._require_owned(operation)
            operation.uncertain = True
        self.trip(reason)

    def close_admission(self) -> None:
        with self._condition:
            self._accepting = False
            self._condition.notify_all()

    @contextmanager
    def cleanup_for(self, operation: Operation) -> Iterator[None]:
        """Allow cancellation/receipt reads for an already-owned remote family."""
        with self._condition:
            self._require_owned(operation)
            if not operation.dispatched:
                raise RuntimeError("Cleanup requires an accepted operation")
        token = _cleanup.set(self._cleanup_key)
        try:
            yield
        finally:
            _cleanup.reset(token)

    @contextmanager
    def owned_cleanup(self) -> Iterator[None]:
        """Only the campaign owner enters this scope, never a model/tool callback."""
        self.close_admission()
        token = _cleanup.set(self._cleanup_key)
        try:
            yield
        finally:
            _cleanup.reset(token)

    def drain(self, timeout: float) -> None:
        deadline = time.monotonic() + timeout
        with self._condition:
            while self._pending:
                remaining = deadline - time.monotonic()
                if remaining <= 0:
                    raise UnsettledOperations("Accepted operations remain unsettled")
                self._condition.wait(remaining)

    def mark_root_closed(self) -> None:
        with self._condition:
            self._root_closed = True

    def finish(self) -> None:
        """Independent completion gate, including failures swallowed by workers."""
        with self._condition:
            if self._pending:
                raise UnsettledOperations("Cannot finish with accepted operations pending")
            self._closed = True
            self._accepting = False
            if self._fatal is not None:
                raise RuntimeError("Hermes trial failed at its task boundary")
            if self._interrupt_failure is not None:
                raise RuntimeError("Hermes root interruption failed") from self._interrupt_failure
            if self._root is None or not self._root_closed:
                raise RuntimeError("Hermes root lifecycle did not complete")

    def admit_native_routes(self, names) -> None:
        with self._condition:
            if self._root is not None or not self._accepting or self._fatal is not None:
                raise BoundaryAbort("Task routes must be admitted before root construction")
            self._qualified_tools |= frozenset(names)

    def middleware(self, kind: str, **kwargs: Any) -> Any:
        # Middleware gets the closure's trial even in a native RPC daemon that
        # contains BaseException or does not retain the originating ContextVar.
        self.guard()
        operation = self.admit(kind)
        self.dispatch(operation)
        try:
            if kind == "model":
                supplied = set()
                with self._condition:
                    for tool in kwargs["request"].get("tools", ()):
                        definition = tool.get("function", tool)
                        name = definition.get("name")
                        if isinstance(name, str):
                            self._supplied_tools.add(name)
                            self._supplied_schemas[name] = deepcopy(tool)
                            supplied.add(name)
                canonical = self.task_client.validate_model_profile(
                    kwargs["request"], api_mode=kwargs.get("api_mode"),
                    context={key: kwargs[key] for key in (
                        "session_id", "model", "provider", "base_url", "api_mode",
                    ) if key in kwargs},
                )
                missing = {canonical.get(name, name) for name in supplied} - self._qualified_tools
            else:
                missing = {kwargs["tool_name"]} - self._qualified_tools
            if missing:
                # Keep the native definitions intact. A catalog entry or an
                # unrelated terminal-provider proof cannot qualify a tool's
                # task-facing file/network path, including nested code RPC.
                self.trip("Supplied tool routes have not been qualified")
                raise BoundaryAbort("Unqualified tool routes: " + ", ".join(sorted(missing)))
            if kind == "tool":
                # Publish and admit the selected environment before cold native
                # tool access, including separately isolated child ownership.
                self.task_client.require_task_environment(kwargs.get("task_id"))
            # Route admission does not require network success. Preserve native
            # NO_NETWORK errors from an owned task route as ordinary results.
            if kind == "tool" and kwargs["tool_name"] in self.task_client.native_routes:
                result = self.task_client.native_dispatch(kwargs["tool_name"], kwargs["args"], kwargs)
            else:
                result = kwargs["next_call"](kwargs["request" if kind == "model" else "args"])
        except Exception as exc:
            # Hermes falls through to next_call when ordinary middleware
            # exceptions precede dispatch. A boundary failure must never run
            # the controller's original native handler as a fallback.
            self.trip("Campaign middleware failed before confirmed completion")
            raise BoundaryAbort("Campaign middleware cannot fall through") from exc
        finally:
            # This only settles the local callback. Each remote operation has
            # its own ticket and survives local unwinding until acknowledged.
            self.settle(operation)
        self.guard()
        return result


@contextmanager
def trial_context(boundary: TrialBoundary) -> Iterator[None]:
    """Install before importing/constructing the root; the host owns one trial."""
    global _process_boundary
    with _process_lock:
        if _process_boundary is not None:
            raise RuntimeError("A host process cannot serve two Harbor trials")
        _process_boundary = boundary
    token = _current.set(boundary)
    try:
        yield
    finally:
        _current.reset(token)
        # Keep a failed/unsettled trial bound: late native workers must see the
        # same closed authority, never an absent scope that permits dispatch.
        with _process_lock:
            if boundary._closed and not boundary.fatal and not boundary.pending:
                _process_boundary = None


def current_trial() -> TrialBoundary:
    with _process_lock:
        boundary = _process_boundary
    if boundary is None:
        raise BoundaryAbort("No Harbor trial is installed")
    contextual = _current.get()
    if contextual is not None and contextual is not boundary:
        raise BoundaryAbort("Mismatched Harbor trial context")
    return boundary


def construct_root(boundary: TrialBoundary, factory: Callable[[], Any]) -> Any:
    """Called inside trial_context, after provider and middleware registration."""
    if current_trial() is not boundary:
        raise BoundaryAbort("Root construction has no matching trial context")
    boundary.guard()
    try:
        root = factory()
    except BaseException:
        boundary.trip("Hermes root constructor failed")
        raise
    boundary.bind_root(root)
    return root
