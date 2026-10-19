"""Best-effort resource observations for one adapter run, not billed usage.

Task probes include the probe itself. Host observations require an explicitly
registered child; the controller and its cgroup never enter the measurement.
Sampling cannot account for processes born and exited between observations.
"""

import asyncio
import contextvars
import hashlib
import json
import os
import time
from contextlib import contextmanager
from functools import wraps
from pathlib import Path

KEY = "resource_observations"
ARTIFACT = "resource-observations.json"
INTERVAL_SECONDS = 1.0
MAX_SAMPLES = 2048
MAX_HOST_SCOPES = 64
MAX_PROBE_FAILURES = 64

# Only numeric counters and an internal cgroup identity leave the task. The
# identity is hashed before publication; no command lines or environment are read.
TASK_PROBE = r"""/bin/sh -c '
pages=$(getconf PAGESIZE 2>/dev/null) || pages=0
rss=0; count=0; missed=0; init=unknown
for f in /proc/[0-9]*/stat; do
  if IFS= read -r line 2>/dev/null < "$f"; then
    rest=${line##*) }; set -- $rest
    if [ "$#" -ge 22 ]; then
      shift 19; start=$1; shift 2; resident=$1
      count=$((count + 1)); rss=$((rss + resident))
      [ "$f" != /proc/1/stat ] || init=$start
    else missed=$((missed + 1)); fi
  else missed=$((missed + 1)); fi
done
printf "init\t%s\nprocess_count\t%s\nmissed_processes\t%s\n" "$init" "$count" "$missed"
[ "$pages" -gt 0 ] && printf "process_rss_bytes\t%s\n" "$((rss * pages))"
cg=
while IFS= read -r line; do case "$line" in 0::*) cg=${line#0::};; esac; done < /proc/self/cgroup
case "$cg" in
  /*) case "$cg" in *..*) cg=;; esac;;
  *) cg=;;
esac
if [ -n "$cg" ] && [ -r "/sys/fs/cgroup${cg%/}/cpu.stat" ]; then
  root=/sys/fs/cgroup${cg%/}
  printf "cgroup_identity\t%s\n" "$cg"
  while read -r key value; do
    case "$key" in usage_usec|user_usec|system_usec) printf "cpu_%s\t%s\n" "$key" "$value";; esac
  done 2>/dev/null < "$root/cpu.stat"
  for name in current peak; do
    if IFS= read -r value 2>/dev/null < "$root/memory.$name"; then
      printf "cgroup_memory_%s_bytes\t%s\n" "$name" "$value"
    fi
  done
fi
'"""

_active = contextvars.ContextVar("anonbench1_resource_observer", default=None)


def begin_resource_observation():
    """Begin sampling only after the adapter has admitted execution."""
    observer = _active.get()
    if observer is not None:
        observer.start()


def _process(pid):
    fields = Path(f"/proc/{pid}/stat").read_text().rsplit(")", 1)[1].split()
    return {
        "parent": int(fields[1]), "start": int(fields[19]),
        "cpu": int(fields[11]) + int(fields[12]), "rss": int(fields[21]),
    }


@contextmanager
def observe_process(pid):
    """Observe an owned native child only within admitted execution."""
    observer = _active.get()
    scope = observer.register(pid) if observer is not None else None
    try:
        yield
    finally:
        if scope is not None:
            observer.unregister(scope)


