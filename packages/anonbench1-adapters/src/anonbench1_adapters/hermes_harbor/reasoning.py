"""Per-Trial reasoning input and bounded native request/usage observations.

The operator supplies the reviewed model binding; no private model roster lives
in source. Middleware observes the native request, not the provider's internal
reasoning. Missing provider usage and auxiliary settlement remain unknown.
"""

import json
import math
import os
import threading
from contextlib import contextmanager
from pathlib import Path
from typing import Literal
from uuid import uuid4

from pydantic import BaseModel, ConfigDict, Field, model_validator

ProviderEffort = Literal["none", "low", "medium", "high", "xhigh", "max"]
ExecutionMode = Literal["qualification", "scored"]
LEVELS = frozenset({"low", "medium", "high", "xhigh", "max"})
AUXILIARY_EVENT_LIMIT = 256


class ReasoningSupport(BaseModel):
    """Exact model binding from the private operator's reviewed support matrix."""

    model_config = ConfigDict(extra="forbid")
    model_id: str = Field(min_length=1)
    provider_efforts: list[ProviderEffort]
    source: str = Field(min_length=1)
    # Default is Hermes's resolved enabled=False spelling. Other native
    # spellings need an explicit entry in the operator's admitted mapping.
    native_effort_mapping: dict[str, ProviderEffort] = Field(
        default_factory=lambda: {**{level: level for level in LEVELS}, "disabled": "none"}
    )

    @model_validator(mode="after")
    def admitted_vocabulary(self):
        levels = frozenset(self.provider_efforts)
        if len(levels) != len(self.provider_efforts) or levels not in (LEVELS, LEVELS | {"none"}):
            raise ValueError("Reasoning support must match the reviewed provider vocabulary")
        for native, provider in self.native_effort_mapping.items():
            if native != provider and not (native in {"off", "disabled"} and provider == "none"):
                raise ValueError("A native spelling mapping cannot floor or change provider effort")
        return self

    def admit(self, model: str, requested: ProviderEffort) -> None:
        if model != self.model_id or requested not in self.provider_efforts:
            raise ValueError("Requested reasoning is not admitted for this exact model binding")


def native_reasoning(requested: ProviderEffort) -> dict:
    return {"enabled": False} if requested == "none" else {"enabled": True, "effort": requested}


def selected_effort(config: dict | None) -> str | None:
    if not isinstance(config, dict):
        return None
    return "disabled" if config.get("enabled") is False else config.get("effort")


def effort_fields(request: dict) -> dict:
    # extra_body can override ordinary SDK fields. Conflicting spellings cannot
    # qualify merely because one of them matches the requested setting.
    values = {}
    for prefix, body in (("", request), ("extra_body.", request.get("extra_body", {}))):
        if not isinstance(body, dict):
            values[prefix + "invalid_body"] = type(body).__name__
            continue
        if "reasoning_effort" in body:
            values[prefix + "reasoning_effort"] = body["reasoning_effort"]
        reasoning = body.get("reasoning")
        if isinstance(reasoning, dict) and "effort" in reasoning:
            values[prefix + "reasoning.effort"] = reasoning["effort"]
    return values


def common_effort(values) -> str | None:
    values = list(values)
    if values and isinstance(values[0], str) and all(value == values[0] for value in values):
        return values[0]
    return None


def sdk_effort(response) -> dict:
    # Normalized native responses can omit provider fields. Absence here says
    # nothing about provider acceptance or the provider's internal reasoning.
    if not hasattr(response, "model_fields_set") or not callable(getattr(response, "model_dump", None)):
        return {}
    return effort_fields(response.model_dump(exclude_unset=True))


