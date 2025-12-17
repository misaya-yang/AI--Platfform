"""
统一鉴权中间件

支持：
- JWT Token 认证（登录用户）
- API Key 认证
- Guest Session 认证（游客）
- Anonymous 用户（基于 Cookie）

优先级：Authorization > X-API-Key > X-Guest-Session > Anonymous Cookie
"""

from __future__ import annotations

import time
from dataclasses import dataclass, field
from typing import Any, Callable, Dict, List, Optional

from fastapi import Request, HTTPException
from starlette.middleware.base import BaseHTTPMiddleware, RequestResponseEndpoint
from starlette.responses import Response, JSONResponse

from ..auth.user_resolver import UserContext
from ..observability.logging import get_logger

logger = get_logger(__name__)


@dataclass
class AuthConfig:
    """鉴权配置"""
    # JWT 配置
    jwt_enabled: bool = False
    jwt_secret: str = ""
    jwt_algorithms: List[str] = field(default_factory=lambda: ["HS256"])
    jwt_audience: Optional[str] = None
    jwt_issuer: Optional[str] = None

    # API Key 配置
    api_key_enabled: bool = False
    api_key_header: str = "X-API-Key"

    # Guest Session 配置
    guest_session_enabled: bool = True
    guest_session_header: str = "X-Guest-Session"

    # Anonymous 配置
    anonymous_enabled: bool = True
    anonymous_cookie: str = "ag_anon_id"
    anonymous_header: str = "X-AG-Anonymous-Id"

    # 白名单路径（不需要认证）
    whitelist_paths: List[str] = field(default_factory=lambda: [
        "/health",
        "/health/live",
        "/health/ready",
        "/metrics",
        "/docs",
        "/openapi.json",
    ])


@dataclass
class UserInfo:
    """认证后的用户信息"""
    user_id: str
    user_type: str  # "user" | "guest" | "anonymous"
    tenant_id: str = ""
    tier: str = "anonymous"
    is_authenticated: bool = False
    roles: List[str] = field(default_factory=list)
    session_id: Optional[str] = None  # 游客 session ID
    ip: str = ""
    metadata: Dict[str, Any] = field(default_factory=dict)

    def to_user_context(self) -> UserContext:
        """转换为 UserContext"""
        return UserContext(
            user_id=self.user_id,
            tenant_id=self.tenant_id,
            tier=self.tier,
            is_authenticated=self.is_authenticated,
            ip=self.ip,
            roles=self.roles,
        )


