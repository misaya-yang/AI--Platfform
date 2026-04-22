"""
Tests for ConnectorRegistry — tenant-conditional tool surface.

Covers:
  - Predicate-gated visibility: tenant A sees the tool, tenant B doesn't.
  - 60s TTL cache: predicate runs once for repeated lookups of the same tenant.
  - Predicate errors are isolated — a crashing connector hides its tools
    instead of breaking the request.
  - Re-registration replaces the old entry cleanly.
  - Invalidate clears the cache.
"""

from __future__ import annotations

import pytest

from src.core.auth.user_resolver import UserContext
from assistant_service.core.tools.connector_registry import (
    ConnectorRegistry,
    get_connector_registry,
    reset_connector_registry_for_tests,
)
from assistant_service.core.tools.tool_registry import (
    ToolCallRequest,
    ToolCategory,
    ToolDefinition,
    ToolParameter,
    ToolRiskLevel,
)


def _fake_tool(name: str = "fake_tool") -> ToolDefinition:
    return ToolDefinition(
        name=name,
        description=f"fake tool {name} for testing",
        parameters=[
            ToolParameter(name="x", type="string", description="x", required=True),
        ],
        category=ToolCategory.INTEGRATION,
        risk_level=ToolRiskLevel.LOW,
    )


def _req(tenant_id: str) -> ToolCallRequest:
    return ToolCallRequest(
        call_id=f"c-{tenant_id}",
        tool_name="probe",
        arguments={},
        user=UserContext(user_id="u", tenant_id=tenant_id),
    )


@pytest.mark.asyncio
async def test_predicate_gated_visibility():
    """Connector visible to tenantA, hidden for tenantB."""
    reg = ConnectorRegistry()

    async def predicate(request: ToolCallRequest) -> bool:
        return (request.user.tenant_id if request.user else "") == "tenantA"

    reg.register("fake", [_fake_tool("fake_read"), _fake_tool("fake_write")], predicate)

    vis_a = await reg.visible_tools(_req("tenantA"))
    vis_b = await reg.visible_tools(_req("tenantB"))

    assert {t.name for t in vis_a} == {"fake_read", "fake_write"}
    assert vis_b == []


@pytest.mark.asyncio
async def test_predicate_is_cached_within_ttl():
    """Calling visible_tools twice for the same tenant only hits the predicate once."""
    reg = ConnectorRegistry()
    calls = {"count": 0}

    async def predicate(request: ToolCallRequest) -> bool:
        calls["count"] += 1
        return True

    reg.register("fake", [_fake_tool()], predicate, cache_ttl_seconds=60.0)

    await reg.visible_tools(_req("tenantA"))
    await reg.visible_tools(_req("tenantA"))
    await reg.visible_tools(_req("tenantA"))

    assert calls["count"] == 1

    # Different tenant ID → distinct cache key → one extra predicate call.
    await reg.visible_tools(_req("tenantB"))
    assert calls["count"] == 2


@pytest.mark.asyncio
async def test_cache_expires_after_ttl(monkeypatch):
    """After the TTL, the predicate is re-evaluated."""
    import assistant_service.core.tools.connector_registry as cr

    reg = ConnectorRegistry()
    calls = {"count": 0}

    async def predicate(request: ToolCallRequest) -> bool:
        calls["count"] += 1
        return True

    # TTL = 1s but we'll manipulate time.time() to skip past it cheaply.
    reg.register("fake", [_fake_tool()], predicate, cache_ttl_seconds=1.0)

    base = 1_000_000.0
    current = {"t": base}

    def fake_time():
        return current["t"]

    monkeypatch.setattr(cr.time, "time", fake_time)

    await reg.visible_tools(_req("tenantA"))
    assert calls["count"] == 1

    current["t"] = base + 0.5  # still inside TTL
    await reg.visible_tools(_req("tenantA"))
    assert calls["count"] == 1

    current["t"] = base + 5.0  # past TTL
    await reg.visible_tools(_req("tenantA"))
    assert calls["count"] == 2


