"""
Tests for PermissionMiddleware + chain.run_on_tool_call.

Covers: default allow-all, deny/confirm verdicts, policy_from_sets helper,
async policies, middleware ordering (first non-allow wins), forgiveness for
buggy/misbehaving policies.
"""

from __future__ import annotations

from typing import Any

import pytest

from assistant_service.core.agent.middleware import (
    MiddlewareChain,
    ToolVerdict,
    VerdictKind,
)
from assistant_service.core.agent.middlewares.permission import (
    PermissionMiddleware,
    allow_all,
    policy_from_sets,
)


# ---------------------------------------------------------------------------
# Default policy
# ---------------------------------------------------------------------------


def test_allow_all_policy() -> None:
    verdict = allow_all("any_tool", {}, ctx=None)  # type: ignore[arg-type]
    assert verdict.is_allow
    assert verdict.kind is VerdictKind.ALLOW


@pytest.mark.asyncio
async def test_middleware_default_is_pass_through() -> None:
    chain = MiddlewareChain([PermissionMiddleware()])
    verdict = await chain.run_on_tool_call(None, "fs_write", {"path": "x"})  # type: ignore[arg-type]
    assert verdict.is_allow


# ---------------------------------------------------------------------------
# policy_from_sets
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_policy_from_sets_denies_listed_tools() -> None:
    policy = policy_from_sets(deny={"bash", "fs_write"})
    chain = MiddlewareChain([PermissionMiddleware(policy)])

    assert (await chain.run_on_tool_call(None, "bash", {})).kind is VerdictKind.DENY  # type: ignore[arg-type]
    assert (await chain.run_on_tool_call(None, "fs_write", {})).kind is VerdictKind.DENY  # type: ignore[arg-type]
    assert (await chain.run_on_tool_call(None, "fs_read", {})).is_allow  # type: ignore[arg-type]


@pytest.mark.asyncio
async def test_policy_from_sets_confirms_listed_tools() -> None:
    policy = policy_from_sets(confirm={"web_fetch"})
    chain = MiddlewareChain([PermissionMiddleware(policy)])

    verdict = await chain.run_on_tool_call(None, "web_fetch", {"url": "https://x"})  # type: ignore[arg-type]
    assert verdict.kind is VerdictKind.CONFIRM
    assert "approval" in verdict.reason.lower()


# ---------------------------------------------------------------------------
# Async policies
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_async_policy() -> None:
    async def async_policy(tool_name: str, arguments: dict[str, Any], ctx: Any) -> ToolVerdict:
        if tool_name == "fs_write":
            return ToolVerdict.deny("async says no", source="policy")
        return ToolVerdict.allow()

    chain = MiddlewareChain([PermissionMiddleware(async_policy)])
    verdict = await chain.run_on_tool_call(None, "fs_write", {})  # type: ignore[arg-type]
    assert verdict.kind is VerdictKind.DENY
    assert "async" in verdict.reason


# ---------------------------------------------------------------------------
# Chain ordering: first non-allow wins
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_first_non_allow_wins() -> None:
    strict_policy = policy_from_sets(deny={"fs_write"})
    permissive_policy = policy_from_sets()  # allow all

    chain = MiddlewareChain(
        [
            PermissionMiddleware(strict_policy),
            PermissionMiddleware(permissive_policy),
        ]
    )
    verdict = await chain.run_on_tool_call(None, "fs_write", {})  # type: ignore[arg-type]
    assert verdict.kind is VerdictKind.DENY


# ---------------------------------------------------------------------------
# Verdict shape
# ---------------------------------------------------------------------------


def test_tool_verdict_factories() -> None:
    assert ToolVerdict.allow(source="s").kind is VerdictKind.ALLOW
    assert ToolVerdict.deny("why", source="s").kind is VerdictKind.DENY
    assert ToolVerdict.confirm("why", source="s").kind is VerdictKind.CONFIRM
    assert not ToolVerdict.deny("why").is_allow


@pytest.mark.asyncio
async def test_middleware_tags_source_when_policy_forgets() -> None:
    def bare_policy(_n: str, _a: dict[str, Any], _c: Any) -> ToolVerdict:
        return ToolVerdict.deny("no")  # no source

    chain = MiddlewareChain([PermissionMiddleware(bare_policy)])
    verdict = await chain.run_on_tool_call(None, "x", {})  # type: ignore[arg-type]
    assert verdict.source == "permission"


# ---------------------------------------------------------------------------
# Forgiving contract
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_buggy_policy_falls_back_to_allow() -> None:
    def bad_policy(_n: str, _a: dict[str, Any], _c: Any) -> Any:
        return "not a verdict"

    chain = MiddlewareChain([PermissionMiddleware(bad_policy)])
    verdict = await chain.run_on_tool_call(None, "x", {})  # type: ignore[arg-type]
    assert verdict.is_allow


@pytest.mark.asyncio
async def test_missing_hook_is_skipped() -> None:
    """A middleware with only before_call must not break chain.run_on_tool_call."""

    class HalfMiddleware:
        name = "half"

        async def before_call(self, ctx: Any, messages: list[Any]):
            return
            yield  # unreachable

    chain = MiddlewareChain([HalfMiddleware()])
    verdict = await chain.run_on_tool_call(None, "x", {})  # type: ignore[arg-type]
    assert verdict.is_allow
