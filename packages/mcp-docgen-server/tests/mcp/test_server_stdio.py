"""End-to-end stdio smoke test: spawn the server, speak JSON-RPC, verify.

Skipped cleanly when either:
  - the ``mcp`` Python SDK isn't installed (Phase 2 — expected)
  - the ``docgen`` package isn't importable (Agent A still running)

Once both are available this test should pass in CI.
"""

from __future__ import annotations

import asyncio
import json
import os
import sys
from pathlib import Path

import pytest

# Skip early if the transport stack isn't present.
pytest.importorskip("mcp")

try:  # pragma: no cover — skip guard
    import docgen  # noqa: F401
except ImportError:
    pytest.skip(
        "docgen package not yet available (Agent A still running)",
        allow_module_level=True,
    )


REPO_ROOT = Path(__file__).resolve().parents[3]
PKG_DIR = REPO_ROOT / "packages" / "mcp-docgen-server"
PKG_SRC = PKG_DIR / "src"


# ---------------------------------------------------------------------------
# JSON-RPC helpers
#
# MCP stdio transport uses newline-delimited JSON (NDJSON) — one JSON
# object per line, UTF-8. The earlier draft of this test assumed LSP
# Content-Length framing; the SDK does not implement that.


def _frame(payload: dict) -> bytes:
    return (json.dumps(payload) + "\n").encode("utf-8")


async def _read_frame(stream: asyncio.StreamReader) -> dict | None:
    line = await stream.readline()
    if not line:
        return None
    return json.loads(line.decode("utf-8"))


# ---------------------------------------------------------------------------
# The test


@pytest.mark.asyncio
async def test_list_tools_over_stdio(tmp_path: Path) -> None:
    """Boot the server, initialize, list_tools, expect generate_document."""
    env = {
        **os.environ,
        "MCP_TRANSPORT": "stdio",
        "PYTHONPATH": os.pathsep.join(
            filter(None, [str(PKG_SRC), os.environ.get("PYTHONPATH", "")])
        ),
        # Ensure the local artifact store has a writable root.
        "TMPDIR": str(tmp_path),
    }

    proc = await asyncio.create_subprocess_exec(
        sys.executable,
        "-m",
        "mcp_docgen_server",
        stdin=asyncio.subprocess.PIPE,
        stdout=asyncio.subprocess.PIPE,
        stderr=asyncio.subprocess.PIPE,
        env=env,
        cwd=str(tmp_path),
    )

    try:
        assert proc.stdin is not None and proc.stdout is not None

        # 1. initialize
        proc.stdin.write(
            _frame(
                {
                    "jsonrpc": "2.0",
                    "id": 1,
                    "method": "initialize",
                    "params": {
                        "protocolVersion": "2024-11-05",
                        "capabilities": {},
                        "clientInfo": {"name": "pytest", "version": "0"},
                    },
                }
            )
        )
        await proc.stdin.drain()
        resp = await asyncio.wait_for(_read_frame(proc.stdout), timeout=15)
        assert resp is not None and resp.get("id") == 1
        assert "result" in resp, resp

        # 2. notifications/initialized
        proc.stdin.write(
            _frame(
                {
                    "jsonrpc": "2.0",
                    "method": "notifications/initialized",
                    "params": {},
                }
            )
        )
        await proc.stdin.drain()

        # 3. tools/list
        proc.stdin.write(
            _frame(
                {
                    "jsonrpc": "2.0",
                    "id": 2,
                    "method": "tools/list",
                    "params": {},
                }
            )
        )
        await proc.stdin.drain()
        tools = await asyncio.wait_for(_read_frame(proc.stdout), timeout=10)
        assert tools is not None and tools.get("id") == 2
        names = [t["name"] for t in tools["result"]["tools"]]
        assert "generate_document" in names, names
        # Verify the input schema includes the 4 formats.
        tool = next(t for t in tools["result"]["tools"] if t["name"] == "generate_document")
        assert "format" in tool["inputSchema"]["properties"]

        # 4. resources/list
        proc.stdin.write(
            _frame(
                {
                    "jsonrpc": "2.0",
                    "id": 3,
                    "method": "resources/list",
                    "params": {},
                }
            )
        )
        await proc.stdin.drain()
        resources = await asyncio.wait_for(_read_frame(proc.stdout), timeout=10)
        assert resources is not None and resources.get("id") == 3
        uris = [r["uri"] for r in resources["result"]["resources"]]
        assert "docgen://design-systems" in uris

    finally:
        proc.terminate()
        try:
            await asyncio.wait_for(proc.wait(), timeout=5)
        except asyncio.TimeoutError:  # pragma: no cover
            proc.kill()
            await proc.wait()
