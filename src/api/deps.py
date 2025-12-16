from __future__ import annotations

from typing import List, Optional

from fastapi import Depends, HTTPException, Request
from pydantic import BaseModel

from ..config.settings import Settings
from ..core.auth.api_key import verify_api_key
from ..core.auth.jwt import decode_jwt_token
from ..core.auth.user_resolver import UserContext, UserResolver, UserResolverConfig
from ..core.exceptions import AuthError
from ..core.gateway.multi_dimension_rate_limiter import MultiDimensionRateLimiter
from ..adapters.langgraph_proxy import LangGraphProxy


class AuthContext(BaseModel):
    user_id: str = ""
    tenant_id: str = ""
    roles: List[str] = ["user"]


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


def get_user_resolver(request: Request) -> Optional[UserResolver]:
    """获取用户解析器"""
    return getattr(request.app.state, "user_resolver", None)


async def get_user_context(
    request: Request,
    settings: Settings = Depends(get_settings),
) -> UserContext:
    """获取用户上下文（支持 JWT、API Key、匿名用户）"""
    resolver = get_user_resolver(request)
    
    if resolver:
        return await resolver.resolve(request)
    
    # 回退到基于 AuthContext 的解析
    auth_ctx = await get_auth_context(request, settings)
    
    # 确定用户层级
    tier = "normal"
    if "admin" in auth_ctx.roles:
        tier = "admin"
    elif "premium" in auth_ctx.roles or "vip" in auth_ctx.roles:
        tier = "premium"
    elif "enterprise" in auth_ctx.roles:
        tier = "enterprise"
    elif not auth_ctx.user_id or auth_ctx.user_id.startswith("anon:"):
        tier = "anonymous"
    
    client_ip = ""
    if request.client:
        client_ip = request.client.host
    
    # 检查 X-Forwarded-For
    forwarded = request.headers.get("X-Forwarded-For")
    if forwarded:
        client_ip = forwarded.split(",")[0].strip()
    
    return UserContext(
        user_id=auth_ctx.user_id or f"anon:{client_ip}",
        tenant_id=auth_ctx.tenant_id,
        tier=tier,
        is_authenticated=bool(auth_ctx.user_id and not auth_ctx.user_id.startswith("anon:")),
        ip=client_ip,
        roles=auth_ctx.roles,
    )


async def get_auth_context(
    request: Request, settings: Settings = Depends(get_settings)
) -> AuthContext:
    auth_cfg = settings.authentication
    if not auth_cfg.jwt.enabled and not auth_cfg.api_key.enabled:
        ctx = AuthContext(user_id="local", tenant_id="local", roles=["admin"])
        request.state.auth = ctx
        return ctx

    roles: List[str] = ["user"]
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
        raw_roles = payload.get("roles") or payload.get("role") or roles
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
        verify_api_key(key, auth_cfg.api_key.keys)
        ctx = AuthContext()
        request.state.auth = ctx
        return ctx

    # 允许匿名访问（返回空的 AuthContext）
    ctx = AuthContext()
    request.state.auth = ctx
    return ctx
