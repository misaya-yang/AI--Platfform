"""Rate limiting for the pure ASGI middleware stack."""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any

from starlette.responses import JSONResponse
from starlette.types import ASGIApp, Message, Receive, Scope, Send

from ...client_ip import get_client_ip_from_scope
from ..rate_limit_http import RateLimitInfo, SlidingWindowRateLimiter
from .base import PureASGIMiddleware


@dataclass
class StreamingRateLimitConfig:
    """流式友好的限流配置"""

    enabled: bool = True
    global_limit: int = 1000
    global_window: int = 60
    user_limit: int = 30
    user_window: int = 60
    guest_limit: int = 10
    guest_window: int = 60
    ip_limit: int = 60
    ip_window: int = 60
    whitelist_paths: list[str] = field(
        default_factory=lambda: ["/health", "/health/live", "/health/ready"]
    )


@dataclass
class StreamingAdmissionConfig:
    """Active-stream admission settings (config surface for deploy guards).

    Full admission middleware is optional; this dataclass documents the
    expected whitelist and is used by release security regressions.
    """

    enabled: bool = True
    whitelist_paths: list[str] = field(
        default_factory=lambda: ["/health", "/health/live", "/health/ready"]
    )


class StreamingRateLimitMiddleware(PureASGIMiddleware):
    """
    流式友好的限流中间件

    对于流式路径，跳过限流检查（或使用异步检查）。
    对于非流式路径，执行完整的限流检查。
    """

    def __init__(self, app: ASGIApp, config: StreamingRateLimitConfig):
        super().__init__(app)
        self.config = config
        # Use sliding window limiter with in-memory fallback.
        self.limiter = SlidingWindowRateLimiter(redis_client=None)

    async def __call__(self, scope: Scope, receive: Receive, send: Send) -> None:
        if scope["type"] != "http":
            await self.app(scope, receive, send)
            return

        if not self.config.enabled:
            await self.app(scope, receive, send)
            return

        path = scope.get("path", "")
        if self._is_whitelisted(path):
            await self.app(scope, receive, send)
            return

        # Bind redis client if available.
        self._bind_redis_client(scope)

        user_info = scope.get("state", {}).get("user_info")
        user_type = self._get_user_field(user_info, "user_type", "")
        user_id = self._get_user_field(user_info, "user_id", "")
        client_ip = self._get_client_ip(scope)

        checks = [
            ("global", "ratelimit:global", self.config.global_limit, self.config.global_window),
        ]

        if user_info:
            if user_type == "user":
                checks.append(
                    (
                        "user",
                        f"ratelimit:user:{user_id}",
                        self.config.user_limit,
                        self.config.user_window,
                    )
                )
            elif user_type in ("guest", "anonymous"):
                guest_key = user_id if user_type == "guest" else client_ip
                checks.append(
                    (
                        "guest",
                        f"ratelimit:guest:{guest_key}",
                        self.config.guest_limit,
                        self.config.guest_window,
                    )
                )

        checks.append(
            (
                "ip",
                f"ratelimit:ip:{client_ip}",
                self.config.ip_limit,
                self.config.ip_window,
            )
        )

        for dimension, key, limit, window in checks:
            result = await self.limiter.check(key, limit, window)
            if not result.allowed:
                result.dimension = dimension
                response = self._build_rate_limit_response(result)
                await response(scope, receive, send)
                return

        async def rate_limit_send(message: Message) -> None:
            if message["type"] == "http.response.start":
                headers = list(message.get("headers", []))
                headers.append((b"x-ratelimit-limit", str(self.config.user_limit).encode()))
                headers.append((b"x-ratelimit-window", str(self.config.user_window).encode()))
                message = {**message, "headers": headers}
            await send(message)

        await self.app(scope, receive, rate_limit_send)

    def _is_whitelisted(self, path: str) -> bool:
        """检查路径是否在白名单"""
        return any(path == wp or path.startswith(wp + "/") for wp in self.config.whitelist_paths)

    def _get_user_field(self, user_info: Any, field: str, default: Any = None) -> Any:
        if not user_info:
            return default
        if isinstance(user_info, dict):
            return user_info.get(field, default)
        return getattr(user_info, field, default)

    def _get_client_ip(self, scope: Scope) -> str:
        return get_client_ip_from_scope(scope)

    def _bind_redis_client(self, scope: Scope) -> None:
        if getattr(self.limiter, "redis", None) is not None:
            return

        app = scope.get("app")
        if not app:
            return

        redis = getattr(getattr(app, "state", None), "redis", None)
        if not redis:
            return

        redis_client = None
        if hasattr(redis, "get_native_client"):
            redis_client = redis.get_native_client()
        elif hasattr(redis, "pipeline"):
            redis_client = redis

        if redis_client:
            self.limiter.redis = redis_client

    def _build_rate_limit_response(self, info: RateLimitInfo) -> JSONResponse:
        return JSONResponse(
            status_code=429,
            content={
                "error": {
                    "code": "RATE_LIMIT_EXCEEDED",
                    "message": "Too many requests",
                    "dimension": info.dimension,
                    "limit": info.limit,
                    "remaining": 0,
                    "reset_at": info.reset_at,
                    "retry_after": info.retry_after,
                },
            },
            headers={
                "X-RateLimit-Limit": str(info.limit),
                "X-RateLimit-Remaining": "0",
                "X-RateLimit-Reset": str(info.reset_at),
                "X-RateLimit-Dimension": info.dimension,
                "Retry-After": str(info.retry_after),
            },
        )
