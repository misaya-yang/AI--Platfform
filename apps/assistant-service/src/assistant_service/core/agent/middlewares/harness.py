"""Small reliability middlewares for the agent runtime harness.

These middlewares are intentionally not registered by default. Deployments or
tests can add them to ``AgentLoop.middleware_chain`` once thresholds are chosen.
"""

from __future__ import annotations

import json
import time
from dataclasses import replace
from typing import Any

from ..middleware import ToolVerdict


class CallLimitMiddleware:
    """Deny tool calls after a per-run maximum."""

    name = "call-limit"

    def __init__(self, max_calls: int) -> None:
        self.max_calls = max(1, int(max_calls))

    async def on_tool_call(
        self, ctx: Any, tool_name: str, arguments: dict[str, Any]
    ) -> ToolVerdict:
        del tool_name, arguments
        count = int(getattr(ctx, "_middleware_tool_call_count", 0) or 0) + 1
        ctx._middleware_tool_call_count = count
        if count > self.max_calls:
            return ToolVerdict.deny(
                f"tool call limit exceeded ({self.max_calls})",
                source=self.name,
            )
        return ToolVerdict.allow(source=self.name)


class LoopDetectionMiddleware:
    """Deny repeated identical tool calls within one run."""

    name = "loop-detection"

    def __init__(self, max_repeats: int = 1) -> None:
        self.max_repeats = max(1, int(max_repeats))

    async def on_tool_call(
        self, ctx: Any, tool_name: str, arguments: dict[str, Any]
    ) -> ToolVerdict:
        seen = getattr(ctx, "_middleware_tool_fingerprints", None)
        if not isinstance(seen, dict):
            seen = {}
            ctx._middleware_tool_fingerprints = seen
        fingerprint = self._fingerprint(tool_name, arguments)
        seen[fingerprint] = int(seen.get(fingerprint, 0) or 0) + 1
        if seen[fingerprint] > self.max_repeats:
            return ToolVerdict.deny(
                f"repeated tool call detected for {tool_name}",
                source=self.name,
            )
        return ToolVerdict.allow(source=self.name)

    @staticmethod
    def _fingerprint(tool_name: str, arguments: dict[str, Any]) -> str:
        try:
            args = json.dumps(arguments or {}, sort_keys=True, default=str)
        except (TypeError, ValueError):
            args = str(arguments)
        return f"{tool_name}:{args}"


class TimeBudgetMiddleware:
    """Deny new tool calls once a run exceeds a wall-clock budget."""

    name = "time-budget"

    def __init__(self, max_seconds: float) -> None:
        self.max_seconds = max(0.0, float(max_seconds))

    async def on_tool_call(
        self, ctx: Any, tool_name: str, arguments: dict[str, Any]
    ) -> ToolVerdict:
        del tool_name, arguments
        started_at = float(getattr(ctx, "trace_started_at", 0.0) or time.time())
        if time.time() - started_at > self.max_seconds:
            return ToolVerdict.deny(
                f"time budget exceeded ({self.max_seconds:.2f}s)",
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

    async def on_stream_event(self, ctx: Any, event: Any) -> None:
        events = getattr(ctx, "_middleware_trace_events", None)
        if not isinstance(events, list):
            events = []
            ctx._middleware_trace_events = events
        events.append(event.event_type)
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
        if False:
            yield None


__all__ = [
    "CallLimitMiddleware",
    "LoopDetectionMiddleware",
    "PreCompletionChecklistMiddleware",
    "TimeBudgetMiddleware",
    "TraceSensorMiddleware",
]