class AuthMiddleware(BaseHTTPMiddleware):
    """
    统一鉴权中间件

    职责：
    1. 从请求中提取认证信息（token、api key、guest session 等）
    2. 验证认证信息的有效性
    3. 提取用户信息
    4. 将用户信息注入到请求上下文
    """

    def __init__(
        self,
        app,
        config: AuthConfig,
        jwt_decoder: Optional[Callable] = None,
        api_key_validator: Optional[Callable] = None,
        guest_session_validator: Optional[Callable] = None,
    ):
        super().__init__(app)
        self.config = config
        self.jwt_decoder = jwt_decoder
        self.api_key_validator = api_key_validator
        self.guest_session_validator = guest_session_validator

    async def dispatch(
        self,
        request: Request,
        call_next: RequestResponseEndpoint,
    ) -> Response:
        """处理请求鉴权"""
        # 检查白名单路径
        if self._is_whitelisted(request.url.path):
            return await call_next(request)

        client_ip = self._get_client_ip(request)

        try:
            # 按优先级尝试认证
            user_info = await self._authenticate(request, client_ip)

            # 将用户信息注入到请求上下文
            request.state.user_info = user_info
            request.state.user_context = user_info.to_user_context()

            # 添加响应头（用于调试）
            response = await call_next(request)
            response.headers["X-User-Id"] = user_info.user_id
            response.headers["X-User-Type"] = user_info.user_type

            return response

        except HTTPException:
            raise
        except Exception as e:
            logger.error(f"Authentication error: {e}", exc_info=True)
            return JSONResponse(
                status_code=500,
                content={"error": "Internal authentication error"},
            )

    async def _authenticate(
        self,
        request: Request,
        client_ip: str,
    ) -> UserInfo:
        """
        认证逻辑（按优先级）

        1. Authorization header (JWT Token) -> 登录用户
        2. X-API-Key header -> API Key 用户
        3. X-Guest-Session header -> 游客会话
        4. Anonymous Cookie/Header -> 匿名用户
        """
        # 1. 尝试 JWT Token 认证
        if self.config.jwt_enabled:
            auth_header = request.headers.get("Authorization", "")
            if auth_header.lower().startswith("bearer "):
                token = auth_header[7:].strip()
                user_info = await self._authenticate_jwt(token, client_ip)
                if user_info:
                    return user_info

        # 2. 尝试 API Key 认证
        if self.config.api_key_enabled:
            api_key = request.headers.get(self.config.api_key_header)
            if api_key:
                user_info = await self._authenticate_api_key(api_key, client_ip)
                if user_info:
                    return user_info

        # 3. 尝试 Guest Session 认证
        if self.config.guest_session_enabled:
            session_id = request.headers.get(self.config.guest_session_header)
            if session_id:
                user_info = await self._authenticate_guest_session(session_id, client_ip)
                if user_info:
                    return user_info
                # 游客 session 无效时返回 401
                raise HTTPException(
                    status_code=401,
                    detail="Invalid or expired guest session",
                )

        # 4. 匿名用户
        if self.config.anonymous_enabled:
            anon_id = (
                request.headers.get(self.config.anonymous_header)
                or request.cookies.get(self.config.anonymous_cookie)
                or getattr(getattr(request, "state", None), "anonymous_id", None)
                or client_ip
            )
            return UserInfo(
                user_id=f"anon:{anon_id}",
                user_type="anonymous",
                tenant_id="public",
                tier="anonymous",
                is_authenticated=False,
                ip=client_ip,
                roles=["guest"],
            )

        # 都没有且匿名禁用，返回 401
        raise HTTPException(
            status_code=401,
            detail="Authentication required",
        )

    async def _authenticate_jwt(
        self,
        token: str,
        client_ip: str,
    ) -> Optional[UserInfo]:
        """JWT Token 认证"""
        if not self.jwt_decoder:
            return None

        try:
            payload = await self.jwt_decoder(
                token,
                secret=self.config.jwt_secret,
                algorithms=self.config.jwt_algorithms,
                audience=self.config.jwt_audience,
                issuer=self.config.jwt_issuer,
            )

            user_id = payload.get("sub") or payload.get("user_id")
            if not user_id:
                return None

            # 提取角色和层级
            roles = payload.get("roles", [])
            if isinstance(roles, str):
                roles = [roles]

            tier = payload.get("tier", "normal")
            if "admin" in roles:
                tier = "admin"
            elif "enterprise" in roles:
                tier = "enterprise"
            elif "premium" in roles or "vip" in roles:
                tier = "premium"

            return UserInfo(
                user_id=str(user_id),
                user_type="user",
                tenant_id=str(payload.get("tenant_id", "")),
                tier=tier,
                is_authenticated=True,
                roles=roles,
                ip=client_ip,
                metadata=payload,
            )
        except Exception as e:
            logger.warning(f"JWT validation failed: {e}")
            raise HTTPException(
                status_code=401,
                detail="Invalid or expired token",
            )

    async def _authenticate_api_key(
        self,
        api_key: str,
        client_ip: str,
    ) -> Optional[UserInfo]:
        """API Key 认证"""
        if not self.api_key_validator:
            return None

        try:
            key_info = await self.api_key_validator(api_key)
            if not key_info:
                raise HTTPException(
                    status_code=401,
                    detail="Invalid API key",
                )

            return UserInfo(
                user_id=key_info.get("user_id", f"apikey:{api_key[:8]}"),
                user_type="user",
                tenant_id=key_info.get("tenant_id", ""),
                tier=key_info.get("tier", "normal"),
                is_authenticated=True,
                roles=key_info.get("roles", ["user"]),
                ip=client_ip,
            )
        except HTTPException:
            raise
        except Exception as e:
            logger.warning(f"API key validation failed: {e}")
            return None

    async def _authenticate_guest_session(
        self,
        session_id: str,
        client_ip: str,
    ) -> Optional[UserInfo]:
        """Guest Session 认证"""
        if not self.guest_session_validator:
            # 没有验证器时，简单信任 session_id
            return UserInfo(
                user_id=session_id,
                user_type="guest",
                tenant_id="public",
                tier="anonymous",
                is_authenticated=False,
                session_id=session_id,
                ip=client_ip,
                roles=["guest"],
            )

        try:
            session_info = await self.guest_session_validator(session_id)
            if not session_info:
                return None

            return UserInfo(
                user_id=session_id,
                user_type="guest",
                tenant_id=session_info.get("tenant_id", "public"),
                tier="anonymous",
                is_authenticated=False,
                session_id=session_id,
                ip=client_ip,
                roles=["guest"],
                metadata=session_info,
            )
        except Exception as e:
            logger.warning(f"Guest session validation failed: {e}")
            return None

    def _is_whitelisted(self, path: str) -> bool:
        """检查路径是否在白名单中"""
        for whitelist_path in self.config.whitelist_paths:
            if path == whitelist_path or path.startswith(whitelist_path + "/"):
                return True
        return False

    def _get_client_ip(self, request: Request) -> str:
        """获取真实客户端 IP（考虑代理）"""
        forwarded = request.headers.get("X-Forwarded-For")
        if forwarded:
            return forwarded.split(",")[0].strip()

        real_ip = request.headers.get("X-Real-IP")
        if real_ip:
            return real_ip

        if request.client:
            return request.client.host

        return "unknown"


# ============ JWT 解码辅助函数 ============

async def default_jwt_decoder(
    token: str,
    secret: str,
    algorithms: List[str],
    audience: Optional[str] = None,
    issuer: Optional[str] = None,
) -> Dict[str, Any]:
    """默认 JWT 解码器"""
    import jwt

    return jwt.decode(
        token,
        secret,
        algorithms=algorithms,
        audience=audience,
        issuer=issuer,
    )


# ============ 远程 JWT 验证器 ============

class RemoteJWTValidator:
    """
    远程 JWT 验证器

    调用用户服务验证 JWT，支持实时吊销检查
    """

    def __init__(
        self,
        verify_url: str,
        timeout: float = 5.0,
        cache_ttl: int = 60,
    ):
        self.verify_url = verify_url
        self.timeout = timeout
        self.cache_ttl = cache_ttl
        self._cache: Dict[str, tuple] = {}  # token -> (payload, expire_time)

    async def __call__(
        self,
        token: str,
        **kwargs,
    ) -> Dict[str, Any]:
        """验证 JWT"""
        import httpx

        # 检查缓存
        if token in self._cache:
            payload, expire_time = self._cache[token]
            if time.time() < expire_time:
                return payload
            del self._cache[token]

        # 调用远程服务验证
        async with httpx.AsyncClient(timeout=self.timeout) as client:
            response = await client.post(
                self.verify_url,
                json={"token": token},
            )

            if response.status_code != 200:
                raise HTTPException(
                    status_code=401,
                    detail="Token validation failed",
                )

            payload = response.json()

            # 缓存结果
            self._cache[token] = (payload, time.time() + self.cache_ttl)

            return payload
