from __future__ import annotations

from typing import Any

import pytest
from assistant_service.core.tool_invoker import (
    CapabilityAllowlist,
    RegistryToolInvoker,
    ToolInvocationContext,
)
from assistant_service.core.tools.tool_registry import (
    ToolCallResult,
    ToolDefinition,
    ToolParameter,
    ToolRegistry,
)


def _tool(name: str) -> ToolDefinition:
    return ToolDefinition(
        name=name,
        description=f"test tool {name}",
        parameters=[
            ToolParameter(
                name="input",
                type="string",
                description="test input",
                required=False,
            )
        ],
    )


def _context(
    allowlist: CapabilityAllowlist | None,
    *,
    session_id: str = "session-1",
    scope_id: str | None = None,
    kb_dataset_ids: list[str] | None = None,
) -> ToolInvocationContext:
    return ToolInvocationContext(
        session_id=session_id,
        user_id="user-1",
        tenant_id="tenant-1",
        request_id="request-1",
        capability_allowlist=allowlist,
        scope_id=scope_id,
        kb_dataset_ids=kb_dataset_ids or [],
    )


def _registry_with_executors(*names: str) -> tuple[ToolRegistry, dict[str, int]]:
    registry = ToolRegistry()
    calls = dict.fromkeys(names, 0)

    for name in names:

        async def executor(request: Any, *, _name: str = name) -> ToolCallResult:
            calls[_name] += 1
            return ToolCallResult(
                call_id=request.call_id,
                tool_name=request.tool_name,
                success=True,
                result={"tool": _name, "arguments": request.arguments},
            )

        registry.register(_tool(name), executor)

    return registry, calls


@pytest.mark.asyncio
async def test_absent_allowlist_preserves_legacy_visibility_and_invocation() -> None:
    registry, calls = _registry_with_executors("alpha", "beta")
    invoker = RegistryToolInvoker(tool_registry=registry)
    context = _context(None)

    definitions = await invoker.get_tool_definitions_filtered(context)
    result = await invoker.invoke("beta", {}, context)

    assert [definition.name for definition in definitions] == ["alpha", "beta"]
    assert invoker.get_available_tools(context) == ["alpha", "beta"]
    assert result.success is True
    assert calls == {"alpha": 0, "beta": 1}


@pytest.mark.asyncio
async def test_explicit_empty_allowlist_exposes_and_invokes_no_tools() -> None:
    registry, calls = _registry_with_executors("alpha")
    invoker = RegistryToolInvoker(tool_registry=registry)
    context = _context(CapabilityAllowlist())

    definitions = await invoker.get_tool_definitions_filtered(context)
    result = await invoker.invoke("alpha", {}, context)

    assert definitions == []
    assert invoker.get_available_tools(context) == []
    assert result.success is False
    assert result.error == "Tool 'alpha' is not available to this Agent."
    assert calls["alpha"] == 0


@pytest.mark.asyncio
async def test_allowlist_is_an_upper_bound_for_listing_and_invocation() -> None:
    registry, calls = _registry_with_executors("alpha", "beta", "gamma")
    invoker = RegistryToolInvoker(tool_registry=registry)
    context = _context(CapabilityAllowlist(frozenset({"alpha", "gamma"})))

    definitions = await invoker.get_tool_definitions_filtered(
        context,
        tool_names=["alpha", "beta"],
    )
    denied = await invoker.invoke("beta", {}, context)
    allowed = await invoker.invoke("gamma", {}, context)

    assert [definition.name for definition in definitions] == ["alpha"]
    assert denied.success is False
    assert allowed.success is True
    assert calls == {"alpha": 0, "beta": 0, "gamma": 1}


@pytest.mark.asyncio
async def test_allowlist_denial_runs_before_cached_result_lookup() -> None:
    registry, calls = _registry_with_executors("search_knowledge_base")
    invoker = RegistryToolInvoker(tool_registry=registry)
    arguments = {"input": "same-query"}

    primed = await invoker.invoke(
        "search_knowledge_base",
        arguments,
        _context(None, session_id="shared-session"),
    )
    denied = await invoker.invoke(
        "search_knowledge_base",
        arguments,
        _context(CapabilityAllowlist(), session_id="shared-session"),
    )

    assert primed.success is True
    assert denied.success is False
    assert denied.metadata.get("cache_hit") is not True
    assert calls["search_knowledge_base"] == 1


