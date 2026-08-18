from __future__ import annotations

from datetime import datetime, timedelta, timezone
from types import SimpleNamespace
from unittest.mock import AsyncMock
from urllib.parse import parse_qs, urlsplit

import pytest
from ai_gateway_core.security import encrypt_value
from fastapi import HTTPException

from src.api.v1.connectors import (
    OAUTH_STATE_TTL_SECONDS,
    _consume_oauth_state,
    _decrypt_connector_secret,
    _refresh_token_if_needed,
    _store_oauth_state,
    initiate_oauth,
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


def _request(
    redis=None,
    database=None,
    encryption_key: str = "connector-oauth-test-key",
) -> SimpleNamespace:
    return SimpleNamespace(
        app=SimpleNamespace(
            state=SimpleNamespace(
                redis=redis,
                database=database,
                connector_encryption_key=encryption_key,
            )
        ),
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
async def test_oauth_callback_rejects_state_provider_mismatch_without_user(monkeypatch) -> None:
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
            provider="slack",
            code="provider-code",
            state=state,
            request=request,
        )

    assert exc_info.value.status_code == 400
    token_post.assert_not_called()


class _ConnectorDb:
    def __init__(self, config: dict | None = None) -> None:
        self.config = config
        self.executions: list[tuple[str, tuple]] = []

    async def fetchrow(self, query: str, *_args):
        if "FROM connector_configs" in query:
            return self.config
        raise AssertionError(f"unexpected query: {query}")

    async def execute(self, query: str, *args) -> None:
        self.executions.append((query, args))


@pytest.mark.asyncio
async def test_oauth_init_decodes_json_string_extra_config() -> None:
    db = _ConnectorDb(
        {
            "tenant_id": "tenant",
            "provider": "confluence",
            "client_id": "client-id",
            "auth_url": "https://oauth.example/authorize",
            "token_url": "https://oauth.example/token",
            "redirect_uri": None,
            "scopes": "read",
            "enabled": True,
            "extra_config": '{"audience":"custom-audience"}',
        }
    )
    request = _request(redis=_FakeRedis(), database=db)

    result = await initiate_oauth("confluence", request, _user())

    params = parse_qs(urlsplit(result["auth_url"]).query)
    assert params["audience"] == ["custom-audience"]


@pytest.mark.asyncio
async def test_oauth_callback_decrypts_secret_and_uses_safe_form_post(monkeypatch) -> None:
    key = "connector-oauth-test-key"
    db = _ConnectorDb(
        {
            "tenant_id": "tenant",
            "provider": "github",
            "client_id": "client-id",
            "client_secret": encrypt_value("synthetic-client-secret", key),
            "auth_url": "https://oauth.example/authorize",
            "token_url": "https://oauth.example/token",
            "redirect_uri": None,
            "scopes": "repo:read",
            "enabled": True,
            "extra_config": {},
        }
    )
    redis = _FakeRedis()
    request = _request(redis=redis, database=db, encryption_key=key)
    state = "tenant:user:github:nonce"
    await _store_oauth_state(
        request,
        state=state,
        tenant_id="tenant",
        user_id="user",
        provider="github",
        nonce="nonce",
    )
    response = SimpleNamespace(
        status_code=200,
        json=lambda: {
            "access_token": "synthetic-access-token",
            "refresh_token": "synthetic-refresh-token",
            "expires_in": 3600,
        },
    )
    safe_post = AsyncMock(return_value=response)
    monkeypatch.setattr("src.api.v1.connectors.safe_form_post", safe_post)

    result = await oauth_callback(
        provider="github",
        code="provider-code",
        state=state,
        request=request,
    )

    assert result.status_code == 302
    assert "connected=github" in result.headers["location"]
    call = safe_post.await_args
    assert call.args == ("https://oauth.example/token",)
    assert call.kwargs["data"]["client_secret"] == "synthetic-client-secret"
    assert call.kwargs["headers"] == {"Accept": "application/json"}
    assert db.executions


@pytest.mark.asyncio
async def test_refresh_uses_safe_form_post_with_decrypted_secret(monkeypatch) -> None:
    key = "connector-oauth-test-key"
    request = _request(encryption_key=key)
    config = _decrypt_connector_secret(
        request,
        {
            "client_id": "client-id",
            "client_secret": encrypt_value("synthetic-client-secret", key),
            "token_url": "https://oauth.example/token",
        },
    )
    db = _ConnectorDb()
    response = SimpleNamespace(
        status_code=200,
        json=lambda: {"access_token": "new-access-token", "expires_in": 3600},
    )
    safe_post = AsyncMock(return_value=response)
    monkeypatch.setattr("src.api.v1.connectors.safe_form_post", safe_post)

    token = await _refresh_token_if_needed(
        db,
        request,
        {
            "id": "connector-id",
            "access_token": "expired-token",
            "refresh_token": "synthetic-refresh-token",
            "token_expires_at": datetime.now(timezone.utc) - timedelta(seconds=1),
        },
        config,
    )

    assert token == "new-access-token"
    call = safe_post.await_args
    assert call.args == ("https://oauth.example/token",)
    assert call.kwargs["data"]["client_secret"] == "synthetic-client-secret"
    assert db.executions