def sdk_usage(response) -> dict | None:
    """Keep explicit SDK fields only; native SimpleNamespace zeros are not raw usage."""
    usage = getattr(response, "usage", None)
    if not hasattr(usage, "model_fields_set") or not callable(getattr(usage, "model_dump", None)):
        return None
    raw = usage.model_dump(exclude_unset=True)
    result = {}
    for target, aliases in {
        "input_tokens": ("input_tokens", "prompt_tokens"),
        "output_tokens": ("output_tokens", "completion_tokens"),
    }.items():
        values = [raw[key] for key in aliases if key in raw]
        value = values[0] if len(values) == 1 else None
        result[target] = value if type(value) is int and value >= 0 else None
    details = raw.get("output_tokens_details", raw.get("completion_tokens_details"))
    value = details.get("reasoning_tokens") if isinstance(details, dict) else None
    result["reasoning_tokens"] = value if type(value) is int and value >= 0 else None
    details = raw.get("input_tokens_details", raw.get("prompt_tokens_details"))
    value = details.get("cached_tokens") if isinstance(details, dict) else None
    result["cache_read_tokens"] = value if type(value) is int and value >= 0 else None
    return result


class ReasoningAbort(BaseException):
    """Evidence-integrity failures must not enter native exception fallback."""


class ReasoningObserver:
    """One native process's observations; this does not own execution or billing."""

    def __init__(self, requested, support, native_selected, mode, path, trial_id, route):
        self.requested = requested
        self.support = support
        self.native_config = native_selected
        self.native_selected = selected_effort(native_selected)
        self.mode = mode
        if mode not in ("qualification", "scored"):
            raise ValueError("Unknown campaign execution mode")
        self.path = path
        self.trial_id = trial_id
        self.route = route
        self.lock = threading.RLock()
        self.requests = 0
        self.responses = 0
        self.failed = False
        self.sessions = set()
        self.observations = []
        self.usage = dict.fromkeys(
            ("input_tokens", "output_tokens", "reasoning_tokens", "cache_read_tokens"), 0
        )
        self.usage_observations = dict.fromkeys(self.usage, 0)
        self.auxiliary_events = []
        self.auxiliary_counts = {"pre": 0, "post": 0}
        self.auxiliary_dropped = 0
        self.auxiliary_failed = False

    def snapshot(self):
        normalized = self.support.native_effort_mapping.get(self.native_selected)
        configuration = None
        if self.observations:
            configuration = normalized == self.requested and all(
                observation["route_matches"] and observation["selected_provider_effort"] == self.requested
                for observation in self.observations
            )
        provider = None
        reported = [observation["provider_fields"] for observation in self.observations
                    if observation["response_received"]]
        if any(fields and common_effort(fields.values()) != self.requested for fields in reported):
            provider = False
        elif reported and len(reported) == self.requests and all(reported):
            provider = True
        return {
            "trial_id": self.trial_id,
            "requested_provider_effort": self.requested,
            "native_selected_effort": self.native_selected,
            "native_selected_config": self.native_config,
            "native_selected_effort_source": "hermes_constants.resolve_reasoning_config",
            "native_provider_effort": normalized,
            "native_effort_mapping": self.support.native_effort_mapping,
            "native_effort_mapping_source": self.support.source,
            "selected_provider_effort": common_effort(
                observation["selected_provider_effort"] for observation in self.observations
            ),
            "selected_provider_effort_source": "native.llm_execution.request",
            # This hook precedes the native transport; do not label its request
            # as an observed wire send or invent a provider acknowledgement.
            "effective_wire_effort": None,
            "effective_wire_effort_source": "unobserved_after_native.llm_execution",
            "provider_effective_effort": common_effort(
                common_effort(fields.values()) for fields in reported
            ),
            "provider_effective_effort_source": "native.llm_execution.return.explicit_sdk_fields",
            "configuration_qualified": configuration,
            "configuration_evidence_source": "admitted_native_mapping_and_native.llm_execution.request",
            "provider_acceptance": provider,
            "provider_acceptance_source": "native.llm_execution.return.explicit_sdk_fields",
            "requested_level_status": (
                "unqualified" if configuration is False or provider is False
                else "configuration_qualified" if configuration is True else "unknown"
            ),
            "observations": self.observations,
            "support_source": self.support.source,
            "session_ids": sorted(self.sessions),
            "requests": self.requests, "responses": self.responses, "failed": self.failed,
            "provider_usage": {
                key: value if self.usage_observations[key] else None
                for key, value in self.usage.items()
            } if self.responses else None,
            "provider_usage_observations": dict(self.usage_observations),
            "provider_usage_scope": "observed_native_llm_execution_responses_across_observed_sessions",
            "reasoning_tokens": self.usage["reasoning_tokens"]
            if self.usage_observations["reasoning_tokens"] else None,
            "usage_evidence_source": "native.llm_execution.return.explicit_sdk_fields",
            "auxiliary_calls": {
                "source": "native.pre_auxiliary_call/post_auxiliary_call",
                "scope": "observed_auxiliary_attempts_in_this_native_process",
                "status": "observed" if any(self.auxiliary_counts.values()) else "not_observed",
                "complete": False,
                "pre_calls": self.auxiliary_counts["pre"],
                "post_calls": self.auxiliary_counts["post"],
                "event_limit": AUXILIARY_EVENT_LIMIT,
                "dropped_events": self.auxiliary_dropped,
                "observation_failed": self.auxiliary_failed,
                "usage_source": "hermes.native_normalize_usage",
                "zero_or_missing_usage_is_unknown": True,
                "input_tokens_exclude_cache": True,
                "prompt_tokens_include_cache": True,
                "reasoning_tokens_included_in_output": True,
                "relationship_to_main_usage": "not_established_do_not_add",
                "events": list(self.auxiliary_events),
            },
            # The native hook does not cover protected auxiliary workers or
            # establish provider-side completion after host process teardown.
            "usage_complete": False, "auxiliary_usage_complete": False,
            "research_qualified": False,
        }

    def persist(self):
        temporary = self.path.with_name(f".{self.path.name}.{uuid4().hex}.tmp")
        descriptor = os.open(
            temporary, os.O_WRONLY | os.O_CREAT | os.O_EXCL | os.O_NOFOLLOW, 0o600,
        )
        try:
            with os.fdopen(descriptor, "w") as output:
                if os.geteuid() == 0:
                    # The root task writer publishes into the host-owned logs mount.
                    owner = self.path.parent.stat()
                    os.fchown(output.fileno(), owner.st_uid, owner.st_gid)
                json.dump(self.snapshot(), output)
                output.write("\n")
            os.replace(temporary, self.path)
        except BaseException:
            try:
                temporary.unlink()
            except OSError:
                pass
            raise

    def pre_auxiliary_call(self, **payload):
        self._record_auxiliary("pre", payload)

    def post_auxiliary_call(self, **payload):
        self._record_auxiliary("post", payload)

    def _record_auxiliary(self, phase, payload):
        # Native auxiliary hooks are observer-only. Their failures must not
        # interrupt provider work or use the main middleware's abort path.
        with self.lock:
            try:
                self.auxiliary_counts[phase] += 1
                if len(self.auxiliary_events) >= AUXILIARY_EVENT_LIMIT:
                    self.auxiliary_dropped += 1
                else:
                    event = {"phase": phase}
                    for key in ("api_request_id", "aux_task", "session_id", "task_id",
                                "turn_id", "model", "provider", "api_mode", "error_type"):
                        value = payload.get(key)
                        event[key] = value[:256] if isinstance(value, str) and value else None
                    event["parent_context"] = (
                        "present" if any(event[key] for key in ("session_id", "task_id", "turn_id"))
                        else "unattributed"
                    )
                    retry = payload.get("retry_count")
                    event["retry_count"] = retry if type(retry) is int and retry >= 0 else None
                    event["streaming"] = payload.get("streaming") is True
                    if phase == "pre":
                        request = payload.get("request")
                        body = request.get("body") if isinstance(request, dict) else None
                        event["request_effort_fields"] = effort_fields(body) if isinstance(body, dict) else {}
                        event["request_evidence_source"] = "native.pre_auxiliary_call.before_relay"
                    else:
                        duration = payload.get("api_duration")
                        event["duration_seconds"] = (
                            duration if type(duration) in (float, int)
                            and math.isfinite(duration) and duration >= 0 else None
                        )
                        usage = payload.get("usage")
                        # Native normalization loses field presence and defaults
                        # absent fields to zero; these cannot prove observed zeros.
                        event["native_normalized_usage"] = {
                            key: value if type(value := usage.get(key)) is int and value > 0 else None
                            for key in ("input_tokens", "prompt_tokens", "output_tokens",
                                        "cache_read_tokens", "cache_write_tokens", "reasoning_tokens")
                        } if isinstance(usage, dict) and not event["streaming"] else None
                    self.auxiliary_events.append(event)
                self.persist()
            except Exception:  # noqa: BLE001 -- diagnostics cannot abort native auxiliary work.
                self.auxiliary_failed = True

    @contextmanager
    def _retaining_evidence(self):
        try:
            with self.lock:
                yield
        except Exception as exc:
            # Only evidence failures bypass native ordinary-exception fallback.
            # Provider execution stays outside this scope and keeps its owner.
            with self.lock:
                self.failed = True
            raise ReasoningAbort("Reasoning evidence could not be retained") from exc

    def middleware(self, *, request, next_call, **context):
        with self._retaining_evidence():
            self.requests += 1
            session = context.get("session_id")
            if isinstance(session, str) and session:
                self.sessions.add(session)
            fields = effort_fields(request)
            observation = {
                "request_number": self.requests,
                "session_id": session,
                "request_fields": fields,
                "request_evidence_source": "native.llm_execution.request",
                "selected_provider_effort": common_effort(fields.values()),
                "route_matches": request.get("model") == self.support.model_id
                and all(context.get(key) == value for key, value in self.route.items()),
                "response_received": False, "provider_fields": {},
                "provider_evidence_source": "native.llm_execution.return.explicit_sdk_fields",
            }
            self.observations.append(observation)
            self.persist()
        # Effort mismatches classify this cell. They do not cancel dispatch,
        # discard the native outcome, rewrite rewards, or trigger a replay.
        result = next_call(request)
        with self._retaining_evidence():
            usage = sdk_usage(result)
            self.responses += 1
            observation["response_received"] = True
            observation["provider_fields"] = sdk_effort(result)
            observation["usage"] = usage
            for key, total in self.usage.items():
                value = usage.get(key) if usage is not None else None
                if value is not None:
                    self.usage[key] = total + value
                    self.usage_observations[key] += 1
            self.persist()
        return result


