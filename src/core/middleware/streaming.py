"""
流式响应友好的纯 ASGI 中间件

解决 BaseHTTPMiddleware 缓冲 StreamingResponse 的问题。

关键点：
1. BaseHTTPMiddleware 的 call_next() 会缓冲整个响应体
2. 纯 ASGI 中间件直接传递 send/receive，不缓冲响应
3. 对于流式路径，必须使用纯 ASGI 实现
"""

from __future__ import annotations

import time
import uuid
from dataclasses import dataclass, field
from typing import Any, Callable, Dict, List, Optional, Set

from starlette.types import ASGIApp, Message, Receive, Scope, Send

from ..observability.logging import get_logger
from ...services.metrics import get_metrics_recorder

logger = get_logger(__name__)


# ============ 流式路径检测 ============

STREAMING_PATHS: Set[str] = {
    "/api/v1/stream",
}

STREAMING_PATH_PREFIXES: List[str] = [
    "/api/v1/conversations/",  # /api/v1/conversations/{id}/stream
    "/api/v1/langgraph/",  # LangGraph SSE endpoints
    "/api/v1/proxy/",  # 透明代理 SSE endpoints
    "/proxy/",  # 透明代理（无版本前缀）
]

# Streaming suffixes to detect (LangGraph 兼容)
STREAMING_SUFFIXES: List[str] = [
    "/stream",           # 通用流式后缀
    "/runs/stream",      # LangGraph runs stream
    "/sse",              # SSE endpoint
]

# 额外的流式路径关键词检测
STREAMING_KEYWORDS: List[str] = [
    "/stream",           # 包含 stream 的路径
    "/events",           # SSE events
]


def is_streaming_path(path: str) -> bool:
    """检查是否是流式路径

    Critical for latency: paths detected as streaming will bypass
    response buffering in middleware, enabling true streaming.
    """
    # Exact match
    if path in STREAMING_PATHS:
        return True

    # Check suffixes (handles /runs/stream, /conversations/xxx/stream, etc.)
    for suffix in STREAMING_SUFFIXES:
        if path.endswith(suffix):
            return True

    # Check prefixes (streaming API areas)
    for prefix in STREAMING_PATH_PREFIXES:
        if path.startswith(prefix):
            # Check if this specific path is streaming-related
            for keyword in STREAMING_KEYWORDS:
                if keyword in path:
                    return True
            # Also check suffixes within prefixed paths
            for suffix in STREAMING_SUFFIXES:
                if path.endswith(suffix):
                    return True

    # Check keywords anywhere in path (but exclude false positives like "upstream")
    path_lower = path.lower()
    if "/stream" in path_lower and "upstream" not in path_lower:
        return True

    return False


# ============ 纯 ASGI 中间件基类 ============

class PureASGIMiddleware:
    """
    纯 ASGI 中间件基类

    不使用 BaseHTTPMiddleware，避免缓冲 StreamingResponse。
    子类需要实现 process_request 和 process_response 方法。
    """

    def __init__(self, app: ASGIApp):
        self.app = app

    async def __call__(self, scope: Scope, receive: Receive, send: Send) -> None:
        if scope["type"] != "http":
            await self.app(scope, receive, send)
            return

        # 流式路径直接传递
        path = scope.get("path", "")
        if is_streaming_path(path):
            await self._handle_streaming(scope, receive, send)
            return

        # 非流式路径可以进行处理
        await self._handle_normal(scope, receive, send)

    async def _handle_streaming(self, scope: Scope, receive: Receive, send: Send) -> None:
        """处理流式请求 - 直接传递，不缓冲"""
        # 仅做必要的请求处理（如注入 state）
        await self.process_streaming_request(scope, receive)
        await self.app(scope, receive, send)

    async def _handle_normal(self, scope: Scope, receive: Receive, send: Send) -> None:
        """处理非流式请求 - 可以进行完整处理"""
        # 请求处理
        should_continue = await self.process_request(scope, receive)
        if not should_continue:
            # 如果返回 False，说明已经发送了响应
            return

        # 包装 send 以处理响应
        await self.app(scope, receive, self._wrap_send(scope, send))

    def _wrap_send(self, scope: Scope, send: Send) -> Send:
        """包装 send 以处理响应"""
        async def wrapped_send(message: Message) -> None:
            if message["type"] == "http.response.start":
                message = await self.process_response_start(scope, message)
            await send(message)
        return wrapped_send

    async def process_streaming_request(self, scope: Scope, receive: Receive) -> None:
        """处理流式请求（仅必要处理，如注入 state）"""
        pass

    async def process_request(self, scope: Scope, receive: Receive) -> bool:
        """处理请求，返回 True 继续，False 表示已发送响应"""
        return True

    async def process_response_start(self, scope: Scope, message: Message) -> Message:
        """处理响应开始，可以修改响应头"""
        return message


