from types import SimpleNamespace

import pytest

from src.api.v1.confluence import list_all_bindings, list_connections
from src.core.auth.user_resolver import UserContext


def _request_without_confluence_service():
    return SimpleNamespace(app=SimpleNamespace(state=SimpleNamespace()))


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
