from __future__ import annotations

from unittest.mock import AsyncMock, Mock

import pytest
from fastapi import HTTPException

import src.api.v1.api_keys as api_keys_module
from src.api.v1.api_keys import CreateAPIKeyRequest, create_api_key


def _current_user(*, tier: str = "normal") -> dict[str, object]:
    return {
        "user_id": "user-1",
        "tenant_id": "tenant-1",
        "roles": ["user"],
        "tier": tier,
    }


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "scopes",
    [
        pytest.param(["admin"], id="admin-role"),
        pytest.param(["admin:*"], id="admin-wildcard"),
        pytest.param(["console:services:edit"], id="management-capability"),
    ],
)
async def test_self_service_api_key_rejects_privileged_or_management_scopes(
    monkeypatch: pytest.MonkeyPatch,
    scopes: list[str],
) -> None:
    create_key = AsyncMock()
    monkeypatch.setattr(api_keys_module.APIKeyService, "create_api_key", create_key)

    with pytest.raises(HTTPException) as exc_info:
        await create_api_key(
            CreateAPIKeyRequest(name="unprivileged-key", scopes=scopes),
            db=object(),
            current_user=_current_user(),
        )

    assert exc_info.value.status_code == 403
    assert exc_info.value.detail["disallowed_scopes"] == scopes
    create_key.assert_not_awaited()


@pytest.mark.asyncio
async def test_self_service_api_key_ignores_forged_tier_and_uses_caller_tier(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    create_key = AsyncMock(return_value={"key_id": "ak_test"})
    clear_cache = Mock()
    monkeypatch.setattr(api_keys_module.APIKeyService, "create_api_key", create_key)
    monkeypatch.setattr(api_keys_module, "clear_service_access_constraint_cache", clear_cache)

    request = CreateAPIKeyRequest(
        name="unprivileged-key",
        scopes=["knowledge:read"],
        tier="admin",
    )
    result = await create_api_key(
        request,
        db=object(),
        current_user=_current_user(tier="normal"),
    )

    assert result == {"key_id": "ak_test"}
    assert create_key.await_args.kwargs["scopes"] == ["knowledge:read"]
    assert create_key.await_args.kwargs["tier"] == "normal"
    assert create_key.await_args.kwargs["tier"] != request.tier
    clear_cache.assert_called_once_with()
