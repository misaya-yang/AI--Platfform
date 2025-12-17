from __future__ import annotations

import hashlib
from typing import List, Optional

from fastapi import Depends, HTTPException, Request
from pydantic import BaseModel

from ..config.settings import Settings
from ..core.auth.api_key import verify_api_key
from ..core.auth.jwt import decode_jwt_token
from ..core.auth.user_resolver import UserContext
from ..core.exceptions import AuthError
from ..core.gateway.multi_dimension_rate_limiter import MultiDimensionRateLimiter
from ..adapters.langgraph_proxy import LangGraphProxy


class AuthContext(BaseModel):
    user_id: str = ""
    tenant_id: str = ""
    roles: List[str] = ["guest"]


def get_settings(request: Request) -> Settings:
    return request.app.state.settings


def get_dispatcher(request: Request):
    return request.app.state.dispatcher


def get_task_manager(request: Request):
    return request.app.state.task_manager


def get_registry(request: Request):
    return request.app.state.registry


def get_session_manager(request: Request):
    return request.app.state.session_manager


def get_health_monitor(request: Request):
    return request.app.state.health_monitor


def get_langgraph_proxy(request: Request) -> Optional[LangGraphProxy]:
    """获取 LangGraph 代理"""
    return getattr(request.app.state, "langgraph_proxy", None)

def require_langgraph_proxy(request: Request) -> LangGraphProxy:
    """获取 LangGraph 代理（若未初始化则返回 503）"""
    proxy = getattr(request.app.state, "langgraph_proxy", None)
    if proxy is None:
        raise HTTPException(
            status_code=503,
            detail="LangGraph proxy is not initialized (check GATEWAY_LANGGRAPH__ENABLED and INSTANCE_URLS).",
        )
    return proxy


def get_rate_limiter(request: Request) -> Optional[MultiDimensionRateLimiter]:
    """获取多维度限流器"""
    return getattr(request.app.state, "multi_rate_limiter", None)


def get_guest_session_manager(request: Request):
    """获取游客会话管理器"""
    return getattr(request.app.state, "guest_session_manager", None)


def _get_client_ip(request: Request) -> str:
    """Best-effort client IP (proxy-aware)."""
    forwarded = request.headers.get("X-Forwarded-For")
    if forwarded:
        return forwarded.split(",")[0].strip()
    real_ip = request.headers.get("X-Real-IP")
    if real_ip:
        return real_ip
    if request.client:
        return request.client.host
    return "unknown"


def _derive_api_key_user_id(api_key: str) -> str:
    digest = hashlib.sha256(api_key.encode("utf-8")).hexdigest()
    return f"apikey:{digest[:16]}"


def _normalize_roles(raw_roles) -> List[str]:
    if not raw_roles:
        return ["user"]
    if isinstance(raw_roles, str):
        return [raw_roles]
    if isinstance(raw_roles, list):
        return [str(r) for r in raw_roles if r is not None]
    return ["user"]


