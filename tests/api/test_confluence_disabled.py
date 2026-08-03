from types import SimpleNamespace

import pytest
from fastapi import HTTPException

from src.api.v1.confluence import (
    list_all_bindings,
    list_connections,
)
from src.api.v1.confluence import (
    test_connection_credentials as probe_connection_credentials,
)
from src.core.auth.user_resolver import UserContext


def _request_without_confluence_service():
    return SimpleNamespace(app=SimpleNamespace(state=SimpleNamespace()))


def _request_with_rbac():
    class RBAC:
        def require(self, _roles, permission):
            assert permission == "confluence:manage"

    return SimpleNamespace(
        app=SimpleNamespace(
            state=SimpleNamespace(dispatcher=SimpleNamespace(rbac=RBAC()))
        )
    )


def _user() -> UserContext:
    return UserContext(
        user_id="user_1",
        tenant_id="tenant_1",
        is_authenticated=True,
        roles=["admin"],
    )


@pytest.mark.asyncio
async def test_confluence_connection_list_returns_empty_when_integration_disabled():
    result = await list_connections(_request_without_confluence_service(), user=_user())

    assert result == []


@pytest.mark.asyncio
async def test_confluence_binding_list_returns_empty_when_integration_disabled():
    result = await list_all_bindings(_request_without_confluence_service(), user=_user())

    assert result == []


@pytest.mark.asyncio
async def test_confluence_credential_probe_is_fail_closed_before_any_network_call():
    with pytest.raises(HTTPException) as exc_info:
        await probe_connection_credentials(
            _request_with_rbac(),
            payload={
                "domain": "127.0.0.1",
                "email": "attacker@example.test",
                "api_token": "not-used",
            },
            user=_user(),
        )

    assert exc_info.value.status_code == 503
    assert "trusted-origin" in str(exc_info.value.detail)
