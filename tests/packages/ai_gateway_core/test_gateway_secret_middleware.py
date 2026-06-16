from __future__ import annotations

from ai_gateway_core.auth.gateway_secret import GatewaySecret
from ai_gateway_core.auth.gateway_secret_middleware import GatewaySecretAuthMiddleware
from fastapi import FastAPI
from fastapi.testclient import TestClient


def _app(*, allow_anonymous: bool = False) -> tuple[FastAPI, GatewaySecret]:
    secret = GatewaySecret(secret="shared-secret-for-tests")
    app = FastAPI()
    app.add_middleware(
        GatewaySecretAuthMiddleware,
        gateway_secret=secret,
        allow_anonymous=allow_anonymous,
    )

    @app.get("/health")
    async def health():
        return {"status": "ok"}

    @app.get("/protected")
    async def protected():
        return {"status": "accepted"}

    return app, secret


def test_gateway_secret_middleware_allows_health_without_signature() -> None:
    app, _secret = _app()

    response = TestClient(app).get("/health")

    assert response.status_code == 200
    assert response.json() == {"status": "ok"}


def test_gateway_secret_middleware_rejects_missing_signature() -> None:
    app, _secret = _app()

    response = TestClient(app).get("/protected")

    assert response.status_code == 401
    assert response.json() == {
        "code": "AUTH_DENIED",
        "message": "Missing or invalid X-Gateway-Secret",
    }


def test_gateway_secret_middleware_accepts_valid_signature() -> None:
    app, secret = _app()

    response = TestClient(app).get(
        "/protected",
        headers={secret.header_name: secret.sign()},
    )

    assert response.status_code == 200
    assert response.json() == {"status": "accepted"}


def test_gateway_secret_middleware_allows_unsigned_dev_when_enabled() -> None:
    app, _secret = _app(allow_anonymous=True)

    response = TestClient(app).get("/protected")

    assert response.status_code == 200
    assert response.json() == {"status": "accepted"}
