"""End-to-end H-2 auth contract: admin role must survive the gateway → proxy → assistant-service hop.

This test exercises the full chain in-process:

    JWT (roles=["admin"])
        ↓  gateway UserResolver (real JWT decode, HS256)
    UserContext(roles=["admin"])
        ↓  gateway proxy (real httpx client, but ASGI-transport)
    HTTP → fake assistant-service FastAPI app
        ↓  real GatewaySecretAuthMiddleware
        ↓  real get_user_context
    UserContext(roles=["admin"])  ← asserted here

H-2 (audit): before Phase 5a the proxy did not forward roles, so
downstream ``user.roles`` was always ``["user"]`` and ``tool_registry``
silently downgraded every admin to a normal user. This test locks the
end-to-end contract — it must fail loudly if the header injection or
parsing ever regresses.

Reference: plans/Audit-Assistant-Proxy-Current-HEAD-2026-04-23.md §H-2,
plans/TechWhitePaper-Service-Extraction-2026-04-23.md §二 item 7.
"""
from __future__ import annotations

import httpx
import jwt as pyjwt
import pytest
from fastapi import Depends, FastAPI, Request
from fastapi.testclient import TestClient

from ai_gateway_core.auth.gateway_secret import GatewaySecret, InMemoryReplayStore
from assistant_service.auth import GatewaySecretAuthMiddleware
from assistant_service.auth import get_user_context as assistant_get_user_context
from src.core.auth.user_resolver import UserResolver, UserResolverConfig


JWT_SECRET = "e2e-jwt-secret-32-chars-XXXXXXXXX"
GATEWAY_SECRET = "e2e-gateway-secret-32-chars-YYYYYYY"


def _make_jwt(*, roles: list[str], sub: str = "admin-user", tenant_id: str = "t1", tier: str = "admin") -> str:
    payload = {
        "sub": sub,
        "tenant_id": tenant_id,
        "tier": tier,
        "roles": roles,
    }
    return pyjwt.encode(payload, JWT_SECRET, algorithm="HS256")


def _build_fake_assistant_service_app(shared_replay_store: InMemoryReplayStore) -> FastAPI:
    """A minimal FastAPI app that plays the role of assistant-service.

    Runs the exact same ``GatewaySecretAuthMiddleware`` and
    ``get_user_context`` code assistant-service runs, then echoes the
    resolved identity so the gateway-side test can assert on it.
    """
    app = FastAPI()
    gs = GatewaySecret(secret=GATEWAY_SECRET, replay_store=shared_replay_store)
    app.add_middleware(
        GatewaySecretAuthMiddleware,
        gateway_secret=gs,
        allow_anonymous=False,
    )

    @app.post("/api/v1/assistant/echo-auth")
    async def echo_auth(user=Depends(assistant_get_user_context)):
        return {
            "user_id": user.user_id,
            "tenant_id": user.tenant_id,
            "tier": user.tier,
            "user_type": user.user_type,
            "roles": list(user.roles),
        }

    return app


def _build_gateway_app(monkeypatch, fake_as_app: FastAPI) -> FastAPI:
    """Gateway app with a single proxy route that delegates to assistant-service.

    We swap the shared ``ServiceProxy``'s ``_get_client`` to point at an
    httpx client using ``ASGITransport`` — that way the gateway thinks
    it's talking to a real HTTP endpoint but actually hits our in-process
    fake app. The request still goes through the real sign/verify path.
    """
    # The gateway proxy reads GATEWAY_ASSISTANT_SHARED_SECRET at import
    # time. Set it *before* importing the module so the signer is built.
    monkeypatch.setenv("GATEWAY_ASSISTANT_SHARED_SECRET", GATEWAY_SECRET)

    import importlib

    import src.api.v1._assistant_proxy as ap

    importlib.reload(ap)

    transport = httpx.ASGITransport(app=fake_as_app)
    asgi_client = httpx.AsyncClient(
        transport=transport, base_url="http://assistant-service"
    )

    async def fake_get_client():
        return asgi_client

    # Patch BOTH the client factory and the reset path so retries don't
    # spin up a fresh real httpx client during this test.
    monkeypatch.setattr(ap._proxy, "_get_client", fake_get_client)
    async def _noop_reset():
        pass
    monkeypatch.setattr(ap._proxy, "_reset_client", _noop_reset)

    resolver = UserResolver(
        UserResolverConfig(
            jwt_enabled=True,
            jwt_secret=JWT_SECRET,
            jwt_algorithms=["HS256"],
        )
    )

    gw = FastAPI()

    @gw.post("/gw/echo-auth")
    async def route(request: Request):
        user = await resolver.resolve(request)
        body = await request.body()
        return await ap.proxy_to_assistant_service(
            request, user, path="echo-auth", body=body
        )

    return gw


