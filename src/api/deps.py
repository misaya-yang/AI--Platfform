from __future__ import annotations

import asyncio
import hashlib
import logging
import os
import time
from typing import Any

from ai_gateway_core.exceptions import AuthError
from fastapi import Depends, HTTPException, Request
from pydantic import BaseModel

from ..adapters.langgraph_proxy import LangGraphProxy
from ..config.settings import Settings
from ..core.auth.api_key import verify_api_key
from ..core.auth.jwt import decode_jwt_token
from ..core.auth.jwt_config import get_jwt_algorithms, get_jwt_secret
from ..core.auth.permissions import Capability, build_permission_denied_detail, check_capability
from ..core.auth.rbac import RBAC
from ..core.auth.user_resolver import UserContext
from ..core.client_ip import get_client_ip_from_request
from ..core.gateway.multi_dimension_rate_limiter import MultiDimensionRateLimiter

logger = logging.getLogger(__name__)


class AuthContext(BaseModel):
    user_id: str = ""
    tenant_id: str = ""
    roles: list[str] = ["guest"]
    permissions: list[str] = []
    is_authenticated: bool = False


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


def get_langgraph_proxy(request: Request) -> LangGraphProxy | None:
    """获取 LangGraph 代理"""
    return getattr(request.app.state, "langgraph_proxy", None)


def get_knowledge_service(request: Request):
    """Deprecated: KB now runs as independent microservice at :8092.
    Kept as stub — returns None. Knowledge routes proxy to KB Service."""
    return None


def get_knowledge_worker(request: Request):
    """Deprecated: KB worker now runs inside KB Service microservice."""
    return None


def require_langgraph_proxy(request: Request) -> LangGraphProxy:
    """获取 LangGraph 代理（若未初始化则返回 503）"""
    proxy = getattr(request.app.state, "langgraph_proxy", None)
    if proxy is None:
        raise HTTPException(
            status_code=503,
            detail="LangGraph proxy is not initialized (check GATEWAY_LANGGRAPH__ENABLED and INSTANCE_URLS).",
        )
    return proxy


def get_image_storage_service(request: Request):
    """Get ImageStorageService for image storage operations."""
    svc = getattr(request.app.state, "image_storage_service", None)
    if svc is None:
        raise HTTPException(
            status_code=503,
            detail="Image storage service is not initialized.",
        )
    return svc


def get_rate_limiter(request: Request) -> MultiDimensionRateLimiter | None:
    """获取多维度限流器"""
    return getattr(request.app.state, "multi_rate_limiter", None)


async def enforce_rate_limit(
    request: Request,
    user=None,
    operation: str | None = None,
) -> None:
    """Helper to apply multi-dimension rate limiting to any endpoint.

    Call at the top of a heavy endpoint. Raises HTTPException 429 if the
    current request exceeds any configured dimension (ip/user/tenant/
    operation). Silently no-ops if the limiter is not configured.

    Args:
        request: FastAPI Request (used to locate the limiter from app state
                 and to pull client IP when user.ip is unset)
        user: optional UserContext for tier/user/tenant dimensions. If None
              (anonymous public endpoint), limiting falls back to IP-only.
        operation: optional operation name that maps to
                   config.operation_limits (e.g. "quiz_generate",
                   "file_upload", "code_execute", "image_generate")
    """
    from fastapi import HTTPException

    from ..core.gateway.multi_dimension_rate_limiter import RateLimitContext

    limiter: MultiDimensionRateLimiter | None = getattr(
        request.app.state, "multi_rate_limiter", None
    )
    if limiter is None:
        return

    # Build context. Anonymous endpoints have no user → IP-only limiting.
    if user is not None:
        ctx = RateLimitContext.from_user_context(user, operation=operation)
    else:
        ctx = RateLimitContext(ip="", operation=operation)
    if not ctx.ip:
        ctx.ip = _get_client_ip(request)

    result = await limiter.check(ctx)
    if not result.allowed:
        raise HTTPException(
            status_code=429,
            detail=f"Rate limit exceeded ({operation or 'request'})",
            headers={
                "X-RateLimit-Limit": str(result.limit),
                "X-RateLimit-Remaining": str(result.remaining),
                "Retry-After": str(result.retry_after),
            },
        )