async def get_user_context(
    request: Request,
    settings: Settings = Depends(get_settings),
) -> UserContext:
    """User context for request scoping (JWT / API key / anonymous cookie)."""
    client_ip = _get_client_ip(request)
    auth_cfg = settings.authentication

    # Local/dev convenience: if auth is disabled, behave like an admin user.
    if not auth_cfg.jwt.enabled and not auth_cfg.api_key.enabled:
        return UserContext(
            user_id="local",
            tenant_id="local",
            tier="admin",
            is_authenticated=True,
            ip=client_ip,
            roles=["admin"],
        )

    # 1) JWT (Bearer)
    auth_header = request.headers.get("Authorization") or ""
    if auth_cfg.jwt.enabled and auth_header.lower().startswith("bearer "):
        token = auth_header.split(" ", 1)[1].strip()
        payload = decode_jwt_token(
            token,
            secret=auth_cfg.jwt.secret,
            algorithms=auth_cfg.jwt.algorithms,
            audience=auth_cfg.jwt.audience,
            issuer=auth_cfg.jwt.issuer,
        )
        user_id = str(payload.get("sub") or payload.get("user_id") or "")
        if not user_id:
            raise AuthError("Missing user_id in JWT token")
        tenant_id = str(payload.get("tenant_id") or payload.get("tenant") or "")
        roles = _normalize_roles(payload.get("roles") or payload.get("role"))
        tier = str(payload.get("tier") or "normal")
        if "admin" in roles:
            tier = "admin"
        elif "enterprise" in roles:
            tier = "enterprise"
        elif "premium" in roles or "vip" in roles:
            tier = "premium"
        return UserContext(
            user_id=user_id,
            tenant_id=tenant_id,
            tier=tier,
            is_authenticated=True,
            ip=client_ip,
            roles=roles,
        )

    # 2) API key
    if auth_cfg.api_key.enabled:
        api_key = request.headers.get(auth_cfg.api_key.header_name)
        if api_key:
            db = getattr(request.app.state, "database", None)
            key_hash = hashlib.sha256(api_key.encode("utf-8")).hexdigest()
            if db and getattr(db, "enabled", False):
                key_info = await db.get_api_key(key_hash)
                if key_info:
                    roles = _normalize_roles(key_info.get("roles"))
                    tenant_id = str(key_info.get("tenant_id") or "")
                    user_id = str(key_info.get("user_id") or "") or _derive_api_key_user_id(api_key)
                    tier = str(key_info.get("tier") or "normal")
                    return UserContext(
                        user_id=user_id,
                        tenant_id=tenant_id,
                        tier=tier,
                        is_authenticated=True,
                        ip=client_ip,
                        roles=roles,
                    )

            # Fallback to static allowlist (env-configured keys)
            verify_api_key(api_key, auth_cfg.api_key.keys)
            return UserContext(
                user_id=_derive_api_key_user_id(api_key),
                tenant_id="",
                tier="normal",
                is_authenticated=True,
                ip=client_ip,
                roles=["user"],
            )

    # 3) Anonymous (stable ID minted by middleware)
    anon_id = getattr(getattr(request, "state", None), "anonymous_id", None) or client_ip
    return UserContext(
        user_id=f"anon:{anon_id}",
        tenant_id="public",
        tier="anonymous",
        is_authenticated=False,
        ip=client_ip,
        roles=["guest"],
    )


async def get_auth_context(
    request: Request, settings: Settings = Depends(get_settings)
) -> AuthContext:
    auth_cfg = settings.authentication
    if not auth_cfg.jwt.enabled and not auth_cfg.api_key.enabled:
        ctx = AuthContext(user_id="local", tenant_id="local", roles=["admin"])
        request.state.auth = ctx
        return ctx

    # Unauthenticated requests default to a guest role.
    roles: List[str] = ["guest"]
    user_id = ""
    tenant_id = ""

    auth_header = request.headers.get("Authorization")
    if auth_cfg.jwt.enabled and auth_header and auth_header.lower().startswith("bearer "):
        token = auth_header.split(" ", 1)[1]
        payload = decode_jwt_token(
            token,
            secret=auth_cfg.jwt.secret,
            algorithms=auth_cfg.jwt.algorithms,
            audience=auth_cfg.jwt.audience,
            issuer=auth_cfg.jwt.issuer,
        )
        user_id = str(payload.get("sub") or payload.get("user_id") or "")
        tenant_id = str(payload.get("tenant_id") or "")
        raw_roles = payload.get("roles") or payload.get("role") or ["user"]
        if isinstance(raw_roles, str):
            roles = [raw_roles]
        elif isinstance(raw_roles, list):
            roles = [str(r) for r in raw_roles]

        ctx = AuthContext(user_id=user_id, tenant_id=tenant_id, roles=roles)
        request.state.auth = ctx
        return ctx

    if auth_cfg.api_key.enabled:
        key = request.headers.get(auth_cfg.api_key.header_name)
        if not key:
            raise AuthError("Missing API key")

        db = getattr(request.app.state, "database", None)
        key_hash = hashlib.sha256(key.encode("utf-8")).hexdigest()
        if db and getattr(db, "enabled", False):
            key_info = await db.get_api_key(key_hash)
            if key_info:
                roles = _normalize_roles(key_info.get("roles"))
                tenant_id = str(key_info.get("tenant_id") or "")
                user_id = str(key_info.get("user_id") or "") or _derive_api_key_user_id(key)
                ctx = AuthContext(user_id=user_id, tenant_id=tenant_id, roles=roles)
                request.state.auth = ctx
                return ctx

        verify_api_key(key, auth_cfg.api_key.keys)
        ctx = AuthContext(user_id=_derive_api_key_user_id(key), tenant_id="", roles=["user"])
        request.state.auth = ctx
        return ctx

    # 允许匿名访问（返回空的 AuthContext）
    ctx = AuthContext()
    request.state.auth = ctx
    return ctx
