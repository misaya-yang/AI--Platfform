from __future__ import annotations

import hashlib
import logging
import time
from typing import List, Optional

from fastapi import Depends, HTTPException, Request
from pydantic import BaseModel

from ..config.settings import Settings
from ..core.auth.api_key import verify_api_key
from ..core.auth.jwt import decode_jwt_token
from ..core.auth.jwt_config import get_jwt_secret, get_jwt_algorithms
from ..core.auth.user_resolver import UserContext
from ..core.exceptions import AuthError
from ..core.gateway.multi_dimension_rate_limiter import MultiDimensionRateLimiter
from ..adapters.langgraph_proxy import LangGraphProxy

logger = logging.getLogger(__name__)


class AuthContext(BaseModel):
    user_id: str = ""
    tenant_id: str = ""
    roles: List[str] = ["guest"]
    permissions: List[str] = []


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

def get_knowledge_service(request: Request):
    """Get KnowledgeService (KBMS)."""
    svc = getattr(request.app.state, "knowledge_service", None)
    if svc is None:
        raise HTTPException(
            status_code=503,
            detail="Knowledge service is not initialized (check GATEWAY_KNOWLEDGE__ENABLED and Qdrant settings).",
        )
    return svc

def get_knowledge_worker(request: Request):
    """Get KnowledgeWorker (KBMS)."""
    worker = getattr(request.app.state, "knowledge_worker", None)
    if worker is None:
        raise HTTPException(
            status_code=503,
            detail="Knowledge worker is not initialized (check GATEWAY_KNOWLEDGE__ENABLED).",
        )
    return worker

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


def _extract_service_id_from_path(path: str) -> Optional[str]:
    for prefix in ("/api/v1/proxy/", "/proxy/"):
        if path.startswith(prefix):
            remainder = path[len(prefix):]
            if not remainder:
                return None
            return remainder.split("/", 1)[0] or None
    return None


async def _record_auth_failure(
    request: Request,
    user_id: Optional[str],
    tenant_id: Optional[str],
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
        logger.debug(f"[AUTH][TIMING] auth_disabled total={((time.perf_counter() - t_start) * 1000):.1f}ms")
        return _cache_and_return(UserContext(
            user_id="guest",
            tenant_id="public",
            tier="anonymous",
            is_authenticated=False,
            ip=client_ip,
            roles=["guest"],
        ))

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
        return _cache_and_return(UserContext(
            user_id=user_id,
            tenant_id=tenant_id,
            tier=tier,
            is_authenticated=True,
            ip=client_ip,
            roles=roles,
        ))

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

                    # Cache API key metadata for downstream auth decisions (e.g., allowed_services).
                    request.state.api_key_info = key_info
                    request.state.api_key_hash = key_hash

                    # Merge permissions from DB (consistent with JWT path)
                    try:
                        db_permissions = await db.get_user_permissions(user_id)
                        for perm in db_permissions:
                            if perm not in roles:
                                roles.append(perm)
                    except Exception as e:
                        logger.warning(f"[AUTH] Failed to fetch DB permissions for API key user {user_id}: {e}")

                    logger.info(f"[AUTH][TIMING] API_KEY user={user_id} total={((time.perf_counter() - t_start) * 1000):.1f}ms")
                    return _cache_and_return(UserContext(
                        user_id=user_id,
                        tenant_id=tenant_id,
                        tier=tier,
                        is_authenticated=True,
                        ip=client_ip,
                        roles=roles,
                    ))

            # Fallback to static allowlist (env-configured keys)
            try:
                verify_api_key(api_key, auth_cfg.api_key.keys)
            except AuthError:
                await _record_auth_failure(request, _derive_api_key_user_id(api_key), None)
                raise
            logger.info(f"[AUTH][TIMING] API_KEY_STATIC total={((time.perf_counter() - t_start) * 1000):.1f}ms")
            return _cache_and_return(UserContext(
                user_id=_derive_api_key_user_id(api_key),
                tenant_id="",
                tier="normal",
                is_authenticated=True,
                ip=client_ip,
                roles=["user"],
            ))

    # 3) Anonymous (stable ID minted by middleware)
    anon_id = getattr(getattr(request, "state", None), "anonymous_id", None) or client_ip
    logger.debug(f"[AUTH][TIMING] anonymous total={((time.perf_counter() - t_start) * 1000):.1f}ms")
    return _cache_and_return(UserContext(
        user_id=f"anon:{anon_id}",
        tenant_id="public",
        tier="anonymous",
        is_authenticated=False,
        ip=client_ip,
        roles=["guest"],
    ))


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
        ctx = AuthContext(user_id="guest", tenant_id="public", roles=["guest"], permissions=[])
        request.state.auth = ctx
        return ctx

    # Unauthenticated requests default to a guest role.
    roles: List[str] = ["guest"]
    permissions: List[str] = []
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
        raw_permissions = payload.get("permissions") or []
        if isinstance(raw_permissions, str):
            permissions = [raw_permissions]
        elif isinstance(raw_permissions, list):
            permissions = [str(p) for p in raw_permissions]

        # Merge permissions from DB if available (keeps JWT small when missing)
        db = getattr(request.app.state, "database", None)
        if db and getattr(db, "enabled", False) and user_id:
            try:
                db_permissions = await db.get_user_permissions(user_id)
                for perm in db_permissions:
                    if perm not in permissions:
                        permissions.append(perm)
            except Exception as e:
                logger.warning(f"[AUTH] Failed to fetch DB permissions in auth_context for user {user_id}: {e}")

        # Merge permissions into roles so RBAC can honor them directly.
        for perm in permissions:
            if perm not in roles:
                roles.append(perm)

        ctx = AuthContext(user_id=user_id, tenant_id=tenant_id, roles=roles, permissions=permissions)
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
                tenant_id = str(key_info.get("tenant_id") or "")
                user_id = str(key_info.get("user_id") or "") or _derive_api_key_user_id(key)
                ctx = AuthContext(user_id=user_id, tenant_id=tenant_id, roles=roles, permissions=[])
                request.state.auth = ctx
                return ctx

        try:
            verify_api_key(key, auth_cfg.api_key.keys)
        except AuthError:
            await _record_auth_failure(request, _derive_api_key_user_id(key), tenant_id)
            raise
        ctx = AuthContext(user_id=_derive_api_key_user_id(key), tenant_id="", roles=["user"], permissions=[])
        request.state.auth = ctx
        return ctx

    # 允许匿名访问（返回空的 AuthContext）
    ctx = AuthContext()
    request.state.auth = ctx
    return ctx
