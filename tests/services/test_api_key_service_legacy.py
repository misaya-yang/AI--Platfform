from __future__ import annotations

from unittest.mock import AsyncMock

import pytest

from src.services.auth.api_key_service import APIKeyService


@pytest.mark.asyncio
async def test_list_api_keys_uses_stable_public_key_id_for_legacy_rows():
    db = AsyncMock()
    db.fetchrow = AsyncMock(return_value={"column_name": "scopes"})
    db.fetch = AsyncMock(
        return_value=[
            {
                "key_id": "ak_legacy_3",
                "name": "legacy",
                "is_active": True,
                "created_at": "2026-01-01T00:00:00Z",
            }
        ]
    )

    service = APIKeyService(db)
    rows = await service.list_api_keys()

    assert rows[0]["key_id"] == "ak_legacy_3"
    query = db.fetch.call_args.args[0]
    assert "COALESCE(key_id, ('ak_legacy_' || id::text)) AS key_id" in query


@pytest.mark.asyncio
async def test_list_api_keys_uses_permissions_as_scopes_for_base_schema():
    db = AsyncMock()
    db.fetchrow = AsyncMock(return_value={"column_name": "permissions"})
    db.fetch = AsyncMock(return_value=[{"key_id": "ak_legacy_3", "scopes": ["knowledge:read"]}])

    service = APIKeyService(db)
    rows = await service.list_api_keys()

    assert rows[0]["scopes"] == ["knowledge:read"]
    query = db.fetch.call_args.args[0]
    assert "permissions AS scopes" in query


@pytest.mark.asyncio
async def test_create_api_key_uses_permissions_as_scopes_for_base_schema():
    db = AsyncMock()
    db.fetchrow = AsyncMock(
        side_effect=[
            {"column_name": "permissions"},
            {
                "id": 1,
                "key_id": "ak_created",
                "key_prefix": "agk_created",
                "name": "created",
                "description": "",
                "scopes": ["knowledge:read"],
                "roles": ["user"],
                "tier": "normal",
                "rate_limit": None,
                "created_at": "2026-01-01T00:00:00Z",
                "expires_at": None,
                "enabled": True,
            },
        ]
    )

    service = APIKeyService(db)
    result = await service.create_api_key(
        name="created",
        user_id="user-1",
        scopes=["knowledge:read"],
    )

    assert result["scopes"] == ["knowledge:read"]
    assert result["is_active"] is True
    query = db.fetchrow.call_args_list[1].args[0]
    assert "tenant_id, permissions, roles" in query
    assert "permissions AS scopes" in query


@pytest.mark.asyncio
async def test_validate_api_key_uses_permissions_as_scopes_for_base_schema():
    db = AsyncMock()
    db.fetchrow = AsyncMock(
        side_effect=[
            {"column_name": "permissions"},
            {
                "id": 9,
                "key_id": "ak_valid",
                "name": "valid",
                "user_id": "user-1",
                "tenant_id": None,
                "scopes": ["knowledge:read"],
                "roles": ["user"],
                "tier": None,
                "rate_limit": None,
                "enabled": True,
                "expires_at": None,
            },
            {"id": 9},
        ]
    )

    service = APIKeyService(db)
    result = await service.validate_api_key("agk_test")

    assert result == {
        "key_id": "ak_valid",
        "name": "valid",
        "user_id": "user-1",
        "tenant_id": "",
        "scopes": ["knowledge:read"],
        "tier": "normal",
        "roles": ["user"],
        "rate_limit": None,
    }
    query = db.fetchrow.call_args_list[1].args[0]
    assert "ak.permissions AS scopes" in query


@pytest.mark.asyncio
async def test_get_api_key_matches_by_public_legacy_key_id():
    db = AsyncMock()
    db.fetchrow = AsyncMock(side_effect=[{"column_name": "scopes"}, {"key_id": "ak_legacy_4"}])

    service = APIKeyService(db)
    row = await service.get_api_key("ak_legacy_4")

    assert row == {"key_id": "ak_legacy_4"}
    query, key_id = db.fetchrow.call_args_list[1].args
    assert "WHERE COALESCE(key_id, ('ak_legacy_' || id::text)) = $1" in query
    assert key_id == "ak_legacy_4"


@pytest.mark.asyncio
async def test_revoke_and_delete_use_public_legacy_key_id():
    db = AsyncMock()
    db.fetchrow = AsyncMock(side_effect=[{"key_id": "ak_legacy_7"}, {"key_id": "ak_legacy_7"}])

    service = APIKeyService(db)

    revoked = await service.revoke_api_key("ak_legacy_7")
    deleted = await service.delete_api_key("ak_legacy_7")

    assert revoked is True
    assert deleted is True
    revoke_query = db.fetchrow.call_args_list[0].args[0]
    delete_query = db.fetchrow.call_args_list[1].args[0]
    assert "COALESCE(key_id, ('ak_legacy_' || id::text)) = $1" in revoke_query
    assert "COALESCE(key_id, ('ak_legacy_' || id::text)) = $1" in delete_query
