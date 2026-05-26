from __future__ import annotations

from types import SimpleNamespace
from unittest.mock import AsyncMock

import pytest
from fastapi import HTTPException

from src.api.v1.connectors import (
    OAUTH_STATE_TTL_SECONDS,
    _consume_oauth_state,
    _store_oauth_state,
    oauth_callback,
)
from src.core.auth.user_resolver import UserContext


class _FakeRedis:
    def __init__(self) -> None:
        self.saved: dict[str, tuple[dict, int | None]] = {}
        self.deleted: list[str] = []

    async def save(self, key: str, value: dict, ttl: int | None = None) -> None:
        self.saved[key] = (value, ttl)

    async def get(self, key: str):
        item = self.saved.get(key)
        return item[0] if item else None

    async def delete(self, key: str) -> bool:
        self.deleted.append(key)
        return self.saved.pop(key, None) is not None


def _request(redis=None) -> SimpleNamespace:
    return SimpleNamespace(
        app=SimpleNamespace(state=SimpleNamespace(redis=redis)),
        base_url="http://test/",
    )


def _user(user_id: str = "user", tenant_id: str = "tenant") -> UserContext:
    return UserContext(
        user_id=user_id,
        tenant_id=tenant_id,
        is_authenticated=True,
    )


@pytest.mark.asyncio
async def test_oauth_callback_rejects_unissued_state_before_token_exchange() -> None:
    with pytest.raises(HTTPException) as exc_info:
        await oauth_callback(
            provider="github",
            code="attacker-code",
            state="tenant:user:github:forgednonce",
            request=_request(),
            user=_user(),
        )

    assert exc_info.value.status_code == 400
    assert "state" in str(exc_info.value.detail).lower()


@pytest.mark.asyncio
async def test_oauth_state_is_stored_in_redis_with_ttl_and_consumed_once() -> None:
    redis = _FakeRedis()
    request = _request(redis)
    state = "tenant:user:github:nonce"

    await _store_oauth_state(
        request,
        state=state,
        tenant_id="tenant",
        user_id="user",
        provider="github",
        nonce="nonce",
    )

    assert not hasattr(request.app.state, "oauth_states")
    assert len(redis.saved) == 1
    stored_key, (stored_value, ttl) = next(iter(redis.saved.items()))
    assert stored_key.endswith(state)
    assert ttl == OAUTH_STATE_TTL_SECONDS
    assert stored_value["tenant_id"] == "tenant"

    consumed = await _consume_oauth_state(request, state)

    assert consumed["user_id"] == "user"
    assert redis.deleted == [stored_key]


@pytest.mark.asyncio
async def test_oauth_callback_rejects_valid_state_before_token_exchange_when_user_mismatch(monkeypatch) -> None:
    token_post = AsyncMock()
    monkeypatch.setattr("src.api.v1.connectors.httpx.AsyncClient.post", token_post)
    redis = _FakeRedis()
    request = _request(redis)
    state = "tenant:alice:github:nonce"
    await _store_oauth_state(
        request,
        state=state,
        tenant_id="tenant",
        user_id="alice",
        provider="github",
        nonce="nonce",
    )

    with pytest.raises(HTTPException) as exc_info:
        await oauth_callback(
            provider="github",
            code="provider-code",
            state=state,
            request=request,
            user=_user(user_id="bob", tenant_id="tenant"),
        )

    assert exc_info.value.status_code == 403
    token_post.assert_not_called()