class ResourceObserver:
    def __init__(self, environment):
        self.environment = environment
        self.started = None
        self.stopped = None
        self.stop_event = asyncio.Event()
        self.scopes = {}
        self.task_samples = []
        self.task_probe_attempts = 0
        self.task_probe_failures = []
        self.task_probe_failure_overflow_count = 0
        self.gaps = {"sampling_misses_short_lived_processes"}
        self.truncated = False
        self.worker = None
        self.final_sample_recorded = False

    def register(self, pid):
        if self.started is None:
            return None
        if len(self.scopes) >= MAX_HOST_SCOPES:
            self.gaps.add("host_scope_limit")
            return None
        name = f"host-process-{len(self.scopes) + 1}"
        try:
            identity = _process(pid)["start"]
        except (OSError, ValueError, IndexError):
            self.gaps.add("host_process_identity_unavailable")
            return None
        self.scopes[name] = {"pid": pid, "identity": identity, "active": True,
                             "samples": [], "cpu_seen": {}, "cpu_ticks": 0,
                             "gaps": {"sampling_misses_short_lived_processes"}}
        self._sample_host(self.scopes[name])
        return name

    def unregister(self, name):
        scope = self.scopes[name]
        self._sample_host(scope)
        scope["active"] = False

    def _append(self, samples, row):
        if len(samples) < MAX_SAMPLES:
            samples.append({"elapsed_seconds": time.monotonic() - self.started, **row})
            return True
        else:
            self.truncated = True
            self.gaps.add("sample_limit")
            return False

    def _sample_host(self, scope):
        try:
            root = _process(scope["pid"])
            if root["start"] != scope["identity"]:
                scope["gaps"].add("root_identity_changed")
                return
        except (OSError, ValueError, IndexError):
            scope["gaps"].add("root_exit_before_final_sample")
            return
        processes = {scope["pid"]: root}
        candidates = {}
        for path in Path("/proc").iterdir():
            if not path.name.isdigit() or int(path.name) == scope["pid"]:
                continue
            try:
                candidates[int(path.name)] = _process(int(path.name))
            except (OSError, ValueError, IndexError):
                continue
        while True:
            found = {pid: data for pid, data in candidates.items()
                     if data["parent"] in processes}
            if not found:
                break
            processes.update(found)
            for pid in found:
                del candidates[pid]
        for pid, data in processes.items():
            identity = (pid, data["start"])
            previous = scope["cpu_seen"].get(identity)
            if previous is not None:
                scope["cpu_ticks"] += max(0, data["cpu"] - previous)
            scope["cpu_seen"][identity] = data["cpu"]
        self._append(scope["samples"], {
            "process_count": len(processes),
            "process_rss_bytes": sum(row["rss"] for row in processes.values())
            * os.sysconf("SC_PAGE_SIZE"),
            "observed_cpu_delta_seconds": scope["cpu_ticks"] / os.sysconf("SC_CLK_TCK"),
        })

    def _probe_failure(self, category, *, return_code=None, error=None):
        self.gaps.add("task_probe_failed")
        if len(self.task_probe_failures) >= MAX_PROBE_FAILURES:
            self.task_probe_failure_overflow_count += 1
            return
        record = {
            "attempt": self.task_probe_attempts,
            "elapsed_seconds": time.monotonic() - self.started,
            "category": category,
        }
        if return_code is not None:
            record["return_code"] = return_code
        if error is not None:
            # Keep the bounded exception class, never its message or command output.
            record["exception_type"] = type(error).__name__[:64]
        self.task_probe_failures.append(record)

    async def sample(self):
        if self.started is None:
            return False
        for scope in self.scopes.values():
            if scope["active"]:
                self._sample_host(scope)
        self.task_probe_attempts += 1
        stage = "exec"
        try:
            result = await asyncio.wait_for(
                self.environment.exec(command=TASK_PROBE, timeout_sec=2), timeout=3,
            )
            if result.return_code:
                self._probe_failure("nonzero_exit", return_code=result.return_code)
                return False
            stage = "parse"
            values = {}
            for line in (result.stdout or "").splitlines():
                key, value = line.split("\t", 1)
                if key in {"init", "cgroup_identity"}:
                    values[key] = value
                elif key in {"process_count", "missed_processes", "process_rss_bytes",
                             "cpu_usage_usec", "cpu_user_usec", "cpu_system_usec",
                             "cgroup_memory_current_bytes", "cgroup_memory_peak_bytes"}:
                    counter = int(value)
                    if counter < 0:
                        raise ValueError("Resource counters must be nonnegative")
                    values[key] = counter
            identity = values.pop("cgroup_identity", None)
            init = values.pop("init", "unknown")
            values["cgroup_identity"] = (
                hashlib.sha256((init + ":" + identity).encode()).hexdigest()
                if identity is not None and init != "unknown" else None
            )
            if values["cgroup_identity"] is None:
                self.gaps.add("task_cgroup_v2_unavailable")
            if values.get("missed_processes"):
                self.gaps.add("task_processes_changed_during_probe")
            if "process_rss_bytes" not in values:
                self.gaps.add("task_rss_unavailable")
            for key in ("cpu_usage_usec", "cgroup_memory_current_bytes",
                        "cgroup_memory_peak_bytes"):
                if key not in values:
                    self.gaps.add(f"task_{key}_unavailable")
                    values[key] = None
            return self._append(self.task_samples, values)
        except (OSError, ValueError, AttributeError, RuntimeError, TimeoutError) as error:
            category = "invalid_output" if stage == "parse" else (
                "timeout" if isinstance(error, TimeoutError) else "exec_error"
            )
            self._probe_failure(category, error=error)
            return False

    async def _loop(self):
        while not self.stop_event.is_set():
            await self.sample()
            try:
                await asyncio.wait_for(self.stop_event.wait(), INTERVAL_SECONDS)
            except TimeoutError:
                pass

    def start(self):
        if self.started is not None:
            return
        self.started = time.monotonic()
        self.worker = asyncio.create_task(self._loop())

    async def stop(self):
        if self.started is None:
            return
        self.stop_event.set()
        if self.worker is not None:
            await self.worker
        self.final_sample_recorded = await self.sample()
        if not self.final_sample_recorded:
            self.gaps.add("task_final_sample_unavailable")
        self.stopped = time.monotonic()

    def observation(self):
        task_delta = None
        if self.final_sample_recorded and len(self.task_samples) >= 2:
            first, last = self.task_samples[0], self.task_samples[-1]
            if first.get("cgroup_identity") and first["cgroup_identity"] == last["cgroup_identity"]:
                before, after = first.get("cpu_usage_usec"), last.get("cpu_usage_usec")
                if before is not None and after is not None and after >= before:
                    task_delta = (after - before) / 1_000_000
        return {
            "source": "anonbench1.adapter.resource_observer",
            "scope": "admitted_execution_including_settlement",
            "start_boundary": "explicit_execution_admission",
            "end_boundary": "adapter_run_exit_after_existing_settlement",
            "complete": False,
            "observation_state": "finished" if self.stopped is not None else "sampling",
            "duration_seconds": (
                (self.stopped or time.monotonic()) - self.started
                if self.started is not None else None
            ),
            "interval_seconds": INTERVAL_SECONDS,
            "sample_limit_per_scope": MAX_SAMPLES,
            "truncated": self.truncated,
            "gaps": sorted(self.gaps | {"native_agent_relationships_unobserved"}),
            "task_environment": {
                "source": "linux_proc_and_cgroup_v2",
                "scope": "task_environment_including_probe",
                "complete": False,
                "rss_semantics": "sum_of_resident_pages_shared_pages_counted_per_process",
                "memory_peak_semantics": "cgroup_lifetime_not_adapter_run",
                "cpu_delta_seconds": task_delta,
                "cpu_delta_scope": "first_successful_sample_to_successful_final_sample",
                "final_sample_recorded": self.final_sample_recorded,
                "samples": self.task_samples,
                "probe_attempts": self.task_probe_attempts,
                "probe_failure_limit": MAX_PROBE_FAILURES,
                "probe_failures": self.task_probe_failures,
                "probe_failure_overflow_count": self.task_probe_failure_overflow_count,
            },
            "native_processes": [
                {"scope": name, "source": "linux_proc_explicit_child_tree",
                 "complete": False, "gaps": sorted(scope["gaps"]),
                 "rss_semantics": "sum_of_resident_pages_shared_pages_counted_per_process",
                 "cpu_semantics": "observed_identity_matched_counter_deltas",
                 "samples": scope["samples"]}
                for name, scope in self.scopes.items()
            ],
        }