@pytest.mark.asyncio
async def test_admin_roles_survive_gateway_to_assistant_hop(monkeypatch) -> None:
    """The star test for audit Finding H-2."""
    replay_store = InMemoryReplayStore()
    fake_as = _build_fake_assistant_service_app(replay_store)
    gateway = _build_gateway_app(monkeypatch, fake_as)

    token = _make_jwt(roles=["admin"])

    with TestClient(gateway) as client:
        r = client.post(
            "/gw/echo-auth",
            headers={"Authorization": f"Bearer {token}"},
            json={},
        )

    assert r.status_code == 200, r.text
    body = r.json()
    assert body["user_id"] == "admin-user"
    assert body["tenant_id"] == "t1"
    assert body["tier"] == "admin"
    # The star assertion — without the Phase 5a fix this was silently
    # downgraded to ["user"].
    assert body["roles"] == ["admin"], (
        f"roles dropped at proxy boundary — got {body['roles']}"
    )


@pytest.mark.asyncio
async def test_multi_role_jwt_preserved(monkeypatch) -> None:
    """Multiple roles round-trip intact, comma-separated at the wire."""
    replay_store = InMemoryReplayStore()
    fake_as = _build_fake_assistant_service_app(replay_store)
    gateway = _build_gateway_app(monkeypatch, fake_as)

    token = _make_jwt(roles=["admin", "premium-analyst", "beta-tester"])

    with TestClient(gateway) as client:
        r = client.post(
            "/gw/echo-auth",
            headers={"Authorization": f"Bearer {token}"},
            json={},
        )

    assert r.status_code == 200, r.text
    assert r.json()["roles"] == ["admin", "premium-analyst", "beta-tester"]


@pytest.mark.asyncio
async def test_no_roles_claim_defaults_to_user(monkeypatch) -> None:
    """JWT without a roles claim → downstream sees ['user']."""
    replay_store = InMemoryReplayStore()
    fake_as = _build_fake_assistant_service_app(replay_store)
    gateway = _build_gateway_app(monkeypatch, fake_as)

    # JWT with empty roles list simulates "no roles claim".
    token = _make_jwt(roles=[])

    with TestClient(gateway) as client:
        r = client.post(
            "/gw/echo-auth",
            headers={"Authorization": f"Bearer {token}"},
            json={},
        )

    assert r.status_code == 200, r.text
    # Gateway forwards empty X-User-Roles; assistant-service defaults
    # to ["user"] — the audited default.
    assert r.json()["roles"] == ["user"]


@pytest.mark.asyncio
async def test_request_without_gateway_secret_rejected(monkeypatch) -> None:
    """Calling the fake assistant-service directly (bypassing the
    gateway proxy, so no ``X-Gateway-Secret``) must yield 401 — this
    is the H-4 sibling-container defense showing up in the e2e path.
    """
    replay_store = InMemoryReplayStore()
    fake_as = _build_fake_assistant_service_app(replay_store)

    with TestClient(fake_as) as client:
        r = client.post(
            "/api/v1/assistant/echo-auth",
            headers={"x-user-id": "attacker", "x-tenant-id": "t1"},
            json={},
        )

    assert r.status_code == 401
    assert r.json()["code"] == "AUTH_DENIED"
