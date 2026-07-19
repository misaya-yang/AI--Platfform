from __future__ import annotations

from types import SimpleNamespace
from typing import Any

import pytest
from ai_gateway_core.persistence.repositories.mcp_repository import (
    DatabaseMCPRepository,
    MCPAuthorizationError,
    MCPValidationError,
)
from assistant_service.core.mcp.runtime import MappingSecretResolver, MCPRuntimeService
from assistant_service.core.tool_invoker import (
    CapabilityAllowlist,
    RegistryToolInvoker,
    ToolInvocationContext,
)
from assistant_service.core.tools.confluence_tool import (
    CONFLUENCE_READ_DEFINITION,
    _TenantClientResolver,
)
from assistant_service.core.tools.tool_registry import (
    ToolCallRequest,
    ToolCallResult,
    ToolRegistry,
)


class _Repository(DatabaseMCPRepository):
    def __init__(self, row: dict[str, Any] | None):
        super().__init__(SimpleNamespace(enabled=True, _pool=object()))
        self.row = row

    async def fetchrow(self, _query: str, *_args: Any) -> dict[str, Any] | None:
        return dict(self.row) if self.row is not None else None


def _principal(**changes: Any) -> dict[str, Any]:
    row = {
        "grant_id": "11111111-1111-4111-8111-111111111111",
        "provider": "confluence",
        "principal_type": "user_delegated",
        "owner_user_id": "user-a",
        "secret_ref": "vault://tenant-a/confluence/user-a",
        "scopes": ["read:confluence-content.all"],
        "audience": "confluence",
        "connection_metadata": {
            "domain": "tenant-a.atlassian.net",
            "email": "user-a@example.com",
        },
        "allowed_channels": ["preview", "hosted_private"],
        "enabled": True,
        "revoked_at": None,
    }
    row.update(changes)
    return row


@pytest.mark.asyncio
async def test_delegated_connector_resolves_only_the_current_user() -> None:
    repository = _Repository(_principal())
    allowed = await repository.authorize_connector_tool(
        tenant_id="tenant-a",
        user_id="user-a",
        authenticated=True,
        provider="confluence",
        tool_name="confluence_read",
        principal_type="user_delegated",
        grant_id="11111111-1111-4111-8111-111111111111",
        channel="preview",
    )
    assert allowed["owner_user_id"] == "user-a"

    for user_id, authenticated, channel in (
        ("user-b", True, "preview"),
        ("user-a", False, "preview"),
        ("user-a", True, "embed"),
    ):
        with pytest.raises(
            MCPAuthorizationError, match="CONNECTOR_DELEGATED_PRINCIPAL_DENIED"
        ):
            await repository.authorize_connector_tool(
                tenant_id="tenant-a",
                user_id=user_id,
                authenticated=authenticated,
                provider="confluence",
                tool_name="confluence_read",
                principal_type="user_delegated",
                grant_id="11111111-1111-4111-8111-111111111111",
                channel=channel,
            )


@pytest.mark.asyncio
async def test_public_connector_requires_explicit_read_only_service_account_grant() -> None:
    repository = _Repository(
        _principal(
            principal_type="service_account",
            owner_user_id=None,
            allowed_channels=["preview", "embed"],
        )
    )
    allowed = await repository.authorize_connector_tool(
        tenant_id="tenant-a",
        user_id="",
        authenticated=False,
        provider="confluence",
        tool_name="confluence_read",
        principal_type="service_account",
        grant_id="11111111-1111-4111-8111-111111111111",
        channel="embed",
    )
    assert allowed["principal_type"] == "service_account"

    with pytest.raises(MCPAuthorizationError, match="CONNECTOR_PUBLIC_CHANNEL_DENIED"):
        await repository.authorize_connector_tool(
            tenant_id="tenant-a",
            user_id="",
            authenticated=False,
            provider="confluence",
            tool_name="confluence_write",
            principal_type="service_account",
            grant_id="11111111-1111-4111-8111-111111111111",
            channel="embed",
        )

    repository.row = _principal(
        principal_type="service_account",
        owner_user_id=None,
        allowed_channels=["preview"],
    )
    with pytest.raises(MCPAuthorizationError, match="CONNECTOR_CHANNEL_DENIED"):
        await repository.authorize_connector_tool(
            tenant_id="tenant-a",
            user_id="",
            authenticated=False,
            provider="confluence",
            tool_name="confluence_read",
            principal_type="service_account",
            grant_id="11111111-1111-4111-8111-111111111111",
            channel="embed",
        )


@pytest.mark.asyncio
async def test_connector_tool_scope_is_enforced_at_runtime() -> None:
    repository = _Repository(
        _principal(
            principal_type="service_account",
            owner_user_id=None,
            scopes=["read:confluence-content.all"],
            allowed_channels=["preview"],
        )
    )
    with pytest.raises(MCPAuthorizationError, match="CONNECTOR_SCOPE_DENIED"):
        await repository.authorize_connector_tool(
            tenant_id="tenant-a",
            user_id="admin-a",
            authenticated=True,
            provider="confluence",
            tool_name="confluence_write",
            principal_type="service_account",
            grant_id="11111111-1111-4111-8111-111111111111",
            channel="preview",
        )

    repository.row = _principal(
        principal_type="service_account",
        owner_user_id=None,
        scopes=["write:confluence-content"],
        allowed_channels=["preview"],
    )
    allowed = await repository.authorize_connector_tool(
        tenant_id="tenant-a",
        user_id="admin-a",
        authenticated=True,
        provider="confluence",
        tool_name="confluence_write",
        principal_type="service_account",
        grant_id="11111111-1111-4111-8111-111111111111",
        channel="preview",
    )
    assert allowed["scopes"] == ["write:confluence-content"]

