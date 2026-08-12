"""Fail-closed MCP client for trusted local stdio plugins."""

from __future__ import annotations

import asyncio
import contextlib
import json
import logging
import mimetypes
import os
import sys
import urllib.parse
import uuid
from pathlib import Path
from typing import Any, Final

from .client import MCP_PROTOCOL_VERSION, MCPClient, MCPError, MCPServerConfig

logger = logging.getLogger(__name__)


class MCPStdioClient(MCPClient):
    """Run an operator-trusted MCP plugin with a bounded stdio transport.

    The subprocess receives only a minimal runtime environment plus explicit
    code-owned allowlisted variables. JSON-RPC responses are correlated by id,
    so independent read-only calls may share one stdio server safely.
    """

    _BASE_ENV_NAMES: Final = (
        "PATH",
        "HOME",
        "LANG",
        "LC_ALL",
        "TMPDIR",
        "SSL_CERT_FILE",
        "SSL_CERT_DIR",
        "SYSTEMROOT",
    )

    def __init__(self, config: MCPServerConfig) -> None:
        if config.transport != "stdio":
            raise ValueError("MCP_STDIO_CONFIG_REQUIRED")
        super().__init__(config)
        self._process: asyncio.subprocess.Process | None = None
        self._reader_task: asyncio.Task[None] | None = None
        self._stderr_task: asyncio.Task[None] | None = None
        self._pending: dict[str, asyncio.Future[dict[str, Any]]] = {}
        self._write_lock = asyncio.Lock()
        self._closing = False

    def _resolve_cwd(self) -> Path:
        """Resolve cwd under the canonical plugin root or plugin data root."""

        configured_plugin_root = Path(self.config.plugin_root)
        configured_data_root = Path(self.config.plugin_data_dir)
        configured_cwd = Path(self.config.cwd)
        try:
            plugin_root = configured_plugin_root.resolve(strict=True)
            configured_data_root.mkdir(parents=True, exist_ok=True)
            data_root = configured_data_root.resolve(strict=True)
        except (OSError, RuntimeError) as exc:
            raise MCPError(
                -30,
                "MCP stdio plugin directory is unavailable",
                stable_code="MCP_STDIO_CWD_UNAVAILABLE",
            ) from exc
        if not plugin_root.is_dir() or not data_root.is_dir():
            raise MCPError(
                -30,
                "MCP stdio plugin directory is unavailable",
                stable_code="MCP_STDIO_CWD_UNAVAILABLE",
            )

        try:
            candidate = configured_cwd.resolve(strict=False)
        except (OSError, RuntimeError) as exc:
            raise MCPError(
                -30,
                "MCP stdio working directory is unavailable",
                stable_code="MCP_STDIO_CWD_UNAVAILABLE",
            ) from exc
        if candidate.is_relative_to(data_root):
            expected_root = data_root
            try:
                configured_cwd.mkdir(parents=True, exist_ok=True)
            except OSError as exc:
                raise MCPError(
                    -30,
                    "MCP stdio working directory is unavailable",
                    stable_code="MCP_STDIO_CWD_UNAVAILABLE",
                ) from exc
        elif candidate.is_relative_to(plugin_root):
            expected_root = plugin_root
        else:
            raise MCPError(
                -30,
                "MCP stdio working directory is outside the plugin boundary",
                stable_code="MCP_STDIO_CWD_OUTSIDE_PLUGIN",
            )

        try:
            cwd = configured_cwd.resolve(strict=True)
        except (OSError, RuntimeError) as exc:
            raise MCPError(
                -30,
                "MCP stdio working directory is unavailable",
                stable_code="MCP_STDIO_CWD_UNAVAILABLE",
            ) from exc
        if not cwd.is_dir():
            raise MCPError(
                -30,
                "MCP stdio working directory is unavailable",
                stable_code="MCP_STDIO_CWD_UNAVAILABLE",
            )
        if not cwd.is_relative_to(expected_root):
            raise MCPError(
                -30,
                "MCP stdio working directory is outside the plugin boundary",
                stable_code="MCP_STDIO_CWD_OUTSIDE_PLUGIN",
            )
        return cwd

    async def initialize(self) -> dict[str, Any]:
        cwd = self._resolve_cwd()

        child_env = {name: os.environ[name] for name in self._BASE_ENV_NAMES if name in os.environ}
        for name in self.config.inherited_env_names:
            if name in os.environ:
                child_env[name] = os.environ[name]
        child_env.update(self.config.process_env)

        command = self.config.command
        if self.config.platform_managed and command == "python":
            # The trusted package describes its runtime portably.  Resolve the
            # platform-managed Python process to this service's interpreter so
            # venvs and hosts that expose only ``python3`` work without adding
            # an ambient PATH dependency.  Untrusted/external commands are not
            # rewritten.
            command = sys.executable
        try:
            self._process = await asyncio.create_subprocess_exec(
                command,
                *self.config.args,
                stdin=asyncio.subprocess.PIPE,
                stdout=asyncio.subprocess.PIPE,
                stderr=asyncio.subprocess.PIPE,
                cwd=str(cwd),
                env=child_env,
                limit=self.config.response_limit_bytes + 1,
            )
        except (OSError, ValueError) as exc:
            raise MCPError(
                -30,
                "MCP stdio server could not start",
                stable_code="MCP_STDIO_START_FAILED",
            ) from exc

        self._closing = False
        self._reader_task = asyncio.create_task(self._read_stdout())
        self._stderr_task = asyncio.create_task(self._drain_stderr())
        try:
            result = await self._jsonrpc(
                "initialize",
                {
                    "protocolVersion": MCP_PROTOCOL_VERSION,
                    "capabilities": {"tools": {"listChanged": True}},
                    "clientInfo": {"name": "ai-gateway", "version": "1.0.0"},
                },
            )
            if result.get("protocolVersion") != MCP_PROTOCOL_VERSION:
                raise MCPError(
                    -16,
                    "MCP protocol version is unsupported",
                    stable_code="MCP_PROTOCOL_VERSION_MISMATCH",
                )
            self._server_info = dict(result.get("serverInfo") or {})
            await self._notify("notifications/initialized")
            self._initialized = True
            logger.info("MCP stdio server initialized: %s", self.config.name)
            return result
        except Exception:
            await self.close()
            raise

    async def _read_stdout(self) -> None:
        process = self._process
        if process is None or process.stdout is None:
            return
        failure: MCPError | None = None
        try:
            while True:
                line = await process.stdout.readline()
                if not line:
                    break
                if len(line) > self.config.response_limit_bytes:
                    failure = MCPError(
                        -19,
                        "MCP response exceeded the configured limit",
                        stable_code="MCP_RESPONSE_TOO_LARGE",
                    )
                    break
                try:
                    payload = json.loads(line)
                except (json.JSONDecodeError, UnicodeDecodeError):
                    failure = MCPError(
                        -18,
                        "MCP response is invalid",
                        stable_code="MCP_RESPONSE_INVALID",
                    )
                    break
                if not isinstance(payload, dict) or payload.get("jsonrpc") != "2.0":
                    failure = MCPError(
                        -18,
                        "MCP response is invalid",
                        stable_code="MCP_RESPONSE_INVALID",
                    )
                    break
                request_id = payload.get("id")
                if request_id is None:
                    continue
                future = self._pending.pop(str(request_id), None)
                if future is not None and not future.done():
                    future.set_result(payload)
        except (ValueError, OSError) as exc:
            failure = MCPError(
                -3,
                "MCP stdio server is unavailable",
                stable_code="MCP_UPSTREAM_UNAVAILABLE",
            )
            failure.__cause__ = exc
        finally:
            if not self._closing:
                failure = failure or MCPError(
                    -3,
                    "MCP stdio server is unavailable",
                    stable_code="MCP_UPSTREAM_UNAVAILABLE",
                )
            if failure is not None:
                for future in tuple(self._pending.values()):
                    if not future.done():
                        future.set_exception(failure)
                self._pending.clear()

    async def _drain_stderr(self) -> None:
        process = self._process
        if process is None or process.stderr is None:
            return
        # Do not echo plugin stderr: it may contain user content or provider
        # diagnostics. Draining prevents a noisy child from blocking.
        while await process.stderr.readline():
            pass

    async def _write_payload(self, payload: dict[str, Any]) -> None:
        process = self._process
        if process is None or process.stdin is None or process.returncode is not None:
            raise MCPError(
                -1,
                "MCP stdio client is not connected",
                stable_code="MCP_NOT_CONNECTED",
            )
        encoded = (json.dumps(payload, separators=(",", ":")) + "\n").encode("utf-8")
        if len(encoded) > self.config.response_limit_bytes:
            raise MCPError(
                -19,
                "MCP request exceeded the configured limit",
                stable_code="MCP_REQUEST_TOO_LARGE",
            )
        try:
            async with self._write_lock:
                process.stdin.write(encoded)
                await process.stdin.drain()
        except (BrokenPipeError, ConnectionError, OSError) as exc:
            raise MCPError(
                -3,
                "MCP stdio server is unavailable",
                stable_code="MCP_UPSTREAM_UNAVAILABLE",
            ) from exc

    async def _jsonrpc(self, method: str, params: dict[str, Any]) -> dict[str, Any]:
        request_id = str(uuid.uuid4())
        future: asyncio.Future[dict[str, Any]] = asyncio.get_running_loop().create_future()
        self._pending[request_id] = future
        try:
            await self._write_payload(
                {
                    "jsonrpc": "2.0",
                    "id": request_id,
                    "method": method,
                    "params": params,
                }
            )
            payload = await asyncio.wait_for(
                asyncio.shield(future),
                timeout=self.config.timeout,
            )
        except TimeoutError as exc:
            raise MCPError(
                -2,
                "MCP stdio request timed out",
                stable_code="MCP_TIMEOUT",
            ) from exc
        finally:
            self._pending.pop(request_id, None)
        if str(payload.get("id")) != request_id:
            raise MCPError(
                -23,
                "MCP response identity does not match the request",
                stable_code="MCP_RESPONSE_ID_MISMATCH",
            )
        if "error" in payload:
            raise MCPError(
                -24,
                "MCP operation failed",
                stable_code="MCP_REMOTE_ERROR",
            )
        value = payload.get("result") or {}
        if not isinstance(value, dict):
            raise MCPError(
                -18,
                "MCP response is invalid",
                stable_code="MCP_RESPONSE_INVALID",
            )
        return value

    async def _notify(self, method: str, params: dict[str, Any] | None = None) -> None:
        payload: dict[str, Any] = {"jsonrpc": "2.0", "method": method}
        if params:
            payload["params"] = params
        await self._write_payload(payload)

    async def download_resource_link(self, resource_url: str) -> tuple[bytes, str]:
        try:
            parsed = urllib.parse.urlsplit(resource_url)
        except ValueError as exc:
            raise MCPError(
                -26,
                "MCP resource URL is invalid",
                stable_code="MCP_RESOURCE_URL_INVALID",
            ) from exc
        if parsed.scheme.lower() != "file" or parsed.hostname not in {None, "", "localhost"}:
            raise MCPError(
                -27,
                "MCP resource origin is not permitted",
                stable_code="MCP_RESOURCE_ORIGIN_MISMATCH",
            )
        root = (Path(self.config.plugin_data_dir) / "artifacts").resolve()
        try:
            target = Path(urllib.parse.unquote(parsed.path)).resolve(strict=True)
        except OSError as exc:
            raise MCPError(
                -28,
                "MCP resource was unavailable",
                stable_code="MCP_RESOURCE_UNAVAILABLE",
            ) from exc
        if not target.is_relative_to(root) or not target.is_file():
            raise MCPError(
                -27,
                "MCP resource path is not permitted",
                stable_code="MCP_RESOURCE_PATH_BLOCKED",
            )
        size = target.stat().st_size
        if size > self.config.response_limit_bytes:
            raise MCPError(
                -19,
                "MCP response exceeded the configured limit",
                stable_code="MCP_RESPONSE_TOO_LARGE",
            )
        body = await asyncio.to_thread(target.read_bytes)
        content_type = mimetypes.guess_type(target.name)[0] or "application/octet-stream"
        return body, content_type

    async def close(self) -> None:
        self._closing = True
        process = self._process
        if process is not None:
            if process.stdin is not None:
                process.stdin.close()
            if process.returncode is None:
                process.terminate()
                try:
                    await asyncio.wait_for(process.wait(), timeout=5)
                except TimeoutError:
                    process.kill()
                    await process.wait()
        for task in (self._reader_task, self._stderr_task):
            if task is not None and not task.done():
                task.cancel()
                with contextlib.suppress(asyncio.CancelledError):
                    await task
        for future in tuple(self._pending.values()):
            if not future.done():
                future.cancel()
        self._pending.clear()
        self._process = None
        self._reader_task = None
        self._stderr_task = None
        self._initialized = False


__all__ = ["MCPStdioClient"]
