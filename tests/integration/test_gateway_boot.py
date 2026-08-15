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

import subprocess
import sys
import textwrap
from pathlib import Path

ROOT = Path(__file__).resolve().parents[2]


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


def test_gateway_boot_does_not_require_assistant_service() -> None:
    """Model the production image, where assistant_service is not installed."""
    script = textwrap.dedent(
        """
        import builtins

        real_import = builtins.__import__

        def without_assistant(name, *args, **kwargs):
            if name == "assistant_service" or name.startswith("assistant_service."):
                raise ModuleNotFoundError("assistant_service is excluded from gateway image")
            return real_import(name, *args, **kwargs)

        builtins.__import__ = without_assistant

        from src.main import create_app

        app = create_app()
        assert app.routes
        """
    )
    result = subprocess.run(
        [sys.executable, "-c", script],
        cwd=ROOT,
        capture_output=True,
        text=True,
        check=False,
    )
    assert result.returncode == 0, result.stderr