def register_reasoning(ctx):
    from hermes_cli.config import load_config
    from hermes_constants import resolve_reasoning_config

    config = load_config()
    settings = ctx.get_config("reasoning")
    support = ReasoningSupport.model_validate(settings["support"])
    requested = settings["requested_provider_effort"]
    support.admit(config["model"]["default"], requested)
    observer = ReasoningObserver(
        requested, support, resolve_reasoning_config(config),
        settings["execution_mode"], Path(settings["evidence_path"]), settings["trial_id"],
        {key: config["model"][key] for key in ("provider", "base_url", "api_mode")},
    )
    ctx.register_middleware("llm_execution", observer.middleware)
    ctx.register_hook("pre_auxiliary_call", observer.pre_auxiliary_call)
    ctx.register_hook("post_auxiliary_call", observer.post_auxiliary_call)
    return config["terminal"]["backend"] == "harbor_task"


def research_evidence_qualified(evidence: dict, requested: ProviderEffort) -> bool:
    """Classify research evidence without deciding execution or reward completion."""
    return (
        evidence.get("requested_provider_effort") == requested
        and evidence.get("configuration_qualified") is True
        and evidence.get("provider_acceptance") is not False
        and evidence.get("failed") is False
        and bool(evidence.get("requests"))
        and evidence.get("requests") == evidence.get("responses")
        and evidence.get("usage_complete") is True
        and evidence.get("auxiliary_usage_complete") is True
        and evidence.get("research_qualified") is True
    )
