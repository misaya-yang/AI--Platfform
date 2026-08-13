"""
AgentMiddleware protocol + chain runner.

Middleware is the project's answer to harness-engine's "middleware layer"
(reference_harness_engine.md) and mirrors the callback pattern agent runtimes use via
LangGraph's `BaseMessageModifier`. The agent loop stays a thin scaffold;
extension concerns (memory load, skill inject, permission gating, sensors)
register as middleware.

Hooks supported this revision:

- `before_call(ctx, messages)` — run before each model call. May mutate
  `messages` in place, may yield zero or more SSE events.
- `on_tool_call(ctx, tool_name, arguments)` — run before each tool
  invocation. Returns a `ToolVerdict` (allow / deny / confirm). First
  non-allow verdict wins; the loop skips the call accordingly.
- `on_tool_result(ctx, tool_name, arguments, result)` — run after a tool
  returns. Middlewares may return a (possibly mutated) ToolCallResult;
  returning None leaves the result unchanged. The chain threads the
  result through every middleware in order so transforms compose
  (e.g. redact → truncate).
- `on_stream_event(ctx, event)` — run before an AgentLoopEvent is emitted
  to the caller. Middlewares may return a replacement event; returning
  None leaves the event unchanged.
- `on_error(ctx, error, phase)` — run when the streaming-first loop sees
  an internal error. Middlewares may yield diagnostic AgentLoopEvents.

All hooks are optional: middlewares implement only the ones they need.
The chain calls `getattr(mw, hook, None)` and skips missing hooks.
"""

from __future__ import annotations

from collections.abc import AsyncGenerator
from dataclasses import dataclass, replace
from enum import Enum
from typing import TYPE_CHECKING, Any, Protocol, runtime_checkable

from ai_gateway_core.logging import get_logger, log_internal_exception

if TYPE_CHECKING:  # avoid runtime import cycle; AgentLoopContext imports this
    from .agent_loop import AgentLoopContext, AgentLoopEvent

logger = get_logger(__name__)


class VerdictKind(str, Enum):
    """Outcome of a permission check."""

    ALLOW = "allow"
    DENY = "deny"
    CONFIRM = "confirm"  # awaiting user approval — emits approval_required SSE


@dataclass
class ToolVerdict:
    """Decision returned by `on_tool_call` middlewares."""

    kind: VerdictKind
    reason: str = ""
    # Optional audit tag — middleware name that produced the verdict.
    source: str = ""

    @classmethod
    def allow(cls, source: str = "") -> ToolVerdict:
        return cls(kind=VerdictKind.ALLOW, source=source)

    @classmethod
    def deny(cls, reason: str, source: str = "") -> ToolVerdict:
        return cls(kind=VerdictKind.DENY, reason=reason, source=source)

    @classmethod
    def confirm(cls, reason: str, source: str = "") -> ToolVerdict:
        return cls(kind=VerdictKind.CONFIRM, reason=reason, source=source)

    @property
    def is_allow(self) -> bool:
        return self.kind is VerdictKind.ALLOW

    def with_source(self, source: str) -> ToolVerdict:
        """Return a copy with `source` set. Used by middleware to audit-tag
        verdicts that were produced by a policy that didn't set one."""
        return replace(self, source=source)


@runtime_checkable
class AgentMiddleware(Protocol):
    """Extension point for the agent loop.

    Implementations are instantiated once per AgentLoop and called for each
    request. They must be non-blocking and side-effect-safe — concurrent
    requests on the same loop will share the instance.
    """

    name: str


