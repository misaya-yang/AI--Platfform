"""
Streaming rate limit middleware tests.

Ensures the pure ASGI streaming rate limiter enforces limits for non-streaming paths.
"""

from fastapi import FastAPI
from fastapi.testclient import TestClient

from src.core.middleware.streaming import (
    StreamingRateLimitConfig,
    StreamingRateLimitMiddleware,
)


def _build_app(config: StreamingRateLimitConfig) -> FastAPI:
    app = FastAPI()
    app.add_middleware(StreamingRateLimitMiddleware, config=config)

    @app.get("/ping")
    async def ping():
        return {"ok": True}

    return app


def test_streaming_rate_limit_blocks_after_limit():
    config = StreamingRateLimitConfig(
        enabled=True,
        global_limit=2,
        global_window=60,
        user_limit=1000,
        guest_limit=1000,
        ip_limit=1000,
        whitelist_paths=[],
    )
    app = _build_app(config)

    client = TestClient(app)
    assert client.get("/ping").status_code == 200
    assert client.get("/ping").status_code == 200

    response = client.get("/ping")
    assert response.status_code == 429
    payload = response.json()
    assert payload["error"]["dimension"] == "global"
