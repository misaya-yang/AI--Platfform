"""Request logging and metrics recording for the pure ASGI stack."""

from __future__ import annotations

import asyncio
import contextlib
import time
import uuid
from dataclasses import dataclass, field

from ai_gateway_core.logging import get_logger
from starlette.types import ASGIApp, Message, Receive, Scope, Send

from ....services.metrics import get_metrics_recorder
from .base import PureASGIMiddleware
from .paths import is_streaming_path

logger = get_logger("src.core.middleware.streaming")


@dataclass
class StreamingLogConfig:
    """流式友好的日志配置"""

    enabled: bool = True
    log_request_body: bool = False
    log_response_body: bool = False
    exclude_paths: list[str] = field(
        default_factory=lambda: ["/health", "/health/live", "/health/ready"]
    )


class StreamingLoggingMiddleware(PureASGIMiddleware):
    """
    流式友好的请求日志中间件

    对于流式路径，仅记录请求开始，不等待响应完成。
    对于非流式路径，记录完整的请求/响应。
    """

    def __init__(self, app: ASGIApp, config: StreamingLogConfig):
        super().__init__(app)
        self.config = config

    @staticmethod
    def _schedule_metrics_record(
        *,
        method: str,
        path: str,
        status_code: int,
        duration_ms: float,
        user_id: str,
        service_id: str,
        error_label: str,
    ) -> None:
        """Best-effort, non-blocking metrics recording."""
        metrics_recorder = get_metrics_recorder()
        try:
            task = asyncio.create_task(
                metrics_recorder.record_request(
                    method=method,
                    path=path,
                    status_code=status_code,
                    duration_ms=duration_ms,
                    user_id=user_id,
                    service_id=service_id,
                )
            )
        except RuntimeError as exc:
            logger.debug("%s: %s", error_label, exc)
            return

        def _done_callback(done: asyncio.Task) -> None:
            with contextlib.suppress(asyncio.CancelledError):
                exc = done.exception()
                if exc is not None:
                    logger.debug("%s: %s", error_label, exc)

        task.add_done_callback(_done_callback)

    async def __call__(self, scope: Scope, receive: Receive, send: Send) -> None:
        if scope["type"] != "http":
            await self.app(scope, receive, send)
            return

        if not self.config.enabled:
            await self.app(scope, receive, send)
            return

        path = scope.get("path", "")
        if self._is_excluded(path):
            await self.app(scope, receive, send)
            return

        # 生成 / 透传请求 ID — preserve incoming X-Request-Id for end-to-end
        # correlation (caller may already have one). Same validation rules as
        # request_logging.py: ≤64 chars, alphanumeric + safe punct.
        incoming_request_id = ""
        for header_name, header_value in scope.get("headers", []):
            if header_name == b"x-request-id":
                try:
                    incoming_request_id = header_value.decode("ascii", errors="ignore").strip()
                except Exception:
                    incoming_request_id = ""
                break
        if (
            incoming_request_id
            and len(incoming_request_id) <= 64
            and all(c.isalnum() or c in "-_." for c in incoming_request_id)
        ):
            request_id = incoming_request_id
        else:
            request_id = str(uuid.uuid4())
        if "state" not in scope:
            scope["state"] = {}
        scope["state"]["request_id"] = request_id

        start_time = time.time()
        method = scope.get("method", "")

        # 对于流式路径，仅记录请求开始
        if is_streaming_path(path):
            logger.info(f"[{request_id}] {method} {path} (streaming)")

            # 包装 send 添加请求 ID 头
            async def streaming_send(message: Message) -> None:
                if message["type"] == "http.response.start":
                    headers = list(message.get("headers", []))
                    headers.append((b"x-request-id", request_id.encode()))
                    message = {**message, "headers": headers}
                await send(message)

            await self.app(scope, receive, streaming_send)

            duration = (time.time() - start_time) * 1000
            logger.info(f"[{request_id}] {method} {path} streaming completed ({duration:.2f}ms)")

            # Record metrics for streaming requests
            user_info = scope.get("state", {}).get("user_info", {})
            self._schedule_metrics_record(
                method=method,
                path=path,
                status_code=200,  # Streaming requests typically succeed if they complete
                duration_ms=duration,
                user_id=user_info.get("user_id", ""),
                service_id=scope.get("state", {}).get("service_id", ""),
                error_label="Failed to record streaming metrics",
            )

            return

        # 非流式路径记录完整请求/响应
        status_code = 0

        async def logging_send(message: Message) -> None:
            nonlocal status_code
            if message["type"] == "http.response.start":
                status_code = message.get("status", 0)
                headers = list(message.get("headers", []))
                headers.append((b"x-request-id", request_id.encode()))
                message = {**message, "headers": headers}
            await send(message)

        try:
            await self.app(scope, receive, logging_send)
            duration = (time.time() - start_time) * 1000
            logger.info(f"[{request_id}] {method} {path} -> {status_code} ({duration:.2f}ms)")

            # Record metrics for dashboard
            user_info = scope.get("state", {}).get("user_info", {})
            self._schedule_metrics_record(
                method=method,
                path=path,
                status_code=status_code,
                duration_ms=duration,
                user_id=user_info.get("user_id", ""),
                service_id=scope.get("state", {}).get("service_id", ""),
                error_label="Failed to record metrics",
            )

        except Exception as e:
            duration = (time.time() - start_time) * 1000
            logger.error(f"[{request_id}] {method} {path} -> ERROR ({duration:.2f}ms): {e}")

            # Record error metrics
            user_info = scope.get("state", {}).get("user_info", {})
            self._schedule_metrics_record(
                method=method,
                path=path,
                status_code=500,
                duration_ms=duration,
                user_id=user_info.get("user_id", ""),
                service_id=scope.get("state", {}).get("service_id", ""),
                error_label="Failed to record error metrics",
            )

            raise

    def _is_excluded(self, path: str) -> bool:
        """检查路径是否被排除"""
        return any(path == ep or path.startswith(ep + "/") for ep in self.config.exclude_paths)
