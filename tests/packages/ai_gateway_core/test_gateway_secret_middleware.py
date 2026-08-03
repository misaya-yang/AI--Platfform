from __future__ import annotations

import pytest
from ai_gateway_core.auth.gateway_secret import GatewaySecret
from ai_gateway_core.auth.gateway_secret_middleware import (
    GatewaySecretAuthMiddleware,
    validate_gateway_auth_configuration,
)
from fastapi import FastAPI
from fastapi.testclient import TestClient
from pydantic import BaseModel


class Payload(BaseModel):
    query: str


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


def test_gateway_secret_and_anonymous_mode_are_mutually_exclusive() -> None:
    with pytest.raises(RuntimeError, match="cannot be combined"):
        validate_gateway_auth_configuration(
            secret="configured-shared-secret",
            allow_anonymous=True,
            allow_anonymous_setting="SERVICE_APP__ALLOW_ANONYMOUS",
        )


@pytest.mark.parametrize(
    ("secret", "allow_anonymous"),
    [("configured-shared-secret", False), ("", True), ("", False)],
)
def test_gateway_auth_configuration_accepts_unambiguous_modes(
    secret: str,
    allow_anonymous: bool,
) -> None:
    validate_gateway_auth_configuration(
        secret=secret,
        allow_anonymous=allow_anonymous,
        allow_anonymous_setting="SERVICE_APP__ALLOW_ANONYMOUS",
    )


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


def test_gateway_secret_middleware_v2_verifies_and_replays_request_body() -> None:
    secret = GatewaySecret(
        secret="shared-secret-for-tests",
        version="v2",
        key_id="local",
        keys={"local": "shared-secret-for-tests"},
    )
    verifier = GatewaySecret(
        secret="shared-secret-for-tests",
        version="v2",
        key_id="local",
        keys={"local": "shared-secret-for-tests"},
    )
    app = FastAPI()
    app.add_middleware(
        GatewaySecretAuthMiddleware,
        gateway_secret=verifier,
        allow_anonymous=False,
    )

    @app.post("/protected")
    async def protected(payload: Payload):
        return {"query": payload.query}

    body = b'{"query":"hello"}'
    header = secret.sign(
        request_id="middleware-v2",
        method="POST",
        path="/protected",
        query="",
        body=body,
    )

    response = TestClient(app).post(
        "/protected",
        content=body,
        headers={
            secret.header_name: header,
            "Content-Type": "application/json",
        },
    )

    assert response.status_code == 200
    assert response.json() == {"query": "hello"}
