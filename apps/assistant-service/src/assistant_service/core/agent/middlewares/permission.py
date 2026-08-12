"""
Permission middleware — gate tool calls through a configurable policy.

Takes a callable `policy(tool_name, arguments, ctx) -> ToolVerdict` and runs it
on every tool call via the `on_tool_call` hook. Registering this middleware
with a stricter policy lets a deployment:

- Deny high-risk tools (`bash`, `fs_write`, destructive APIs) entirely.
- Require user confirmation (confirm verdict → frontend approval flow).
- Allow-list per tenant / execution_profile.

The middleware requires an explicit policy. Policy bugs fail closed at this
security boundary; ordinary non-security middleware hooks remain best-effort.
"""

from __future__ import annotations

import logging
from collections.abc import Awaitable, Callable
from typing import TYPE_CHECKING, Any

from ..middleware import ToolVerdict, VerdictKind

logger = logging.getLogger(__name__)

if TYPE_CHECKING:
    from ..agent_loop import AgentLoopContext


# A policy is any sync or async callable that returns a ToolVerdict.
Policy = Callable[
    [str, dict[str, Any], "AgentLoopContext"],
    ToolVerdict | Awaitable[ToolVerdict],
]


def allow_all(tool_name: str, arguments: dict[str, Any], ctx: AgentLoopContext) -> ToolVerdict:
    """Explicit pass-through policy for callers that deliberately opt in."""
    del tool_name, arguments, ctx
    return ToolVerdict.allow(source="permission")


class PermissionMiddleware:
    """Applies a policy function to every proposed tool call."""

    name = "permission"

    def __init__(self, policy: Policy) -> None:
        if not callable(policy):
            raise TypeError("PermissionMiddleware requires an explicit callable policy")
        self._policy = policy

    async def on_tool_call(
        self,
        ctx: AgentLoopContext,
        tool_name: str,
        arguments: dict[str, Any],
    ) -> ToolVerdict:
        import inspect

        try:
            result = self._policy(tool_name, arguments, ctx)
            if inspect.isawaitable(result):
                result = await result
        except Exception:  # noqa: BLE001 - policy failures must deny, not escape to chain
            logger.exception("permission policy failed for tool %s; denying", tool_name)
            return ToolVerdict.deny(
                reason="permission policy failed closed",
                source="permission",
            )
        if not isinstance(result, ToolVerdict):
            logger.warning(
                "permission policy returned non-ToolVerdict %r for tool %s; denying",
                type(result).__name__,
                tool_name,
            )
            return ToolVerdict.deny(
                reason="permission policy returned an invalid decision",
                source="permission",
            )
        return result if result.source else result.with_source("permission")


# Convenience: build a policy that denies/confirms specific tool names.
def policy_from_sets(
    *,
    deny: set[str] | None = None,
    confirm: set[str] | None = None,
) -> Policy:
    """Build a simple policy from tool-name sets. Everything else allows.
    Useful as a starting point before wiring the full policy_lattice."""
    deny_set = set(deny or ())
    confirm_set = set(confirm or ())

    def _policy(tool_name: str, _arguments: dict[str, Any], _ctx: Any) -> ToolVerdict:
        if tool_name in deny_set:
            return ToolVerdict.deny(
                reason=f"tool {tool_name!r} is denied by policy",
                source="permission",
            )
        if tool_name in confirm_set:
            return ToolVerdict.confirm(
                reason=f"tool {tool_name!r} requires user approval",
                source="permission",
            )
        return ToolVerdict.allow(source="permission")

    return _policy


# Re-export so consumers can `from .permission import ToolVerdict, VerdictKind`.
__all__ = [
    "PermissionMiddleware",
    "Policy",
    "ToolVerdict",
    "VerdictKind",
    "allow_all",
    "policy_from_sets",
]
