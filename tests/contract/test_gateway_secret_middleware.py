"""In-process verification of ``GatewaySecretAuthMiddleware``.

This is the assistant-service side of GATE G5a-5. The runtime docker
check in ``plans/verify-phase-5a.sh`` exercises the same middleware
over a real compose network — this test locks the middleware's
rejection logic so CI catches regressions without Docker.
"""
from __future__ import annotations

from ai_gateway_core.auth.gateway_secret import (
    GatewaySecret,
    InMemoryReplayStore,
)
from fastapi import FastAPI
from fastapi.testclient import TestClient

SECRET = "0123456789abcdef0123456789abcdef"


def _build_app(*, allow_anonymous: bool) -> FastAPI:
    from assistant_service.auth import GatewaySecretAuthMiddleware

    app = FastAPI()
    # Build middleware with a shared replay store so the in-test
    # verifier and signer see the same "seen ids" set.
    gs = GatewaySecret(secret=SECRET, replay_store=InMemoryReplayStore())
    app.add_middleware(
        GatewaySecretAuthMiddleware,
        gateway_secret=gs,
        allow_anonymous=allow_anonymous,
    )

    @app.get("/health")
    def health() -> dict:
        return {"ok": True}

    @app.get("/api/v1/assistant/ping")
    def ping() -> dict:
        return {"pong": True}

    return app


def test_health_always_allowed() -> None:
    app = _build_app(allow_anonymous=False)
    client = TestClient(app)
    # No header at all — health still passes
    r = client.get("/health")
    assert r.status_code == 200


def test_missing_header_rejected_when_anonymous_off() -> None:
    app = _build_app(allow_anonymous=False)
    client = TestClient(app)
    r = client.get("/api/v1/assistant/ping")
    assert r.status_code == 401
    assert r.json()["code"] == "AUTH_DENIED"


def test_missing_header_allowed_when_anonymous_on() -> None:
    app = _build_app(allow_anonymous=True)
    client = TestClient(app)
    r = client.get("/api/v1/assistant/ping")
    assert r.status_code == 200


def test_invalid_signature_rejected() -> None:
    app = _build_app(allow_anonymous=False)
    client = TestClient(app)
    r = client.get(
        "/api/v1/assistant/ping",
        headers={"X-Gateway-Secret": "v1:rid:1234567890123:badsig"},
    )
    assert r.status_code == 401


def test_valid_signature_accepted() -> None:
    app = _build_app(allow_anonymous=False)
    # Sign with the same secret the middleware uses.
    signer = GatewaySecret(secret=SECRET, replay_store=InMemoryReplayStore())
    client = TestClient(app)
    r = client.get(
        "/api/v1/assistant/ping",
        headers={"X-Gateway-Secret": signer.sign()},
    )
    assert r.status_code == 200


def test_malformed_header_rejected() -> None:
    app = _build_app(allow_anonymous=False)
    client = TestClient(app)
    r = client.get(
        "/api/v1/assistant/ping",
        headers={"X-Gateway-Secret": "garbage"},
    )
    assert r.status_code == 401