@pytest.mark.asyncio
async def test_revoked_or_expired_connector_has_no_fallback() -> None:
    repository = _Repository(None)
    with pytest.raises(MCPAuthorizationError, match="CONNECTOR_CAPABILITY_UNAVAILABLE"):
        await repository.authorize_connector_tool(
            tenant_id="tenant-a",
            user_id="user-a",
            authenticated=True,
            provider="confluence",
            tool_name="confluence_read",
            principal_type="user_delegated",
            grant_id="11111111-1111-4111-8111-111111111111",
            channel="preview",
        )


@pytest.mark.asyncio
async def test_principal_creation_rejects_delegated_public_channels() -> None:
    repository = _Repository(None)
    with pytest.raises(MCPValidationError, match="CONNECTOR_PUBLIC_CHANNEL_DENIED"):
        await repository.create_connector_principal(
            tenant_id="tenant-a",
            user_id="admin-a",
            provider="confluence",
            principal_type="user_delegated",
            owner_user_id="user-a",
            secret_ref="vault://tenant-a/confluence/user-a",
            scopes=["read"],
            audience="confluence",
            connection_metadata={"domain": "tenant-a.atlassian.net"},
            allowed_channels=["preview", "embed"],
            expires_at=None,
        )


@pytest.mark.asyncio
async def test_agent_confluence_path_reauthorizes_and_never_uses_legacy_fallback() -> None:
    row = _principal()

    class CredentialRepository:
        calls: list[dict[str, Any]] = []

        async def authorize_connector_tool(self, **values: Any) -> dict[str, Any]:
            self.calls.append(values)
            return row

    credentials = CredentialRepository()
    resolver = _TenantClientResolver(
        database=None,
        fallback_client=object(),  # type: ignore[arg-type]
        credential_repository=credentials,
        secret_resolver=MappingSecretResolver(
            {row["secret_ref"]: "synthetic-confluence-token"}
        ),
    )
    request = ToolCallRequest(
        call_id="call",
        tool_name="confluence_read",
        arguments={},
        user=SimpleNamespace(
            tenant_id="tenant-a", user_id="user-a", is_authenticated=True
        ),
        metadata={
            "agent_id": "agent-a",
            "user_id": "user-a",
            "tenant_id": "tenant-a",
            "connector_principal": {
                "grant_id": row["grant_id"],
                "provider": "confluence",
                "principal_type": "user_delegated",
                "channel": "preview",
            },
        },
    )

    client = await resolver.resolve(request)
    assert client.domain == "tenant-a.atlassian.net"
    assert credentials.calls[0]["user_id"] == "user-a"

    class DenyingRepository:
        async def authorize_connector_tool(self, **_values: Any) -> dict[str, Any]:
            raise MCPAuthorizationError("CONNECTOR_CAPABILITY_UNAVAILABLE")

    denied_resolver = _TenantClientResolver(
        fallback_client=object(),  # type: ignore[arg-type]
        credential_repository=DenyingRepository(),
        secret_resolver=MappingSecretResolver(
            {row["secret_ref"]: "synthetic-confluence-token"}
        ),
    )
    with pytest.raises(MCPAuthorizationError, match="CONNECTOR_CAPABILITY_UNAVAILABLE"):
        await denied_resolver.resolve(request)


@pytest.mark.asyncio
async def test_connector_revocation_removes_agent_tool_visibility_immediately() -> None:
    repository = _Repository(_principal())
    runtime = MCPRuntimeService(
        repository=repository,
        secret_resolver=MappingSecretResolver(),
    )
    registry = ToolRegistry()

    async def executor(request: ToolCallRequest) -> ToolCallResult:
        return ToolCallResult(
            call_id=request.call_id,
            tool_name=request.tool_name,
            success=True,
        )

    registry.register(CONFLUENCE_READ_DEFINITION, executor)
    binding = {
        "type": "connector",
        "id": "confluence_read",
        "risk": "low",
        "config": {
            "provider": "confluence",
            "tool_name": "confluence_read",
            "principal_type": "user_delegated",
            "grant_id": "11111111-1111-4111-8111-111111111111",
            "risk": "low",
        },
    }
    context = ToolInvocationContext(
        session_id="session",
        tenant_id="tenant-a",
        user_id="user-a",
        request_id="request",
        user=SimpleNamespace(
            tenant_id="tenant-a", user_id="user-a", is_authenticated=True
        ),
        metadata={"channel": "preview", "agent_id": "agent-a"},
        capability_allowlist=CapabilityAllowlist(
            frozenset({"confluence_read"}),
            bindings={"confluence_read": binding},
        ),
    )
    invoker = RegistryToolInvoker(tool_registry=registry, mcp_runtime=runtime)

    before = await invoker.get_tool_definitions_filtered(context)
    repository.row = None
    after = await invoker.get_tool_definitions_filtered(context)

    assert [tool.name for tool in before] == ["confluence_read"]
    assert after == []
