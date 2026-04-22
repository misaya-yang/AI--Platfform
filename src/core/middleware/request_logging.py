"""
HTTP 请求日志中间件

记录所有请求用于审计和调试：
- 请求基本信息（时间、方法、路径）
- 用户信息（脱敏）
- 响应状态和耗时
- 异常信息（如果有）
"""

from __future__ import annotations

import asyncio
import json
import time
import uuid
from collections.abc import Callable
from dataclasses import asdict, dataclass, field
from datetime import datetime, timezone
from typing import Any

from fastapi import Request
from starlette.middleware.base import BaseHTTPMiddleware, RequestResponseEndpoint
from starlette.responses import Response

from ai_gateway_core.logging import get_logger

logger = get_logger(__name__)


@dataclass
class RequestLogConfig:
    """请求日志配置"""

    enabled: bool = True
    log_request_body: bool = False  # 是否记录请求体（可能含敏感数据）
    log_response_body: bool = False  # 是否记录响应体
    max_body_length: int = 1000  # 记录的最大 body 长度
    exclude_paths: list[str] = field(
        default_factory=lambda: [
            "/health",
            "/health/live",
            "/health/ready",
            "/metrics",
        ]
    )
    sensitive_headers: list[str] = field(
        default_factory=lambda: [
            "authorization",
            "x-api-key",
            "cookie",
        ]
    )


@dataclass
class RequestLog:
    """请求日志数据"""

    request_id: str
    timestamp: str
    method: str
    path: str
    query_string: str = ""
    user_id: str = "anonymous"
    user_type: str = "unknown"
    client_ip: str = ""
    user_agent: str = ""
    status_code: int = 0
    duration_ms: float = 0.0
    error: str | None = None
    error_type: str | None = None
    request_size: int = 0
    response_size: int = 0
    metadata: dict[str, Any] = field(default_factory=dict)

    def to_dict(self) -> dict[str, Any]:
        return asdict(self)


class RequestLoggingMiddleware(BaseHTTPMiddleware):
    """
    HTTP 请求日志中间件

    记录：
    1. 请求基本信息（时间、方法、路径）
    2. 用户信息（脱敏）
    3. 响应状态和耗时
    4. 异常信息（如果有）
    """

    def __init__(
        self,
        app,
        config: RequestLogConfig,
        log_writer: Callable | None = None,
    ):
        super().__init__(app)
        self.config = config
        self.log_writer = log_writer or self._default_log_writer

    async def dispatch(
        self,
        request: Request,
        call_next: RequestResponseEndpoint,
    ) -> Response:
        """处理请求日志"""
        if not self.config.enabled:
            return await call_next(request)

        # 检查排除路径
        if self._is_excluded(request.url.path):
            return await call_next(request)

        # 生成请求 ID
        request_id = str(uuid.uuid4())
        request.state.request_id = request_id

        # 记录开始时间
        start_time = time.time()

        # 提取请求信息
        log_data = RequestLog(
            request_id=request_id,
            timestamp=datetime.now(timezone.utc).isoformat(),
            method=request.method,
            path=request.url.path,
            query_string=str(request.url.query) if request.url.query else "",
            client_ip=self._get_client_ip(request),
            user_agent=request.headers.get("user-agent", ""),
            request_size=int(request.headers.get("content-length", 0)),
        )

        # 提取用户信息
        user_info = getattr(getattr(request, "state", None), "user_info", None)
        if user_info:
            log_data.user_id = self._mask_user_id(user_info.user_id)
            log_data.user_type = user_info.user_type

        try:
            # 执行请求
            response = await call_next(request)

            # 记录响应信息
            log_data.status_code = response.status_code
            log_data.response_size = int(response.headers.get("content-length", 0))
            log_data.duration_ms = (time.time() - start_time) * 1000

            # 添加请求 ID 到响应头
            response.headers["X-Request-Id"] = request_id

            # 异步写入日志
            asyncio.create_task(self._write_log(log_data))

            return response

        except Exception as e:
            # 记录异常信息
            log_data.status_code = 500
            log_data.error = str(e)
            log_data.error_type = type(e).__name__
            log_data.duration_ms = (time.time() - start_time) * 1000

            # 异步写入日志
            asyncio.create_task(self._write_log(log_data))

            raise

    def _is_excluded(self, path: str) -> bool:
        """检查路径是否被排除"""
        for exclude_path in self.config.exclude_paths:
            if path == exclude_path or path.startswith(exclude_path + "/"):
                return True
        return False

    def _get_client_ip(self, request: Request) -> str:
        """获取客户端 IP"""
        forwarded = request.headers.get("X-Forwarded-For")
        if forwarded:
            return forwarded.split(",")[0].strip()

        real_ip = request.headers.get("X-Real-IP")
        if real_ip:
            return real_ip

        if request.client:
            return request.client.host

        return "unknown"

    def _mask_user_id(self, user_id: str) -> str:
        """脱敏用户 ID"""
        if not user_id:
            return "anonymous"

        # 保留前 4 位和后 4 位，中间用 *** 替代
        if len(user_id) <= 8:
            return user_id[:2] + "***" + user_id[-2:]

        return user_id[:4] + "***" + user_id[-4:]

    async def _write_log(self, log_data: RequestLog) -> None:
        """写入日志"""
        await self.log_writer(log_data)

    async def _default_log_writer(self, log_data: RequestLog) -> None:
        """默认日志写入器"""
        # 根据状态码选择日志级别
        if log_data.status_code >= 500:
            logger.error(
                f"[{log_data.request_id}] {log_data.method} {log_data.path} "
                f"- {log_data.status_code} ({log_data.duration_ms:.2f}ms) "
                f"- Error: {log_data.error}",
                extra=log_data.to_dict(),
            )
        elif log_data.status_code >= 400:
            logger.warning(
                f"[{log_data.request_id}] {log_data.method} {log_data.path} "
                f"- {log_data.status_code} ({log_data.duration_ms:.2f}ms)",
                extra=log_data.to_dict(),
            )
        else:
            logger.info(
                f"[{log_data.request_id}] {log_data.method} {log_data.path} "
                f"- {log_data.status_code} ({log_data.duration_ms:.2f}ms)",
                extra=log_data.to_dict(),
            )


