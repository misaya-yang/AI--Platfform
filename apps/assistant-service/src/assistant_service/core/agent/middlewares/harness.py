"""Default reliability middlewares for the canonical agent runtime."""

from __future__ import annotations

import hashlib
import json
import math
import time
from dataclasses import replace
from typing import Any

from ..middleware import ToolVerdict


def _configured_limit_sources(ctx: Any) -> list[Any]:
    config = getattr(ctx, "config", None)
    if config is None:
        return []
    sources = [getattr(config, "reliability_limits", None)]
    profile = str(getattr(config, "execution_profile", "") or "").strip().casefold()
    profile_limits = getattr(config, "reliability_profile_limits", None)
    if profile and isinstance(profile_limits, dict):
        for candidate_profile, limits in profile_limits.items():
            if str(candidate_profile).strip().casefold() == profile:
                sources.append(limits)
                break
    return sources


def _effective_integer_limit(
    ctx: Any,
    *,
    field_name: str,
    fallback: int,
) -> int:
    candidates = [max(1, int(fallback))]
    for source in _configured_limit_sources(ctx):
        value = getattr(source, field_name, None)
        if isinstance(value, int) and not isinstance(value, bool) and value > 0:
            candidates.append(value)
    return min(candidates)


def _effective_float_limit(
    ctx: Any,
    *,
    field_name: str,
    fallback: float,
) -> float:
    candidates = [max(0.0, float(fallback))]
    for source in _configured_limit_sources(ctx):
        value = getattr(source, field_name, None)
        if (
            isinstance(value, (int, float))
            and not isinstance(value, bool)
            and math.isfinite(float(value))
            and value > 0
        ):
            candidates.append(float(value))
    return min(candidates)


class CallLimitMiddleware:
    """Deny tool calls after a per-run maximum."""

    name = "call-limit"

    def __init__(self, max_calls: int = 256) -> None:
        self.max_calls = max(1, int(max_calls))

    async def on_tool_call(
        self, ctx: Any, tool_name: str, arguments: dict[str, Any]
    ) -> ToolVerdict:
        del tool_name, arguments
        run_budget = getattr(ctx, "run_budget", None)
        run_budget_calls = getattr(run_budget, "tool_calls", None)
        run_budget_limit = getattr(getattr(run_budget, "limits", None), "max_tool_calls", None)
        if (
            isinstance(run_budget_calls, int)
            and not isinstance(run_budget_calls, bool)
            and run_budget_calls >= 0
            and isinstance(run_budget_limit, int)
            and not isinstance(run_budget_limit, bool)
            and run_budget_limit > 0
        ):
            # StreamingToolLoop reserves the normalized batch before this hook,
            # and RunBudget restores this counter across approval resumes.
            count = run_budget_calls
            limit = run_budget_limit
        else:
            limit = _effective_integer_limit(
                ctx,
                field_name="max_tool_calls",
                fallback=self.max_calls,
            )
            count = int(getattr(ctx, "_middleware_tool_call_count", 0) or 0) + 1
            ctx._middleware_tool_call_count = count
        if count > limit:
            return ToolVerdict.deny(
                f"tool call limit exceeded ({limit})",
                source=self.name,
            )
        return ToolVerdict.allow(source=self.name)


class LoopDetectionMiddleware:
    """Deny repeated identical tool calls within one run."""

    name = "loop-detection"

    def __init__(self, max_repeats: int = 3) -> None:
        self.max_repeats = max(1, int(max_repeats))

    async def on_tool_call(
        self, ctx: Any, tool_name: str, arguments: dict[str, Any]
    ) -> ToolVerdict:
        seen = getattr(ctx, "_middleware_tool_fingerprints", None)
        if not isinstance(seen, dict):
            seen = {}
            ctx._middleware_tool_fingerprints = seen
        max_repeats = _effective_integer_limit(
            ctx,
            field_name="max_identical_tool_calls",
            fallback=self.max_repeats,
        )
        fingerprint = self._fingerprint(tool_name, arguments)
        seen[fingerprint] = int(seen.get(fingerprint, 0) or 0) + 1
        if seen[fingerprint] > max_repeats:
            return ToolVerdict.deny(
                f"repeated tool call detected for {tool_name}",
                source=self.name,
            )
        return ToolVerdict.allow(source=self.name)

    @staticmethod
    def _fingerprint(tool_name: str, arguments: dict[str, Any]) -> str:
        try:
            args = json.dumps(
                arguments or {},
                sort_keys=True,
                separators=(",", ":"),
                ensure_ascii=False,
                default=str,
            )
        except (TypeError, ValueError):
            args = str(arguments)
        fingerprint_source = f"{tool_name}\0{args}".encode("utf-8", errors="replace")
        return hashlib.sha256(fingerprint_source).hexdigest()