def get_guest_session_manager(request: Request):
    """获取游客会话管理器"""
    return getattr(request.app.state, "guest_session_manager", None)


def _get_client_ip(request: Request) -> str:
    """Best-effort client IP (proxy-aware)."""
    return get_client_ip_from_request(request)


def _request_trace_id(request: Request) -> str:
    request_id = getattr(request.state, "request_id", "")
    if isinstance(request_id, str) and request_id:
        return request_id
    trace_id = getattr(request.state, "trace_id", "")
    if isinstance(trace_id, str) and trace_id:
        return trace_id
    return ""


def _gateway_rbac(request: Request) -> RBAC:
    dispatcher = getattr(request.app.state, "dispatcher", None)
    rbac = getattr(dispatcher, "rbac", None)
    if isinstance(rbac, RBAC):
        return rbac

    settings = getattr(request.app.state, "settings", None)
    if settings is None:
        settings = Settings()
    return RBAC(role_permissions=settings.rbac.roles)


def require_gateway_capability(
    request: Request,
    auth: AuthContext,
    capability: Capability,
) -> None:
    decision = check_capability(
        rbac=_gateway_rbac(request),
        roles=auth.roles,
        permissions=auth.permissions,
        capability=capability,
    )
    if decision.allowed:
        return
    _schedule_permission_denied_event(request, auth, capability)
    raise HTTPException(
        status_code=403,
        detail=build_permission_denied_detail(
            capability=capability,
            trace_id=_request_trace_id(request),
        ),
    )


def _schedule_permission_denied_event(
    request: Request,
    auth: AuthContext,
    capability: Capability,
) -> None:
    try:
        from ..services.metrics import get_security_event_recorder

        recorder = get_security_event_recorder()
        service_id = _extract_service_id_from_path(request.url.path) if hasattr(request, "url") else None
        metadata = {
            "trace_id": _request_trace_id(request),
            "request_id": str(getattr(request.state, "request_id", "") or ""),
            "required_capability": capability.value,
            "status": "denied",
        }
        task = asyncio.create_task(
            recorder.record_event(
                tenant_id=auth.tenant_id or "public",
                user_id=auth.user_id or None,
                service_id=service_id,
                event_type="auth_failed",
                metadata=metadata,
            )
        )
        task.add_done_callback(lambda done: done.exception())
    except Exception:
        return


def _derive_api_key_user_id(api_key: str) -> str:
    digest = hashlib.sha256(api_key.encode("utf-8")).hexdigest()
    return f"apikey:{digest[:16]}"


def _normalize_roles(raw_roles) -> list[str]:
    if not raw_roles:
        return ["user"]
    if isinstance(raw_roles, str):
        return [raw_roles]
    if isinstance(raw_roles, list):
        return [str(r) for r in raw_roles if r is not None]
    return ["user"]


def _normalize_permissions(raw_permissions) -> list[str]:
    if not raw_permissions:
        return []
    if isinstance(raw_permissions, str):
        return [raw_permissions]
    if isinstance(raw_permissions, list):
        return [str(p) for p in raw_permissions if p is not None]
    return []


def _extract_service_id_from_path(path: str) -> str | None:
    for prefix in ("/api/v1/proxy/", "/proxy/"):
        if path.startswith(prefix):
            remainder = path[len(prefix) :]
            if not remainder:
                return None
            return remainder.split("/", 1)[0] or None
    return None


async def _record_auth_failure(
    request: Request,
    user_id: str | None,
    tenant_id: str | None,
) -> None:
    try:
        if getattr(request.state, "_auth_failure_recorded", False):
            return
        request.state._auth_failure_recorded = True
        from ..services.metrics import get_security_event_recorder

        recorder = get_security_event_recorder()
        service_id = _extract_service_id_from_path(request.url.path)
        await recorder.record_event(
            tenant_id=tenant_id or "public",
            user_id=user_id,
            service_id=service_id,
            event_type="auth_failed",
        )
    except Exception:
        pass


