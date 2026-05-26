"""End-to-end auth contract tests.

Covers three scenarios required by the Phase 5b roadmap prompt:

1. **Admin roles survive the hop** — JWT ``roles=["admin"]`` → downstream
   ``UserContext.roles == ["admin"]`` (audit Finding H-2).
2. **Header smuggling is blocked** — a public client sending
   ``X-User-Type: admin`` at the gateway ingress must NOT reach
   assistant-service with that value. Gateway strips it; downstream
   sees the default ``"user"``.
3. **All 7 canonical identity headers transit** — id / tenant / tier /
   type / roles / email / name populated in the gateway's ``UserContext``
   reach assistant-service's ``UserContext`` intact.

Full chain exercised in-process:

    JWT (roles, email, name, user_type)
        ↓  gateway UserResolver (real JWT decode, HS256)
    UserContext(...)
        ↓  gateway proxy (real httpx client, ASGI-transport)
    HTTP → fake assistant-service FastAPI app
        ↓  real GatewaySecretAuthMiddleware
        ↓  real get_user_context
    UserContext(...)  ← asserted here

Reference: plans/TechWhitePaper-Service-Extraction-2026-04-23.md §二 item 7,
plans/Roadmap-Post-5a-Extraction-2026-04-23.md Phase 5b prompt.
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


def _make_jwt(
    *,
    roles: list[str],
    sub: str = "admin-user",
    tenant_id: str = "t1",
    tier: str = "admin",
    user_type: str | None = None,
    email: str | None = None,
    name: str | None = None,
) -> str:
    payload: dict = {
        "sub": sub,
        "tenant_id": tenant_id,
        "tier": tier,
        "roles": roles,
    }
    if user_type is not None:
        payload["user_type"] = user_type
    if email is not None:
        payload["email"] = email
    if name is not None:
        payload["name"] = name
    return pyjwt.encode(payload, JWT_SECRET, algorithm="HS256")


def _build_fake_assistant_service_app(shared_replay_store: InMemoryReplayStore) -> FastAPI:
    """A minimal FastAPI app that plays the role of assistant-service.

    Runs the exact same ``GatewaySecretAuthMiddleware`` and
    ``get_user_context`` code assistant-service runs, then echoes the
    resolved identity + the raw inbound headers so gateway-side tests
    can assert both the parsed ``UserContext`` and the wire-level
    ``X-User-*`` headers.
    """
    app = FastAPI()
    gs = GatewaySecret(secret=GATEWAY_SECRET, replay_store=shared_replay_store)
    app.add_middleware(
        GatewaySecretAuthMiddleware,
        gateway_secret=gs,
        allow_anonymous=False,
    )

    @app.post("/api/v1/assistant/echo-auth")
    async def echo_auth(request: Request, user=Depends(assistant_get_user_context)):
        # Return the resolved identity AND the raw inbound identity
        # headers so the gateway-side test can assert the full
        # wire-level header set. Includes both ``x-user-*`` and
        # the lone ``x-tenant-id``.
        identity_prefixes = ("x-user-", "x-tenant-", "x-app-")
        raw_headers = {
            k.lower(): v
            for k, v in request.headers.items()
            if any(k.lower().startswith(p) for p in identity_prefixes)
        }
        return {
            "user_id": user.user_id,
            "tenant_id": user.tenant_id,
            "tier": user.tier,
            "user_type": user.user_type,
            "roles": list(user.roles),
            "raw_x_user_headers": raw_headers,
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
async def test_user_type_header_smuggling_stripped(monkeypatch) -> None:
    """Audit Finding H-3 smuggle guard — a public client sending
    ``X-User-Type: admin`` at the gateway ingress must NOT reach
    assistant-service with that value. The gateway strips every
    ``X-User-*`` header before injecting its own trusted values, so
    downstream sees the default ``"user"`` (not ``"admin"``).

    Tests the case-insensitive strip logic too: ``X-USER-TYPE`` (upper),
    ``X-User-Type`` (canonical), and ``x-user-type`` (lower) all must
    be stripped.
    """
    replay_store = InMemoryReplayStore()
    fake_as = _build_fake_assistant_service_app(replay_store)
    gateway = _build_gateway_app(monkeypatch, fake_as)

    token = _make_jwt(roles=["user"], sub="regular-user", tier="normal")

    for variant in ("X-User-Type", "X-USER-TYPE", "x-user-type"):
        with TestClient(gateway) as client:
            r = client.post(
                "/gw/echo-auth",
                headers={
                    "Authorization": f"Bearer {token}",
                    variant: "admin",  # the smuggle attempt
                },
                json={},
            )
        assert r.status_code == 200, r.text
        body = r.json()
        assert body["user_type"] == "user", (
            f"smuggle via {variant!r} bypassed strip — got {body['user_type']!r}"
        )


@pytest.mark.asyncio
async def test_app_identity_headers_from_client_are_not_forwarded(monkeypatch) -> None:
    """Public clients must not be able to forge assistant-service app identity."""
    replay_store = InMemoryReplayStore()
    fake_as = _build_fake_assistant_service_app(replay_store)
    gateway = _build_gateway_app(monkeypatch, fake_as)

    token = _make_jwt(roles=["user"], sub="regular-user", tier="normal")

    with TestClient(gateway) as client:
        r = client.post(
            "/gw/echo-auth",
            headers={
                "Authorization": f"Bearer {token}",
                "X-App-User-Id": "victim-user",
                "X-App-Tenant-Id": "victim-tenant",
            },
            json={},
        )

    assert r.status_code == 200, r.text
    raw_headers = r.json()["raw_x_user_headers"]
    assert "x-app-user-id" not in raw_headers
    assert "x-app-tenant-id" not in raw_headers


@pytest.mark.asyncio
async def test_all_seven_identity_headers_transit(monkeypatch) -> None:
    """GATE G5a-3 end-to-end — every canonical ``X-User-*`` header the
    gateway is meant to inject reaches assistant-service with the exact
    value computed by the gateway's ``UserResolver``.

    This is the wire-level complement to ``test_admin_roles_survive…`` —
    that test proves *roles* traverse, this one proves *all seven*.
    """
    replay_store = InMemoryReplayStore()
    fake_as = _build_fake_assistant_service_app(replay_store)
    gateway = _build_gateway_app(monkeypatch, fake_as)

    token = _make_jwt(
        sub="u-premium-42",
        tenant_id="tenant-acme",
        tier="premium",
        roles=["premium-analyst", "beta-tester"],
        user_type="admin",
        email="alice@acme.test",
        name="Alice Analyst",
    )

    with TestClient(gateway) as client:
        r = client.post(
            "/gw/echo-auth",
            headers={"Authorization": f"Bearer {token}"},
            json={},
        )

    assert r.status_code == 200, r.text
    body = r.json()

    # 1-5: always injected by the proxy.
    headers = body["raw_x_user_headers"]
    assert headers["x-user-id"] == "u-premium-42"
    assert headers["x-tenant-id"] == "tenant-acme"
    assert headers["x-user-tier"] == "premium"
    assert headers["x-user-type"] == "admin"
    assert headers["x-user-roles"] == "premium-analyst,beta-tester"

    # 6-7: only injected when the gateway UserContext has them. Phase
    # 5b populated UserResolver from the email/name JWT claims, so
    # this must now land on the wire.
    assert headers["x-user-email"] == "alice@acme.test", (
        f"X-User-Email missing from wire — got {headers}"
    )
    assert headers["x-user-name"] == "Alice Analyst", (
        f"X-User-Name missing from wire — got {headers}"
    )

    # Sanity: the parsed UserContext matches the key fields.
    assert body["user_id"] == "u-premium-42"
    assert body["tenant_id"] == "tenant-acme"
    assert body["tier"] == "premium"
    assert body["user_type"] == "admin"
    assert body["roles"] == ["premium-analyst", "beta-tester"]


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