class TimeBudgetMiddleware:
    """Deny new tool calls once a run exceeds a wall-clock budget."""

    name = "time-budget"

    def __init__(self, max_seconds: float = 1800.0) -> None:
        self.max_seconds = max(0.0, float(max_seconds))

    async def on_tool_call(
        self, ctx: Any, tool_name: str, arguments: dict[str, Any]
    ) -> ToolVerdict:
        del tool_name, arguments
        run_budget = getattr(ctx, "run_budget", None)
        max_seconds = getattr(
            getattr(run_budget, "limits", None),
            "max_wall_time_seconds",
            None,
        )
        elapsed = getattr(run_budget, "elapsed_seconds", None)
        if not (
            isinstance(max_seconds, (int, float))
            and not isinstance(max_seconds, bool)
            and math.isfinite(float(max_seconds))
            and max_seconds > 0
            and isinstance(elapsed, (int, float))
            and not isinstance(elapsed, bool)
            and math.isfinite(float(elapsed))
            and elapsed >= 0
        ):
            max_seconds = _effective_float_limit(
                ctx,
                field_name="max_wall_time_seconds",
                fallback=self.max_seconds,
            )
        if not (
            isinstance(elapsed, (int, float))
            and not isinstance(elapsed, bool)
            and math.isfinite(float(elapsed))
            and elapsed >= 0
        ):
            trace_started_at = getattr(ctx, "trace_started_at", None)
            started_at = (
                float(trace_started_at) if trace_started_at is not None else time.time()
            )
            elapsed = time.time() - started_at
        if elapsed > max_seconds:
            return ToolVerdict.deny(
                f"time budget exceeded ({max_seconds:.2f}s)",
                source=self.name,
            )
        return ToolVerdict.allow(source=self.name)


class PreCompletionChecklistMiddleware:
    """Convert empty successful completions into recoverable run errors."""

    name = "pre-completion-checklist"

    async def on_stream_event(self, ctx: Any, event: Any) -> Any:
        if event.event_type != "run_finished":
            return None
        if str(getattr(ctx, "generated_content", "") or "").strip():
            return None
        data = dict(getattr(event, "data", {}) or {})
        data.update(
            {
                "error": "empty_assistant_response",
                "recoverable": True,
            }
        )
        return replace(event, event_type="run_error", data=data)


class TraceSensorMiddleware:
    """Record stream/error observations on the AgentLoop context."""

    name = "trace-sensor"

    def __init__(self, max_events: int = 256, max_errors: int = 64) -> None:
        self.max_events = max(1, int(max_events))
        self.max_errors = max(1, int(max_errors))

    async def on_stream_event(self, ctx: Any, event: Any) -> None:
        events = getattr(ctx, "_middleware_trace_events", None)
        if not isinstance(events, list):
            events = []
            ctx._middleware_trace_events = events
        events.append(event.event_type)
        max_events = _effective_integer_limit(
            ctx,
            field_name="max_trace_events",
            fallback=self.max_events,
        )
        if len(events) > max_events:
            del events[:-max_events]
        return None

    async def on_error(self, ctx: Any, error: BaseException, phase: Any):
        errors = getattr(ctx, "_middleware_trace_errors", None)
        if not isinstance(errors, list):
            errors = []
            ctx._middleware_trace_errors = errors
        errors.append(
            {
                "phase": phase.value if hasattr(phase, "value") else str(phase),
                "error_type": type(error).__name__,
            }
        )
        max_errors = _effective_integer_limit(
            ctx,
            field_name="max_trace_errors",
            fallback=self.max_errors,
        )
        if len(errors) > max_errors:
            del errors[:-max_errors]
        if False:
            yield None


__all__ = [
    "CallLimitMiddleware",
    "LoopDetectionMiddleware",
    "PreCompletionChecklistMiddleware",
    "TimeBudgetMiddleware",
    "TraceSensorMiddleware",
]
