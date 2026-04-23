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


def test_gateway_boot_imports_resolve() -> None:
    """create_app() must complete without ImportError.

    Exercises every router-level import chain:
      src.main
        → src.api.router
            → src.api.v1.{assistant,quiz,exams,skills,mcp,connectors,
                          models,health,tool_inventory,conversation_shares}
                → assistant_service.core.*   (gateway→assistant bridge)
                → ai_gateway_core.*          (shared primitives)
    """
    from src.main import create_app

    app = create_app()
    assert app is not None
    assert len(app.routes) > 0, "create_app() produced a FastAPI app with zero routes"