class MiddlewareChain:
    """Runs a list of middlewares in registration order.

    Hooks:
      - `run_before_call(ctx, messages)` — async generator of events.
      - `run_on_tool_call(ctx, tool_name, arguments)` — returns first
        non-allow `ToolVerdict`, else `ToolVerdict.allow()`.
      - `run_on_tool_result(ctx, tool_name, arguments, result)` — threads
        a tool result through transforms.
      - `run_on_stream_event(ctx, event)` — lets middlewares observe or
        replace an outbound AgentLoopEvent.
      - `run_on_error(ctx, error, phase)` — forwards diagnostic events from
        error sensors without letting a buggy sensor crash the turn.
    """

    def __init__(self, middlewares: list[AgentMiddleware] | None = None) -> None:
        self._middlewares: list[AgentMiddleware] = list(middlewares or [])

    def add(self, middleware: AgentMiddleware) -> None:
        self._middlewares.append(middleware)

    @property
    def middlewares(self) -> list[AgentMiddleware]:
        return list(self._middlewares)

    async def run_before_call(
        self,
        ctx: AgentLoopContext,
        messages: list[dict[str, Any]],
    ) -> AsyncGenerator[AgentLoopEvent, None]:
        """Run every middleware's `before_call` once per turn, in order, and
        forward any events each yields. A buggy middleware shouldn't take down
        the turn — exceptions are logged and skipped."""
        for mw in self._middlewares:
            hook = getattr(mw, "before_call", None)
            if hook is None:
                continue
            try:
                async for event in hook(ctx, messages):
                    yield event
            except Exception as exc:
                log_internal_exception(
                    logger,
                    "assistant.middleware.before_call.failed",
                    exc,
                )

    async def run_on_tool_result(
        self,
        ctx: AgentLoopContext,
        tool_name: str,
        arguments: dict[str, Any],
        result: Any,
    ) -> Any:
        """Thread a tool result through every middleware's `on_tool_result`
        hook in registration order. A middleware may return a new result
        (e.g. truncated) or `None` to pass through unchanged. Exceptions are
        logged and skipped — a buggy transform must never swallow the
        result entirely."""
        current = result
        for mw in self._middlewares:
            hook = getattr(mw, "on_tool_result", None)
            if hook is None:
                continue
            try:
                replacement = await hook(ctx, tool_name, arguments, current)
            except Exception as exc:
                log_internal_exception(
                    logger,
                    "assistant.middleware.on_tool_result.failed",
                    exc,
                )
                continue
            if replacement is not None:
                current = replacement
        return current

    async def run_on_stream_event(
        self,
        ctx: AgentLoopContext,
        event: AgentLoopEvent,
    ) -> AgentLoopEvent:
        """Thread an outbound stream event through every middleware.

        A middleware may return a replacement event. Returning None keeps
        the current event. Exceptions are logged and skipped so event
        emission remains best-effort and ordered.
        """
        current = event
        for mw in self._middlewares:
            hook = getattr(mw, "on_stream_event", None)
            if hook is None:
                continue
            try:
                replacement = await hook(ctx, current)
            except Exception as exc:
                log_internal_exception(
                    logger,
                    "assistant.middleware.on_stream_event.failed",
                    exc,
                )
                continue
            if replacement is not None:
                current = replacement
        return current

    async def run_on_error(
        self,
        ctx: AgentLoopContext,
        error: BaseException,
        phase: Any | None = None,
    ) -> AsyncGenerator[AgentLoopEvent, None]:
        """Run error sensors and forward any diagnostic events they yield."""
        for mw in self._middlewares:
            hook = getattr(mw, "on_error", None)
            if hook is None:
                continue
            try:
                async for event in hook(ctx, error, phase):
                    yield event
            except Exception as exc:
                log_internal_exception(
                    logger,
                    "assistant.middleware.on_error.failed",
                    exc,
                )

    async def run_on_tool_call(
        self,
        ctx: AgentLoopContext,
        tool_name: str,
        arguments: dict[str, Any],
    ) -> ToolVerdict:
        """Ask every middleware about a proposed tool call. Return the first
        non-allow verdict, or `ToolVerdict.allow()` if all pass. Forgiving
        contract: missing hooks / non-ToolVerdict returns / exceptions all
        count as allow — a buggy policy must never fail-closed across every
        tool call in a deployment."""
        for mw in self._middlewares:
            hook = getattr(mw, "on_tool_call", None)
            if hook is None:
                continue
            try:
                verdict = await hook(ctx, tool_name, arguments)
            except Exception as exc:
                log_internal_exception(
                    logger,
                    "assistant.middleware.on_tool_call.failed",
                    exc,
                )
                continue
            if not isinstance(verdict, ToolVerdict):
                continue
            if not verdict.is_allow:
                return verdict
        return ToolVerdict.allow()