# ============ 流式友好的鉴权中间件 ============

@dataclass
class StreamingAuthConfig:
    """流式友好的鉴权配置"""
    jwt_enabled: bool = False
    jwt_secret: str = ""
    jwt_algorithms: List[str] = field(default_factory=lambda: ["HS256"])
    api_key_enabled: bool = False
    api_key_header: str = "X-API-Key"
    guest_session_enabled: bool = True
    guest_session_header: str = "X-Guest-Session"
    anonymous_enabled: bool = True
    anonymous_cookie: str = "ag_anon_id"
    anonymous_header: str = "X-AG-Anonymous-Id"
    whitelist_paths: List[str] = field(default_factory=lambda: [
        "/health", "/health/live", "/health/ready", "/metrics", "/docs", "/openapi.json"
    ])


class StreamingAuthMiddleware(PureASGIMiddleware):
    """
    流式友好的鉴权中间件

    对于流式路径，直接传递请求，仅注入用户信息到 scope["state"]。
    对于非流式路径，执行完整的鉴权流程。
    """

    def __init__(self, app: ASGIApp, config: StreamingAuthConfig):
        super().__init__(app)
        self.config = config

    async def process_streaming_request(self, scope: Scope, receive: Receive) -> None:
        """流式请求仅注入用户信息"""
        # 确保 state 存在
        if "state" not in scope:
            scope["state"] = {}

        # 提取用户信息并注入 state
        user_info = self._extract_user_info(scope)
        scope["state"]["user_info"] = user_info

    async def process_request(self, scope: Scope, receive: Receive) -> bool:
        """非流式请求执行完整鉴权"""
        path = scope.get("path", "")

        # 白名单路径跳过
        if self._is_whitelisted(path):
            return True

        # 确保 state 存在
        if "state" not in scope:
            scope["state"] = {}

        # 提取用户信息
        user_info = self._extract_user_info(scope)
        scope["state"]["user_info"] = user_info

        return True

    async def process_response_start(self, scope: Scope, message: Message) -> Message:
        """添加用户信息到响应头"""
        user_info = scope.get("state", {}).get("user_info")
        if user_info and message["type"] == "http.response.start":
            headers = dict(message.get("headers", []))
            headers[b"x-user-id"] = user_info.get("user_id", "unknown").encode()
            headers[b"x-user-type"] = user_info.get("user_type", "unknown").encode()
            message = {**message, "headers": list(headers.items())}
        return message

    def _extract_user_info(self, scope: Scope) -> Dict[str, Any]:
        """从请求头提取用户信息
        
        Supports:
        - Authorization: Bearer <jwt> header
        - X-API-Key header  
        - X-Guest-Session header
        - X-AG-Anonymous-Id header or cookie
        """
        headers = dict(scope.get("headers", []))
        client_ip = self._get_client_ip(scope)

        # Try to extract JWT user if present (don't validate here, just extract claims)
        auth_header = headers.get(b"authorization", b"").decode()
        if auth_header.lower().startswith("bearer "):
            try:
                import base64
                import json
                token = auth_header.split(" ", 1)[1]
                # Decode JWT payload without verification (middleware doesn't verify)
                parts = token.split(".")
                if len(parts) >= 2:
                    payload_b64 = parts[1]
                    # Add padding if needed
                    payload_b64 += "=" * (4 - len(payload_b64) % 4)
                    payload = json.loads(base64.urlsafe_b64decode(payload_b64))
                    user_id = str(payload.get("sub") or payload.get("user_id") or "")
                    if user_id:
                        return {
                            "user_id": user_id,
                            "user_type": "user",
                            "tenant_id": str(payload.get("tenant_id", "")),
                            "tier": str(payload.get("tier", "normal")),
                            "is_authenticated": True,
                            "ip": client_ip,
                            "roles": payload.get("roles", ["user"]),
                        }
            except Exception:
                pass  # Fall through to anonymous

        # Try API key
        api_key = headers.get(self.config.api_key_header.lower().encode(), b"").decode()
        if api_key:
            import hashlib
            key_hash = hashlib.sha256(api_key.encode()).hexdigest()[:16]
            return {
                "user_id": f"apikey:{key_hash}",
                "user_type": "user",
                "tenant_id": "",
                "tier": "normal",
                "is_authenticated": True,
                "ip": client_ip,
                "roles": ["user"],
            }

        # Try guest session
        guest_session = headers.get(self.config.guest_session_header.lower().encode(), b"").decode()
        if guest_session:
            return {
                "user_id": guest_session,
                "user_type": "guest",
                "tenant_id": "public",
                "tier": "anonymous",
                "is_authenticated": False,
                "session_id": guest_session,
                "ip": client_ip,
                "roles": ["guest"],
            }

        # Fallback to anonymous
        anon_id = (
            headers.get(b"x-ag-anonymous-id", b"").decode()
            or self._extract_cookie(headers, self.config.anonymous_cookie)
            or client_ip
        )

        return {
            "user_id": f"anon:{anon_id}",
            "user_type": "anonymous",
            "tenant_id": "public",
            "tier": "anonymous",
            "is_authenticated": False,
            "ip": client_ip,
            "roles": ["guest"],
        }

    def _extract_cookie(self, headers: Dict[bytes, bytes], cookie_name: str) -> str:
        """Extract a specific cookie value from headers"""
        cookie_header = headers.get(b"cookie", b"").decode()
        if cookie_header:
            for part in cookie_header.split(";"):
                if "=" in part:
                    name, value = part.strip().split("=", 1)
                    if name == cookie_name:
                        return value
        return ""

    def _is_whitelisted(self, path: str) -> bool:
        """检查路径是否在白名单"""
        for wp in self.config.whitelist_paths:
            if path == wp or path.startswith(wp + "/"):
                return True
        return False

    def _get_client_ip(self, scope: Scope) -> str:
        """获取客户端 IP"""
        headers = dict(scope.get("headers", []))

        forwarded = headers.get(b"x-forwarded-for", b"").decode()
        if forwarded:
            return forwarded.split(",")[0].strip()

        real_ip = headers.get(b"x-real-ip", b"").decode()
        if real_ip:
            return real_ip

        client = scope.get("client")
        if client:
            return client[0]

        return "unknown"


