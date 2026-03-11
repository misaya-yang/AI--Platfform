from __future__ import annotations

import pytest


class StubQuranUserService:
    async def get_auth_config(self):
        return {
            "generated_at": "2026-03-11T00:00:00Z",
            "enabled": True,
            "auth_url": "https://oauth2.quran.foundation",
            "api_base_url": "https://api.quran.foundation/api/v1",
            "redirect_uri": "https://wahda.example/callback",
            "post_logout_redirect_uri": None,
            "scopes": ["openid", "profile"],
        }

    async def get_authorize_url(self, **kwargs):
        return {
            "generated_at": "2026-03-11T00:00:00Z",
            "authorize_url": "https://oauth2.quran.foundation/oauth2/authorize?client_id=abc",
            "redirect_uri": kwargs["redirect_uri"] or "https://wahda.example/callback",
            "scopes": kwargs["scopes"] or ["openid", "profile"],
        }

    async def exchange_code(self, **kwargs):
        return {
            "generated_at": "2026-03-11T00:00:00Z",
            "token": {"access_token": "user-token", "refresh_token": "refresh-token"},
        }

    async def refresh_token(self, refresh_token: str):
        return {
            "generated_at": "2026-03-11T00:00:00Z",
            "token": {"access_token": "new-user-token", "refresh_token": refresh_token},
        }

    async def get_userinfo(self, access_token: str):
        return {
            "generated_at": "2026-03-11T00:00:00Z",
            "userinfo": {"sub": "123", "name": "Test User", "token": access_token},
        }

    async def proxy_request(self, **kwargs):
        return {
            "generated_at": "2026-03-11T00:00:00Z",
            "method": kwargs["method"].upper(),
            "path": kwargs["path"],
            "payload": {"ok": True},
        }


@pytest.mark.asyncio
async def test_quran_user_endpoints(async_client, test_app):
    test_app.state.quran_user_service = StubQuranUserService()

    config = await async_client.get("/api/v1/quran/user/auth/config")
    authorize_url = await async_client.get(
        "/api/v1/quran/user/auth/authorize-url",
        params={"redirect_uri": "https://wahda.example/callback", "state": "abc"},
    )
    token = await async_client.post(
        "/api/v1/quran/user/auth/token",
        json={"code": "auth-code", "redirect_uri": "https://wahda.example/callback"},
    )
    userinfo = await async_client.get(
        "/api/v1/quran/user/userinfo",
        headers={"Authorization": "Bearer user-token"},
    )
    proxy = await async_client.post(
        "/api/v1/quran/user/request",
        json={
            "method": "GET",
            "path": "bookmarks",
            "access_token": "user-token",
            "query": {"page": 1},
        },
    )

    assert config.status_code == 200
    assert config.json()["enabled"] is True
    assert authorize_url.status_code == 200
    assert "authorize" in authorize_url.json()["authorize_url"]
    assert token.status_code == 200
    assert token.json()["token"]["access_token"] == "user-token"
    assert userinfo.status_code == 200
    assert userinfo.json()["userinfo"]["sub"] == "123"
    assert proxy.status_code == 200
    assert proxy.json()["path"] == "bookmarks"