# ============ 日志存储后端 ============


class RedisLogWriter:
    """Redis 日志写入器"""

    def __init__(
        self,
        redis_client,
        key_prefix: str = "request_log",
        ttl: int = 86400 * 7,  # 7 天
    ):
        self.redis = redis_client
        self.key_prefix = key_prefix
        self.ttl = ttl

    async def __call__(self, log_data: RequestLog) -> None:
        """写入日志到 Redis"""
        try:
            key = f"{self.key_prefix}:{log_data.request_id}"
            await self.redis.setex(key, self.ttl, json.dumps(log_data.to_dict()))

            # 添加到时间序列索引
            timestamp = int(time.time())
            await self.redis.zadd(
                f"{self.key_prefix}:timeline",
                {log_data.request_id: timestamp},
            )

            # 按用户 ID 索引
            if log_data.user_id != "anonymous":
                await self.redis.lpush(
                    f"{self.key_prefix}:user:{log_data.user_id}",
                    log_data.request_id,
                )
                await self.redis.ltrim(
                    f"{self.key_prefix}:user:{log_data.user_id}",
                    0,
                    999,  # 保留最近 1000 条
                )
        except Exception as e:
            logger.warning(f"Failed to write log to Redis: {e}")


class DatabaseLogWriter:
    """数据库日志写入器"""

    def __init__(self, database):
        self.db = database

    async def __call__(self, log_data: RequestLog) -> None:
        """写入日志到数据库"""
        try:
            await self.db.execute(
                """
                INSERT INTO request_logs (
                    request_id, timestamp, method, path, query_string,
                    user_id, user_type, client_ip, user_agent,
                    status_code, duration_ms, error, error_type,
                    request_size, response_size, metadata
                ) VALUES (
                    $1, $2, $3, $4, $5, $6, $7, $8, $9,
                    $10, $11, $12, $13, $14, $15, $16
                )
                """,
                log_data.request_id,
                log_data.timestamp,
                log_data.method,
                log_data.path,
                log_data.query_string,
                log_data.user_id,
                log_data.user_type,
                log_data.client_ip,
                log_data.user_agent,
                log_data.status_code,
                log_data.duration_ms,
                log_data.error,
                log_data.error_type,
                log_data.request_size,
                log_data.response_size,
                json.dumps(log_data.metadata),
            )
        except Exception as e:
            logger.warning(f"Failed to write log to database: {e}")