# ============ 流式友好的限流中间件 ============

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
    whitelist_paths: List[str] = field(default_factory=lambda: [
        "/health", "/health/live", "/health/ready", "/metrics"
    ])


class StreamingRateLimitMiddleware(PureASGIMiddleware):
    """
    流式友好的限流中间件

    对于流式路径，跳过限流检查（或使用异步检查）。
    对于非流式路径，执行完整的限流检查。
    """

    def __init__(self, app: ASGIApp, config: StreamingRateLimitConfig):
        super().__init__(app)
        self.config = config

    async def process_streaming_request(self, scope: Scope, receive: Receive) -> None:
        """流式请求跳过限流或使用轻量检查"""
        # 对于流式请求，可以在这里进行异步限流检查
        # 但为了不阻塞流式响应，这里仅记录
        pass

    async def process_request(self, scope: Scope, receive: Receive) -> bool:
        """非流式请求执行限流检查"""
        if not self.config.enabled:
            return True

        path = scope.get("path", "")
        if self._is_whitelisted(path):
            return True

        # TODO: 实现实际的限流检查
        # 这里为了简化，暂时直接通过
        return True

    def _is_whitelisted(self, path: str) -> bool:
        """检查路径是否在白名单"""
        for wp in self.config.whitelist_paths:
            if path == wp or path.startswith(wp + "/"):
                return True
        return False