@pytest.mark.asyncio
async def test_agent_scope_prevents_cross_runtime_cache_reuse() -> None:
    registry, calls = _registry_with_executors("search_knowledge_base")
    invoker = RegistryToolInvoker(tool_registry=registry)
    allowlist = CapabilityAllowlist(frozenset({"search_knowledge_base"}))
    arguments = {"input": "same-query"}

    first = await invoker.invoke(
        "search_knowledge_base",
        arguments,
        _context(
            allowlist,
            session_id="same-session",
            scope_id="agent-a:version-a",
            kb_dataset_ids=["dataset-a"],
        ),
    )
    second = await invoker.invoke(
        "search_knowledge_base",
        arguments,
        _context(
            allowlist,
            session_id="same-session",
            scope_id="agent-b:version-b",
            kb_dataset_ids=["dataset-a"],
        ),
    )

    assert first.success is True
    assert second.success is True
    assert second.metadata.get("cache_hit") is not True
    assert calls["search_knowledge_base"] == 2


@pytest.mark.asyncio
async def test_agent_knowledge_datasets_are_enforced_before_cache_and_execution() -> None:
    registry, calls = _registry_with_executors("search_knowledge_base")
    invoker = RegistryToolInvoker(tool_registry=registry)
    allowlist = CapabilityAllowlist(frozenset({"search_knowledge_base"}))
    context = _context(
        allowlist,
        scope_id="agent-a:version-a",
        kb_dataset_ids=["dataset-a", "dataset-b"],
    )

    forged = await invoker.invoke(
        "search_knowledge_base",
        {"input": "same-query", "dataset_ids": ["dataset-forged"]},
        context,
    )
    injected = await invoker.invoke(
        "search_knowledge_base",
        {"input": "same-query"},
        context,
    )
    narrowed = await invoker.invoke(
        "search_knowledge_base",
        {"input": "other-query", "dataset_ids": ["dataset-b"]},
        context,
    )

    assert forged.success is False
    assert forged.metadata.get("cache_hit") is not True
    assert injected.success is True
    assert injected.result["arguments"]["dataset_ids"] == ["dataset-a", "dataset-b"]
    assert narrowed.success is True
    assert narrowed.result["arguments"]["dataset_ids"] == ["dataset-b"]
    assert calls["search_knowledge_base"] == 2


@pytest.mark.asyncio
async def test_agent_without_knowledge_datasets_cannot_search_knowledge_base() -> None:
    registry, calls = _registry_with_executors("search_knowledge_base")
    invoker = RegistryToolInvoker(tool_registry=registry)
    context = _context(
        CapabilityAllowlist(frozenset({"search_knowledge_base"})),
        scope_id="agent-a:version-a",
    )

    denied = await invoker.invoke(
        "search_knowledge_base",
        {"input": "query"},
        context,
    )

    assert denied.success is False
    assert calls["search_knowledge_base"] == 0


@pytest.mark.asyncio
async def test_policy_uncertainty_cannot_reuse_a_previously_allowed_cache_hit() -> None:
    class FlakyPolicy:
        calls = 0

        async def get_policy(self, _tenant_id: str) -> Any:
            self.calls += 1
            if self.calls > 2:
                raise RuntimeError("policy unavailable")

            class Allowed:
                blocked_tools: set[str] = set()
                allowed_tools: set[str] = set()
                allowed_categories: set[str] = set()

            return Allowed()

    registry, calls = _registry_with_executors("search_knowledge_base")
    invoker = RegistryToolInvoker(
        tool_registry=registry,
        tenant_tool_policy=FlakyPolicy(),
    )
    context = _context(None, session_id="shared-session")

    primed = await invoker.invoke("search_knowledge_base", {"input": "same"}, context)
    denied = await invoker.invoke("search_knowledge_base", {"input": "same"}, context)

    assert primed.success is True
    assert denied.success is False
    assert denied.metadata.get("cache_hit") is not True
    assert calls["search_knowledge_base"] == 1


@pytest.mark.asyncio
async def test_catalog_hides_warm_cached_tools_when_fresh_policy_is_unavailable() -> None:
    class CachedThenUnavailablePolicy:
        async def get_policy(self, _tenant_id: str) -> Any:
            class CachedAllowed:
                blocked_tools: set[str] = set()
                allowed_tools: set[str] = set()
                allowed_categories: set[str] = set()

            return CachedAllowed()

        async def get_policy_fresh(self, _tenant_id: str) -> Any:
            raise RuntimeError("policy database unavailable")

    registry, _calls = _registry_with_executors("alpha")
    invoker = RegistryToolInvoker(
        tool_registry=registry,
        tenant_tool_policy=CachedThenUnavailablePolicy(),
    )

    definitions = await invoker.get_tool_definitions_filtered(_context(None))

    assert definitions == []


@pytest.mark.asyncio
async def test_tenant_policy_uncertainty_fails_closed() -> None:
    class FailingPolicy:
        async def get_policy(self, _tenant_id: str) -> Any:
            raise RuntimeError("policy unavailable")

    registry, calls = _registry_with_executors("alpha")
    invoker = RegistryToolInvoker(
        tool_registry=registry,
        tenant_tool_policy=FailingPolicy(),
    )
    context = _context(None)

    definitions = await invoker.get_tool_definitions_filtered(context)
    result = await invoker.invoke("alpha", {}, context)

    assert definitions == []
    assert result.success is False
    assert result.error == "Tool 'alpha' is not available for this tenant."
    assert calls["alpha"] == 0


