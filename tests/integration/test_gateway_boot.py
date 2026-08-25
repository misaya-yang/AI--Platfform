"""Gateway import-time boot test.

Closes the Phase-4-discovered blind spot: pytest collection never
imports ``src/api/v1/*.py`` or ``src/main.py``, so ImportErrors that
surface only at service startup went undetected until prod-deploy.
This test constructs the FastAPI app via ``create_app()``, exercising
every ``src/api/v1/*.py`` router-level import in one go.

If this test fails, the gateway process cannot boot — regardless of
unit-test pass rate. Treat it as production-critical.
"""

from __future__ import annotations

from types import SimpleNamespace

import pytest


def test_gateway_boot_imports_resolve() -> None:
    """create_app() must complete without ImportError.

    Exercises every router-level import chain:
      src.main
        → src.api.router
            → src.api.v1.{assistant,quiz,skills,mcp,connectors,
                          models,health,tool_inventory,conversation_shares}
                → ai_gateway_core.*          (shared primitives)
    """
    from src.main import create_app

    app = create_app()
    assert app is not None
    assert len(app.routes) > 0, "create_app() produced a FastAPI app with zero routes"


@pytest.mark.asyncio
async def test_readiness_probes_runtime_ready_endpoint(monkeypatch: pytest.MonkeyPatch) -> None:
    """The Rust Runtime has no /health endpoint; probe its readiness contract."""
    from src import main

    requested: list[str] = []

    class _Client:
        async def __aenter__(self):
            return self

        async def __aexit__(self, *_args):
            return None

        async def get(self, url: str):
            requested.append(url)
            return SimpleNamespace(status_code=200)

    monkeypatch.setattr(main.httpx, "AsyncClient", lambda **_kwargs: _Client())
    checks: dict[str, str] = {}

    assert await main._probe_http_service(
        "agent_runtime",
        "http://agent-runtime:8094",
        checks,
        path="/health/ready",
        required=True,
    )
    assert await main._probe_http_service(
        "knowledge_service",
        "http://knowledge-service:8092",
        checks,
    )
    assert requested == [
        "http://agent-runtime:8094/health/ready",
        "http://knowledge-service:8092/health",
    ]
    assert checks == {"agent_runtime": "healthy", "knowledge_service": "healthy"}