def _cache_auth_resolution(
    request: Request,
    *,
    auth_type: str,
    user_id: str,
    tenant_id: str,
    roles: list[str],
    permissions: list[str] | None,
    authenticated: bool,
) -> None:
    request.state._auth_resolution = {
        "auth_type": auth_type,
        "user_id": user_id,
        "tenant_id": tenant_id,
        "roles": list(roles),
        "permissions": list(permissions or []),
        "authenticated": bool(authenticated),
    }


def _build_auth_context_from_resolution(
    payload: dict[str, Any],
    *,
    include_unauthenticated: bool = False,
) -> AuthContext | None:
    if not isinstance(payload, dict):
        return None
    authenticated = bool(payload.get("authenticated"))
    if not authenticated and not include_unauthenticated:
        return None
    return AuthContext(
        user_id=str(payload.get("user_id") or ""),
        tenant_id=str(payload.get("tenant_id") or ""),
        roles=[str(r) for r in list(payload.get("roles") or [])],
        permissions=[str(p) for p in list(payload.get("permissions") or [])],
        is_authenticated=authenticated,
    )


async def get_user_context(
    request: Request,
    settings: Settings = Depends(get_settings),
) -> UserContext:
    """User context for request scoping (JWT / API key / anonymous cookie).

    Results are cached at request level to avoid redundant auth operations.
    """
    # Request-level cache: return cached result if available
    cached = getattr(request.state, "_cached_user_context", None)
    if cached is not None:
        return cached

    t_start = time.perf_counter()
    client_ip = _get_client_ip(request)
    auth_cfg = settings.authentication

    def _cache_and_return(ctx: UserContext) -> UserContext:
        """Cache the context in request.state and return it."""
        request.state._cached_user_context = ctx
        return ctx

    # If auth is disabled, treat as guest (secure default).
    if not auth_cfg.jwt.enabled and not auth_cfg.api_key.enabled:
        logger.debug(
            f"[AUTH][TIMING] auth_disabled total={((time.perf_counter() - t_start) * 1000):.1f}ms"
        )
        _cache_auth_resolution(
            request,
            auth_type="disabled",
            user_id="guest",
            tenant_id="public",
            roles=["guest"],
            permissions=[],
            authenticated=False,
        )
        return _cache_and_return(
            UserContext(
                user_id="guest",
                tenant_id="public",
                tier="anonymous",
                is_authenticated=False,
                ip=client_ip,
                roles=["guest"],
            )
        )

    # 1) JWT (Bearer)
    auth_header = request.headers.get("Authorization") or ""
    if auth_cfg.jwt.enabled and auth_header.lower().startswith("bearer "):
        t_jwt_start = time.perf_counter()
        token = auth_header.split(" ", 1)[1].strip()

        # Use unified JWT config for consistent secret/algorithms
        jwt_secret = get_jwt_secret(auth_cfg.jwt.secret)
        jwt_algorithms = get_jwt_algorithms(auth_cfg.jwt.algorithms)

        try:
            payload = decode_jwt_token(
                token,
                secret=jwt_secret,
                algorithms=jwt_algorithms,
                audience=auth_cfg.jwt.audience,
                issuer=auth_cfg.jwt.issuer,
            )
        except AuthError:
            await _record_auth_failure(request, None, None)
            raise
        t_jwt_decode = time.perf_counter()
        user_id = str(payload.get("sub") or payload.get("user_id") or "")
        tenant_id = str(payload.get("tenant_id") or payload.get("tenant") or "")
        if not user_id:
            await _record_auth_failure(request, None, tenant_id)
            raise AuthError("Missing user_id in JWT token")

        # Check token revocation in Redis (if available)
        t_redis_start = time.perf_counter()
        token_id = payload.get("jti")
        if token_id:
            redis = getattr(request.app.state, "redis", None)
            if redis and getattr(redis, "enabled", False):
                is_valid = await redis.validate_token(token_id)
                if not is_valid:
                    logger.warning(f"Token revoked for user {user_id}")
                    await _record_auth_failure(request, user_id, tenant_id)
                    raise AuthError("Token has been revoked")
        t_redis_done = time.perf_counter()

        roles = _normalize_roles(payload.get("roles") or payload.get("role"))
        permissions = _normalize_permissions(payload.get("permissions"))
        tier = str(payload.get("tier") or "normal")
        if "admin" in roles:
            tier = "admin"
        elif "enterprise" in roles:
            tier = "enterprise"
        elif "premium" in roles or "vip" in roles:
            tier = "premium"

        # Merge permissions from DB (consistent with get_auth_context)
        t_db_start = time.perf_counter()
        db = getattr(request.app.state, "database", None)
        if db and getattr(db, "enabled", False):
            try:
                db_permissions = await db.get_user_permissions(user_id)
                for perm in db_permissions:
                    if perm not in permissions:
                        permissions.append(perm)
                    if perm not in roles:
                        roles.append(perm)
            except Exception as e:
                logger.warning(f"[AUTH] Failed to fetch DB permissions for user {user_id}: {e}")
        t_db_done = time.perf_counter()

        logger.info(
            f"[AUTH][TIMING] JWT user={user_id} "
            f"decode={((t_jwt_decode - t_jwt_start) * 1000):.1f}ms "
            f"redis={((t_redis_done - t_redis_start) * 1000):.1f}ms "
            f"db_perms={((t_db_done - t_db_start) * 1000):.1f}ms "
            f"total={((time.perf_counter() - t_start) * 1000):.1f}ms"
        )
        _cache_auth_resolution(
            request,
            auth_type="jwt",
            user_id=user_id,
            tenant_id=tenant_id,
            roles=roles,
            permissions=permissions,
            authenticated=True,
        )
        return _cache_and_return(
            UserContext(
                user_id=user_id,
                tenant_id=tenant_id,
                tier=tier,
                is_authenticated=True,
                ip=client_ip,
                roles=roles,
            )
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
                    key_permissions = _normalize_permissions(key_info.get("permissions"))
                    merged_permissions = list(key_permissions)
                    tenant_id = str(key_info.get("tenant_id") or "")
                    user_id = str(key_info.get("user_id") or "") or _derive_api_key_user_id(api_key)
                    tier = str(key_info.get("tier") or "normal")

                    # Cache API key metadata for downstream auth decisions (e.g., allowed_services).
                    request.state.api_key_info = key_info
                    request.state.api_key_hash = key_hash
                    request.state.api_key_permissions = key_permissions

                    for perm in key_permissions:
                        if perm not in roles:
                            roles.append(perm)

                    # Merge permissions from DB (consistent with JWT path)
                    try:
                        db_permissions = await db.get_user_permissions(user_id)
                        for perm in db_permissions:
                            if perm not in merged_permissions:
                                merged_permissions.append(perm)
                            if perm not in roles:
                                roles.append(perm)
                    except Exception as e:
                        logger.warning(
                            f"[AUTH] Failed to fetch DB permissions for API key user {user_id}: {e}"
                        )

                    logger.info(
                        f"[AUTH][TIMING] API_KEY user={user_id} total={((time.perf_counter() - t_start) * 1000):.1f}ms"
                    )
                    _cache_auth_resolution(
                        request,
                        auth_type="api_key",
                        user_id=user_id,
                        tenant_id=tenant_id,
                        roles=roles,
                        permissions=merged_permissions,
                        authenticated=True,
                    )
                    return _cache_and_return(
                        UserContext(
                            user_id=user_id,
                            tenant_id=tenant_id,
                            tier=tier,
                            is_authenticated=True,
                            ip=client_ip,
                            roles=roles,
                        )
                    )

            # Fallback to static allowlist (env-configured keys)
            try:
                verify_api_key(api_key, auth_cfg.api_key.keys)
            except AuthError:
                await _record_auth_failure(request, _derive_api_key_user_id(api_key), None)
                raise
            logger.info(
                f"[AUTH][TIMING] API_KEY_STATIC total={((time.perf_counter() - t_start) * 1000):.1f}ms"
            )

            # Validate static role against whitelist for security
            ALLOWED_STATIC_ROLES = frozenset(["user", "admin", "operator", "developer"])
            static_role = (
                os.getenv("GATEWAY_AUTHENTICATION__API_KEY__STATIC_ROLE", "user").strip() or "user"
            )
            if static_role not in ALLOWED_STATIC_ROLES:
                logger.warning(f"Invalid static role '{static_role}', falling back to 'user'")
                static_role = "user"

            static_user_id = _derive_api_key_user_id(api_key)
            _cache_auth_resolution(
                request,
                auth_type="api_key_static",
                user_id=static_user_id,
                tenant_id="",
                roles=[static_role],
                permissions=[],
                authenticated=True,
            )
            return _cache_and_return(
                UserContext(
                    user_id=static_user_id,
                    tenant_id="",
                    tier="admin" if static_role == "admin" else "normal",
                    is_authenticated=True,
                    ip=client_ip,
                    roles=[static_role],
                )
            )

    # 3) Anonymous (stable ID minted by middleware)
    anon_id = getattr(getattr(request, "state", None), "anonymous_id", None) or client_ip
    logger.debug(f"[AUTH][TIMING] anonymous total={((time.perf_counter() - t_start) * 1000):.1f}ms")
    _cache_auth_resolution(
        request,
        auth_type="anonymous",
        user_id=f"anon:{anon_id}",
        tenant_id="public",
        roles=["guest"],
        permissions=[],
        authenticated=False,
    )
    return _cache_and_return(
        UserContext(
            user_id=f"anon:{anon_id}",
            tenant_id="public",
            tier="anonymous",
            is_authenticated=False,
            ip=client_ip,
            roles=["guest"],
        )
    )


async def get_auth_context(
    request: Request, settings: Settings = Depends(get_settings)
) -> AuthContext:
    """Get authentication context for RBAC.

    Results are cached in request.state.auth to avoid redundant auth operations.
    """
    # Request-level cache: return cached result if available
    cached = getattr(request.state, "auth", None)
    if cached is not None and isinstance(cached, AuthContext):
        return cached

    auth_cfg = settings.authentication
    if not auth_cfg.jwt.enabled and not auth_cfg.api_key.enabled:
        ctx = AuthContext(
            user_id="guest",
            tenant_id="public",
            roles=["guest"],
            permissions=[],
            is_authenticated=False,
        )
        request.state.auth = ctx
        return ctx

    auth_resolution = getattr(request.state, "_auth_resolution", None)
    if auth_resolution is None:
        await get_user_context(request=request, settings=settings)
        auth_resolution = getattr(request.state, "_auth_resolution", None)
    resolved_ctx = _build_auth_context_from_resolution(
        auth_resolution,
        include_unauthenticated=not auth_cfg.api_key.enabled,
    )
    if resolved_ctx is not None:
        request.state.auth = resolved_ctx
        return resolved_ctx

    # Unauthenticated requests default to a guest role.
    roles: list[str] = ["guest"]
    permissions: list[str] = []
    user_id = ""
    tenant_id = ""

    auth_header = request.headers.get("Authorization")
    if auth_cfg.jwt.enabled and auth_header and auth_header.lower().startswith("bearer "):
        token = auth_header.split(" ", 1)[1]

        # Use unified JWT config for consistent secret/algorithms
        jwt_secret = get_jwt_secret(auth_cfg.jwt.secret)
        jwt_algorithms = get_jwt_algorithms(auth_cfg.jwt.algorithms)

        try:
            payload = decode_jwt_token(
                token,
                secret=jwt_secret,
                algorithms=jwt_algorithms,
                audience=auth_cfg.jwt.audience,
                issuer=auth_cfg.jwt.issuer,
            )
        except AuthError:
            await _record_auth_failure(request, None, None)
            raise
        user_id = str(payload.get("sub") or payload.get("user_id") or "")
        tenant_id = str(payload.get("tenant_id") or "")
        if not user_id:
            await _record_auth_failure(request, None, tenant_id)
            raise AuthError("Missing user_id in JWT token")

        # Check token revocation in Redis (if available)
        token_id = payload.get("jti")
        if token_id:
            redis = getattr(request.app.state, "redis", None)
            if redis and getattr(redis, "enabled", False):
                is_valid = await redis.validate_token(token_id)
                if not is_valid:
                    logger.warning(f"Token revoked for user {user_id}")
                    await _record_auth_failure(request, user_id, tenant_id)
                    raise AuthError("Token has been revoked")
        raw_roles = payload.get("roles") or payload.get("role") or ["user"]
        if isinstance(raw_roles, str):
            roles = [raw_roles]
        elif isinstance(raw_roles, list):
            roles = [str(r) for r in raw_roles]
        permissions = _normalize_permissions(payload.get("permissions"))

        # Merge permissions from DB if available (keeps JWT small when missing)
        db = getattr(request.app.state, "database", None)
        if db and getattr(db, "enabled", False) and user_id:
            try:
                db_permissions = await db.get_user_permissions(user_id)
                for perm in db_permissions:
                    if perm not in permissions:
                        permissions.append(perm)
            except Exception as e:
                logger.warning(
                    f"[AUTH] Failed to fetch DB permissions in auth_context for user {user_id}: {e}"
                )

        # Merge permissions into roles so RBAC can honor them directly.
        for perm in permissions:
            if perm not in roles:
                roles.append(perm)

        ctx = AuthContext(
            user_id=user_id,
            tenant_id=tenant_id,
            roles=roles,
            permissions=permissions,
            is_authenticated=True,
        )
        _cache_auth_resolution(
            request,
            auth_type="jwt",
            user_id=user_id,
            tenant_id=tenant_id,
            roles=roles,
            permissions=permissions,
            authenticated=True,
        )
        request.state.auth = ctx
        return ctx

    if auth_cfg.api_key.enabled:
        key = request.headers.get(auth_cfg.api_key.header_name)
        if not key:
            await _record_auth_failure(request, None, None)
            raise AuthError("Missing API key")

        db = getattr(request.app.state, "database", None)
        key_hash = hashlib.sha256(key.encode("utf-8")).hexdigest()
        if db and getattr(db, "enabled", False):
            # Reuse cached api_key_info from get_user_context to avoid duplicate use_count increment
            cached_key_info = getattr(request.state, "api_key_info", None)
            cached_key_hash = getattr(request.state, "api_key_hash", None)
            if cached_key_info and cached_key_hash == key_hash:
                key_info = cached_key_info
            else:
                key_info = await db.get_api_key(key_hash)
            if key_info:
                roles = _normalize_roles(key_info.get("roles"))
                key_permissions = _normalize_permissions(key_info.get("permissions"))
                tenant_id = str(key_info.get("tenant_id") or "")
                user_id = str(key_info.get("user_id") or "") or _derive_api_key_user_id(key)
                permissions = []

                for perm in key_permissions:
                    if perm not in permissions:
                        permissions.append(perm)
                    if perm not in roles:
                        roles.append(perm)
                try:
                    db_permissions = await db.get_user_permissions(user_id)
                    for perm in db_permissions:
                        if perm not in permissions:
                            permissions.append(perm)
                        if perm not in roles:
                            roles.append(perm)
                except Exception as e:
                    logger.warning(
                        f"[AUTH] Failed to fetch DB permissions in auth_context for API key user {user_id}: {e}"
                    )

                ctx = AuthContext(
                    user_id=user_id,
                    tenant_id=tenant_id,
                    roles=roles,
                    permissions=permissions,
                    is_authenticated=True,
                )
                _cache_auth_resolution(
                    request,
                    auth_type="api_key",
                    user_id=user_id,
                    tenant_id=tenant_id,
                    roles=roles,
                    permissions=permissions,
                    authenticated=True,
                )
                request.state.auth = ctx
                return ctx

        try:
            verify_api_key(key, auth_cfg.api_key.keys)
        except AuthError:
            await _record_auth_failure(request, _derive_api_key_user_id(key), tenant_id)
            raise
        ctx = AuthContext(
            user_id=_derive_api_key_user_id(key),
            tenant_id="",
            roles=["user"],
            permissions=[],
            is_authenticated=True,
        )
        _cache_auth_resolution(
            request,
            auth_type="api_key_static",
            user_id=ctx.user_id,
            tenant_id="",
            roles=ctx.roles,
            permissions=[],
            authenticated=True,
        )
        request.state.auth = ctx
        return ctx

    # 允许匿名访问（返回空的 AuthContext）
    ctx = AuthContext()
    _cache_auth_resolution(
        request,
        auth_type="anonymous",
        user_id=ctx.user_id,
        tenant_id=ctx.tenant_id,
        roles=ctx.roles,
        permissions=ctx.permissions,
        authenticated=False,
    )
    request.state.auth = ctx
    return ctx
