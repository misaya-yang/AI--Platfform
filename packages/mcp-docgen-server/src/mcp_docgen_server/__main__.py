"""``python -m mcp_docgen_server`` entry point.

Dispatches to the selected transport based on ``MCP_TRANSPORT``:
  - unset / "stdio"  → main_stdio (default, used by the smoke test)
  - "sse"            → main_sse (Starlette + uvicorn)

Exit code 2 on an unknown value so supervisors (Docker, systemd) can
distinguish misconfiguration from a crash.
"""

from __future__ import annotations

import asyncio
import logging
import os
import sys

from .server import main_sse, main_stdio


def _main() -> int:
    logging.basicConfig(
        level=os.environ.get("LOG_LEVEL", "INFO"),
        format="%(asctime)s %(levelname)s %(name)s %(message)s",
    )
    transport = os.environ.get("MCP_TRANSPORT", "stdio").strip().lower()
    if transport == "stdio":
        asyncio.run(main_stdio())
        return 0
    if transport == "sse":
        asyncio.run(main_sse())
        return 0
    print(f"unknown MCP_TRANSPORT: {transport!r}", file=sys.stderr)
    return 2


def main() -> int:
    """Public alias used by the setuptools ``mcp-docgen-server`` console script."""
    return _main()


if __name__ == "__main__":
    sys.exit(_main())