@pytest.mark.asyncio
async def test_predicate_exception_hides_connector():
    """A crashing predicate doesn't break the whole request — it just hides
    its tools. Other connectors still show up normally."""
    reg = ConnectorRegistry()

    async def bad_predicate(request: ToolCallRequest) -> bool:
        raise RuntimeError("db down")

    async def good_predicate(request: ToolCallRequest) -> bool:
        return True

    reg.register("bad", [_fake_tool("bad_tool")], bad_predicate)
    reg.register("good", [_fake_tool("good_tool")], good_predicate)

    vis = await reg.visible_tools(_req("tenantA"))
    names = {t.name for t in vis}
    assert "bad_tool" not in names
    assert "good_tool" in names


@pytest.mark.asyncio
async def test_reregister_replaces_entry():
    reg = ConnectorRegistry()

    async def p_old(request: ToolCallRequest) -> bool:
        return True

    async def p_new(request: ToolCallRequest) -> bool:
        return False

    reg.register("fake", [_fake_tool("v1")], p_old)
    assert {t.name for t in await reg.visible_tools(_req("t"))} == {"v1"}

    reg.register("fake", [_fake_tool("v2")], p_new)
    assert await reg.visible_tools(_req("t")) == []


@pytest.mark.asyncio
async def test_unregister_removes_entry():
    reg = ConnectorRegistry()

    async def predicate(request: ToolCallRequest) -> bool:
        return True

    reg.register("fake", [_fake_tool()], predicate)
    assert reg.unregister("fake") is True
    assert await reg.visible_tools(_req("t")) == []
    assert reg.unregister("fake") is False  # already gone


@pytest.mark.asyncio
async def test_invalidate_clears_cache():
    reg = ConnectorRegistry()
    calls = {"count": 0}

    async def predicate(request: ToolCallRequest) -> bool:
        calls["count"] += 1
        return True

    reg.register("fake", [_fake_tool()], predicate)
    await reg.visible_tools(_req("tenantA"))
    await reg.visible_tools(_req("tenantA"))
    assert calls["count"] == 1

    reg.invalidate("fake", "tenantA")
    await reg.visible_tools(_req("tenantA"))
    assert calls["count"] == 2


@pytest.mark.asyncio
async def test_tenant_extracted_from_metadata_when_user_missing():
    """If no user context, predicate still sees tenant_id from metadata."""
    reg = ConnectorRegistry()
    seen_tenants: list[str] = []

    async def predicate(request: ToolCallRequest) -> bool:
        # Predicate inspects the request directly.
        tenant_id = ""
        user = request.user
        if user is not None:
            tenant_id = user.tenant_id
        if not tenant_id:
            tenant_id = str((request.metadata or {}).get("tenant_id") or "")
        seen_tenants.append(tenant_id)
        return bool(tenant_id)

    reg.register("fake", [_fake_tool()], predicate)

    req = ToolCallRequest(
        call_id="c", tool_name="probe", arguments={},
        metadata={"tenant_id": "tenantMeta"},
    )
    vis = await reg.visible_tools(req)
    assert {t.name for t in vis} == {"fake_tool"}
    assert seen_tenants == ["tenantMeta"]


def test_singleton_access_mirrors_tool_registry():
    """get_connector_registry() returns the same instance across calls."""
    reset_connector_registry_for_tests()
    a = get_connector_registry()
    b = get_connector_registry()
    assert a is b


@pytest.mark.asyncio
async def test_is_active_single_connector():
    reg = ConnectorRegistry()

    async def predicate(request: ToolCallRequest) -> bool:
        return request.user.tenant_id == "ok"

    reg.register("fake", [_fake_tool()], predicate)
    assert await reg.is_active("fake", _req("ok")) is True
    assert await reg.is_active("fake", _req("no")) is False
    assert await reg.is_active("missing-connector", _req("ok")) is False