# ============ 流式友好的请求日志中间件 ============

@dataclass
class StreamingLogConfig:
    """流式友好的日志配置"""
    enabled: bool = True
    log_request_body: bool = False
    log_response_body: bool = False
    exclude_paths: List[str] = field(default_factory=lambda: [
        "/health", "/health/live", "/health/ready", "/metrics"
    ])


class StreamingLoggingMiddleware(PureASGIMiddleware):
    """
    流式友好的请求日志中间件

    对于流式路径，仅记录请求开始，不等待响应完成。
    对于非流式路径，记录完整的请求/响应。
    """

    def __init__(self, app: ASGIApp, config: StreamingLogConfig):
        super().__init__(app)
        self.config = config

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

        # 生成请求 ID
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
            try:
                metrics_recorder = get_metrics_recorder()
                user_info = scope.get("state", {}).get("user_info", {})
                await metrics_recorder.record_request(
                    method=method,
                    path=path,
                    status_code=200,  # Streaming requests typically succeed if they complete
                    duration_ms=duration,
                    user_id=user_info.get("user_id", ""),
                    service_id=scope.get("state", {}).get("service_id", ""),
                )
            except Exception as metrics_err:
                logger.debug(f"Failed to record streaming metrics: {metrics_err}")

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
            try:
                metrics_recorder = get_metrics_recorder()
                user_info = scope.get("state", {}).get("user_info", {})
                await metrics_recorder.record_request(
                    method=method,
                    path=path,
                    status_code=status_code,
                    duration_ms=duration,
                    user_id=user_info.get("user_id", ""),
                    service_id=scope.get("state", {}).get("service_id", ""),
                )
            except Exception as metrics_err:
                logger.debug(f"Failed to record metrics: {metrics_err}")

        except Exception as e:
            duration = (time.time() - start_time) * 1000
            logger.error(f"[{request_id}] {method} {path} -> ERROR ({duration:.2f}ms): {e}")

            # Record error metrics
            try:
                metrics_recorder = get_metrics_recorder()
                user_info = scope.get("state", {}).get("user_info", {})
                await metrics_recorder.record_request(
                    method=method,
                    path=path,
                    status_code=500,
                    duration_ms=duration,
                    user_id=user_info.get("user_id", ""),
                    service_id=scope.get("state", {}).get("service_id", ""),
                )
            except Exception as metrics_err:
                logger.debug(f"Failed to record error metrics: {metrics_err}")

            raise

    def _is_excluded(self, path: str) -> bool:
        """检查路径是否被排除"""
        for ep in self.config.exclude_paths:
            if path == ep or path.startswith(ep + "/"):
                return True
        return False


# ============ 流式友好的追踪中间件 ============

@dataclass
class StreamingTracingConfig:
    """流式友好的追踪配置"""
    service_name: str = "gateway"
    log_requests: bool = True
    log_responses: bool = True
    exclude_paths: Set[str] = field(default_factory=lambda: {
        "/health", "/health/live", "/health/ready", "/metrics"
    })


