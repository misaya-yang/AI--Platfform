"""
AgentMiddleware protocol + chain runner.

Middleware is the project's answer to harness-engine's "middleware layer"
(reference_harness_engine.md) and mirrors the callback pattern Imam uses via
LangGraph's `BaseMessageModifier`. The agent loop stays a thin scaffold;
extension concerns (memory load, skill inject, permission gating, sensors)
register as middleware.

Hooks supported this revision:

- `before_call(ctx, messages)` — run before each model call. May mutate
  `messages` in place, may yield zero or more SSE events.
- `on_tool_call(ctx, tool_name, arguments)` — run before each tool
  invocation. Returns a `ToolVerdict` (allow / deny / confirm). First
  non-allow verdict wins; the loop skips the call accordingly.

All hooks are optional: middlewares implement only the ones they need.
The chain calls `getattr(mw, hook, None)` and skips missing hooks.

Future hooks (not yet wired): `on_stream_event`, `on_tool_result`,
`on_error`. Keep the protocol small; add hooks only when a concrete
middleware demands one.
"""

from __future__ import annotations

import logging
from collections.abc import AsyncGenerator
from dataclasses import dataclass, replace
from enum import Enum
from typing import TYPE_CHECKING, Any, Protocol, runtime_checkable

if TYPE_CHECKING:  # avoid runtime import cycle; AgentLoopContext imports this
    from .agent_loop import AgentLoopContext, AgentLoopEvent

logger = logging.getLogger(__name__)


class VerdictKind(str, Enum):
    """Outcome of a permission check."""

    ALLOW = "allow"
    DENY = "deny"
    CONFIRM = "confirm"  # awaiting user approval — treat as deny-for-now


@dataclass
class ToolVerdict:
    """Decision returned by `on_tool_call` middlewares."""

    kind: VerdictKind
    reason: str = ""
    # Optional audit tag — middleware name that produced the verdict.
    source: str = ""

    @classmethod
    def allow(cls, source: str = "") -> "ToolVerdict":
        return cls(kind=VerdictKind.ALLOW, source=source)

    @classmethod
    def deny(cls, reason: str, source: str = "") -> "ToolVerdict":
        return cls(kind=VerdictKind.DENY, reason=reason, source=source)

    @classmethod
    def confirm(cls, reason: str, source: str = "") -> "ToolVerdict":
        return cls(kind=VerdictKind.CONFIRM, reason=reason, source=source)

    @property
    def is_allow(self) -> bool:
        return self.kind is VerdictKind.ALLOW

    def with_source(self, source: str) -> "ToolVerdict":
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
        ctx: "AgentLoopContext",
        messages: list[dict[str, Any]],
    ) -> AsyncGenerator["AgentLoopEvent", None]:
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
            except Exception:
                logger.exception(
                    "middleware %r before_call raised; skipping",
                    getattr(mw, "name", type(mw).__name__),
                )

    async def run_on_tool_call(
        self,
        ctx: "AgentLoopContext",
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
            except Exception:
                logger.exception(
                    "middleware %r on_tool_call raised; treating as allow",
                    getattr(mw, "name", type(mw).__name__),
                )
                continue
            if not isinstance(verdict, ToolVerdict):
                continue
            if not verdict.is_allow:
                return verdict
        return ToolVerdict.allow()