def _publish(agent, context):
    observer = getattr(agent, "_resource_observer", None)
    if observer is None or observer.started is None:
        return
    try:
        observation = observer.observation()
        context.metadata = {**(context.metadata or {}), KEY: observation}
        logs = Path(agent.logs_dir)
        logs.mkdir(parents=True, exist_ok=True)
        (logs / ARTIFACT).write_text(json.dumps(observation, indent=2) + "\n")
        trajectory_path = logs / "trajectory.json"
        if trajectory_path.is_file():
            trajectory = json.loads(trajectory_path.read_text())
            metrics = trajectory.get("final_metrics") if isinstance(trajectory, dict) else None
            if isinstance(metrics, dict):
                metrics["extra"] = {**(metrics.get("extra") or {}), KEY: observation}
                trajectory_path.write_text(json.dumps(trajectory, indent=2) + "\n")
        return True
    except Exception:  # noqa: BLE001 - publication must preserve native success or failure.
        observer.gaps.add("publication_failed")


def publish_resources(method):
    """Merge after native population; preserve its token and capture decisions."""
    @wraps(method)
    def wrapped(self, context, *args, **kwargs):
        try:
            return method(self, context, *args, **kwargs)
        finally:
            _publish(self, context)
    return wrapped


def observe_resources(method):
    """Bind a dormant observer; admission explicitly starts measurement."""
    @wraps(method)
    async def wrapped(self, instruction, environment, context, *args, **kwargs):
        observer = ResourceObserver(environment)
        self._resource_observer = observer
        token = _active.set(observer)
        native_error = None
        try:
            return await method(self, instruction, environment, context, *args, **kwargs)
        except BaseException as error:
            native_error = error
            raise
        finally:
            async def finish():
                try:
                    await observer.stop()
                except Exception:  # noqa: BLE001 - diagnostics never replace the native outcome.
                    observer.gaps.add("observer_stop_failed")
                    observer.stopped = time.monotonic()
                if not context.is_empty():
                    published = _publish(self, context)
                    mounted = getattr(getattr(environment, "capabilities", None), "mounted", None)
                    if published and mounted is False:
                        try:
                            # Harbor downloads remote logs after run(), even when
                            # native offline execution already populated context.
                            async with asyncio.timeout(5):
                                for name in (ARTIFACT, "trajectory.json"):
                                    source = Path(self.logs_dir) / name
                                    if source.is_file():
                                        await environment.upload_file(
                                            source_path=source,
                                            target_path=str(self.environment_logs_dir / name),
                                        )
                        except Exception:  # noqa: BLE001 - uploads never replace the native outcome.
                            observer.gaps.add("final_resource_upload_failed")
                            _publish(self, context)

            cleanup = asyncio.create_task(finish())
            cancellation = None
            try:
                while not cleanup.done():
                    try:
                        await asyncio.shield(cleanup)
                    except asyncio.CancelledError as error:
                        cancellation = error
                cleanup.result()
                if cancellation is not None and native_error is None:
                    raise cancellation
            finally:
                _active.reset(token)
    return wrapped