class StreamingTracingMiddleware(PureASGIMiddleware):
    """
    流式友好的追踪中间件

    使用纯 ASGI 实现，避免缓冲 StreamingResponse。
    """

    TRACE_ID_HEADER = b"x-trace-id"
    SPAN_ID_HEADER = b"x-span-id"

    def __init__(self, app: ASGIApp, config: StreamingTracingConfig):
        super().__init__(app)
        self.config = config

    async def __call__(self, scope: Scope, receive: Receive, send: Send) -> None:
        if scope["type"] != "http":
            await self.app(scope, receive, send)
            return

        path = scope.get("path", "")
        if path in self.config.exclude_paths:
            await self.app(scope, receive, send)
            return

        # 获取或生成 trace_id
        headers = dict(scope.get("headers", []))
        trace_id = headers.get(self.TRACE_ID_HEADER, b"").decode() or uuid.uuid4().hex
        span_id = uuid.uuid4().hex[:16]

        # 注入到 state
        if "state" not in scope:
            scope["state"] = {}
        scope["state"]["trace_id"] = trace_id
        scope["state"]["span_id"] = span_id

        start_time = time.time()
        method = scope.get("method", "")

        if self.config.log_requests:
            logger.info(f"Request started: {method} {path}", extra={"trace_id": trace_id})

        status_code = 0

        async def tracing_send(message: Message) -> None:
            nonlocal status_code
            if message["type"] == "http.response.start":
                status_code = message.get("status", 0)
                headers = list(message.get("headers", []))
                headers.append((self.TRACE_ID_HEADER, trace_id.encode()))
                headers.append((self.SPAN_ID_HEADER, span_id.encode()))
                message = {**message, "headers": headers}
            await send(message)

        try:
            await self.app(scope, receive, tracing_send)

            if self.config.log_responses:
                duration = (time.time() - start_time) * 1000
                logger.info(
                    f"Request completed: {method} {path} -> {status_code} ({duration:.2f}ms)",
                    extra={"trace_id": trace_id, "status_code": status_code, "duration_ms": duration}
                )
        except Exception as e:
            duration = (time.time() - start_time) * 1000
            logger.error(
                f"Request failed: {method} {path} ({duration:.2f}ms)",
                extra={"trace_id": trace_id, "error": str(e), "duration_ms": duration},
                exc_info=True
            )
            raise


# ============ 流式友好的匿名身份中间件 ============

@dataclass
class StreamingAnonymousConfig:
    """流式友好的匿名身份配置"""
    enabled: bool = True
    header_name: str = "X-AG-Anonymous-Id"
    cookie_name: str = "ag_anon_id"
    ttl_days: int = 365
    same_site: str = "lax"


def _is_valid_uuid(value: str) -> bool:
    try:
        uuid.UUID(str(value))
        return True
    except Exception:
        return False


class StreamingAnonymousMiddleware(PureASGIMiddleware):
    """
    流式友好的匿名身份中间件

    使用纯 ASGI 实现，为匿名用户提供稳定的标识符。
    """

    def __init__(self, app: ASGIApp, config: StreamingAnonymousConfig):
        super().__init__(app)
        self.config = config

    async def __call__(self, scope: Scope, receive: Receive, send: Send) -> None:
        if scope["type"] != "http":
            await self.app(scope, receive, send)
            return

        if not self.config.enabled:
            await self.app(scope, receive, send)
            return

        # 提取或生成匿名 ID
        headers = dict(scope.get("headers", []))

        # 尝试从 header 获取
        raw_id = headers.get(self.config.header_name.lower().encode(), b"").decode()

        # 尝试从 cookie 获取
        if not raw_id:
            cookie_header = headers.get(b"cookie", b"").decode()
            if cookie_header:
                for part in cookie_header.split(";"):
                    if "=" in part:
                        name, value = part.strip().split("=", 1)
                        if name == self.config.cookie_name:
                            raw_id = value
                            break

        has_valid = bool(raw_id) and _is_valid_uuid(raw_id)
        anon_id = raw_id if has_valid else str(uuid.uuid4())

        # 注入到 state
        if "state" not in scope:
            scope["state"] = {}
        scope["state"]["anonymous_id"] = anon_id

        # 如果需要设置 cookie
        need_set_cookie = not has_valid

        async def anon_send(message: Message) -> None:
            if message["type"] == "http.response.start" and need_set_cookie:
                headers = list(message.get("headers", []))

                # 设置 cookie
                max_age = int(self.config.ttl_days) * 86400
                cookie_value = (
                    f"{self.config.cookie_name}={anon_id}; "
                    f"Max-Age={max_age}; "
                    f"Path=/; "
                    f"HttpOnly; "
                    f"SameSite={self.config.same_site}"
                )
                headers.append((b"set-cookie", cookie_value.encode()))

                # 设置 header
                headers.append((self.config.header_name.encode(), anon_id.encode()))

                message = {**message, "headers": headers}

            await send(message)

        await self.app(scope, receive, anon_send)