@pytest.mark.asyncio
async def test_catalog_snapshot_cannot_expand_when_live_policy_becomes_more_permissive() -> None:
    class ExpandingPolicy:
        calls = 0

        async def get_policy(self, _tenant_id: str) -> Any:
            self.calls += 1

            class Policy:
                blocked_tools: set[str] = set()
                allowed_tools = {"alpha"} if self.calls == 1 else {"alpha", "beta"}
                allowed_categories: set[str] = set()

            return Policy()

    registry, calls = _registry_with_executors("alpha", "beta")
    invoker = RegistryToolInvoker(
        tool_registry=registry,
        tenant_tool_policy=ExpandingPolicy(),
    )
    context = _context(None)

    definitions = await invoker.get_tool_definitions_filtered(context)
    forged = await invoker.invoke("beta", {}, context)

    assert [definition.name for definition in definitions] == ["alpha"]
    assert context.policy_snapshot is not None
    assert forged.success is False
    assert calls["beta"] == 0


@pytest.mark.asyncio
async def test_live_policy_recheck_can_revoke_but_never_expand_catalog_snapshot() -> None:
    class RevokingPolicy:
        calls = 0

        async def get_policy(self, _tenant_id: str) -> Any:
            self.calls += 1

            class Policy:
                allowed_tools: set[str] = set()
                blocked_tools = set() if self.calls <= 2 else {"alpha"}
                allowed_categories: set[str] = set()

            return Policy()

    registry, calls = _registry_with_executors("alpha")
    invoker = RegistryToolInvoker(
        tool_registry=registry,
        tenant_tool_policy=RevokingPolicy(),
    )
    context = _context(None)

    definitions = await invoker.get_tool_definitions_filtered(context)
    revoked = await invoker.invoke("alpha", {}, context)

    assert [definition.name for definition in definitions] == ["alpha"]
    assert revoked.success is False
    assert calls["alpha"] == 0
    assert revoked.metadata["tool_policy_revalidated"] is False


@pytest.mark.asyncio
async def test_secondary_catalog_merge_rechecks_live_revocation() -> None:
    class RevokingPolicy:
        calls = 0

        async def get_policy(self, _tenant_id: str) -> Any:
            self.calls += 1

            class Policy:
                allowed_tools: set[str] = set()
                blocked_tools = set() if self.calls <= 2 else {"alpha"}
                allowed_categories: set[str] = set()

            return Policy()

    registry, _ = _registry_with_executors("alpha")
    invoker = RegistryToolInvoker(
        tool_registry=registry,
        tenant_tool_policy=RevokingPolicy(),
    )
    context = _context(None)

    canonical = await invoker.get_tool_definitions_filtered(context)
    merged = await invoker.filter_tool_definitions_authorized(context, canonical)

    assert [definition.name for definition in canonical] == ["alpha"]
    assert merged == []


@pytest.mark.asyncio
async def test_policy_snapshot_is_bound_to_tenant_user_session_and_run_scope() -> None:
    registry, calls = _registry_with_executors("alpha")
    invoker = RegistryToolInvoker(tool_registry=registry)
    original = _context(None)
    await invoker.get_tool_definitions_filtered(original)
    assert original.policy_snapshot is not None

    mismatched = _context(None, session_id="different-session")
    mismatched.policy_snapshot = original.policy_snapshot
    result = await invoker.invoke("alpha", {}, mismatched)

    assert result.success is False
    assert calls["alpha"] == 0


def test_allowlist_is_immutable_and_serializes_without_changing_none_semantics() -> None:
    names = {"beta", "alpha"}
    source_bindings = {"alpha": {"type": "mcp", "config": {"connection_id": "original"}}}
    allowlist = CapabilityAllowlist(  # type: ignore[arg-type]
        names,
        bindings=source_bindings,
    )
    names.add("gamma")
    source_bindings["alpha"]["config"]["connection_id"] = "mutated-source"
    exposed = allowlist.bindings["alpha"]
    exposed["config"]["connection_id"] = "mutated-copy"

    assert allowlist.tool_names == frozenset({"alpha", "beta"})
    assert allowlist.binding("alpha")["config"]["connection_id"] == "original"
    assert _context(allowlist).to_dict()["capability_allowlist"] == ["alpha", "beta"]
    assert _context(None).to_dict()["capability_allowlist"] is None


def test_allowlist_rejects_a_single_string_as_a_collection() -> None:
    with pytest.raises(TypeError, match="collection of complete tool names"):
        CapabilityAllowlist("alpha")  # type: ignore[arg-type]
