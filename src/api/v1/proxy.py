"""
透明代理路由

提供通配符路由 /proxy/{service_name}/{path:path}，支持：
- 动态路由转发
- SSE 流式传输
- 鉴权和限流
- 上下文注入
"""

from __future__ import annotations

import asyncio
import contextlib
import json
import time
from typing import Any

from fastapi import APIRouter, Depends, HTTPException, Request, Response
from fastapi.responses import StreamingResponse

from ...core.auth.permissions import (
    Capability,
    build_permission_denied_detail,
    check_capability,
)
from ...core.auth.service_access import (
    ServiceAccessPolicy,
    evaluate_service_access,
    normalize_service_scope,
    service_access_policy_from_metadata,
    service_scope_matches,
)
from ...core.auth.user_resolver import UserContext
from ...core.gateway.multi_dimension_rate_limiter import (
    MultiDimensionRateLimiter,
    RateLimitContext,
    RateLimitHeaders,
)
from ...core.observability.logging import get_logger
from ...core.observability.metrics import get_metrics
from ...core.utils import estimate_tokens
from ...proxy import (
    ProxyConfigLoader,
    ProxyRequest,
    ProxyServiceConfig,
    RequestContext,
    TransparentProxy,
)
from ...services.billing import get_quota_service
from ...services.billing.quota_service import OverageStrategy, QuotaStatus
from ...services.metrics.usage_parser import extract_model
from ..deps import (
    AuthContext,
    get_auth_context,
    get_rate_limiter,
    get_user_context,
)

logger = get_logger(__name__)

router = APIRouter(prefix="/proxy", tags=["Transparent Proxy"])


async def _record_security_event(
    event_type: str,
    tenant_id: str,
    user_id: str,
    service_id: str | None,
    metadata: dict[str, Any] | None = None,
) -> None:
    try:
        from ...services.metrics import get_security_event_recorder

        recorder = get_security_event_recorder()
        await recorder.record_event(
            tenant_id=tenant_id or "public",
            user_id=user_id or None,
            service_id=service_id,
            event_type=event_type,
            metadata=metadata,
        )
    except Exception:
        pass


def _current_trace_id(request: Request) -> str:
    request_id = getattr(request.state, "request_id", "")
    if isinstance(request_id, str) and request_id:
        return request_id
    trace_id = getattr(request.state, "trace_id", "")
    if isinstance(trace_id, str):
        return trace_id
    return ""


def _record_rate_limit_decision(dimension: str, service_name: str, allowed: bool) -> None:
    try:
        metrics = get_metrics()
        result = "allowed" if allowed else "blocked"
        if hasattr(metrics.request_metrics, "record_rate_limit_decision"):
            metrics.request_metrics.record_rate_limit_decision(
                dimension=dimension,
                service_id=service_name,
                result=result,
            )
        if not allowed:
            metrics.request_metrics.record_rate_limit(
                dimension=dimension,
                service_id=service_name,
            )
    except Exception:
        # Metrics must not break proxy path.
        pass


# ============ 依赖注入 ============


def get_transparent_proxy(request: Request) -> TransparentProxy:
    """获取透明代理实例"""
    proxy = getattr(request.app.state, "transparent_proxy", None)
    if proxy is None:
        raise HTTPException(
            status_code=503,
            detail="Transparent proxy is not initialized",
        )
    return proxy


def get_proxy_config_loader(request: Request) -> ProxyConfigLoader:
    """获取代理配置加载器"""
    loader = getattr(request.app.state, "proxy_config_loader", None)
    if loader is None:
        raise HTTPException(
            status_code=503,
            detail="Proxy config loader is not initialized",
        )
    return loader


# ============ 权限和限流检查 ============


def _normalize_allowed_services(value: Any) -> list[str]:
    return normalize_service_scope(value)


def _normalize_allowed_models(value: Any) -> list[str]:
    if not value:
        return []
    if isinstance(value, str):
        return [value.strip()] if value.strip() else []
    if isinstance(value, list):
        return [str(v).strip() for v in value if str(v).strip()]
    return []


def _is_model_allowed(allowed_models: list[str], model: str) -> bool:
    if not allowed_models:
        return True
    normalized_model = (model or "").strip().lower()
    if not normalized_model:
        return False
    for allowed in allowed_models:
        normalized_allowed = allowed.strip().lower()
        if not normalized_allowed:
            continue
        if normalized_allowed == "*":
            return True
        if normalized_allowed.endswith("*"):
            if normalized_model.startswith(normalized_allowed[:-1]):
                return True
            continue
        if normalized_model == normalized_allowed:
            return True
    return False


def _estimate_tokens_from_payload(payload: Any) -> int:
    """Best-effort request token estimate for pre-quota checks."""
    if payload is None:
        return 0
    if isinstance(payload, str):
        return estimate_tokens(payload)
    if isinstance(payload, (list, dict)):
        text_like_keys = {
            "prompt",
            "query",
            "question",
            "instruction",
            "text",
            "content",
            "message",
            "messages",
            "input",
        }

        def _walk(node: Any, parent_key: str = "") -> int:
            if isinstance(node, str):
                return estimate_tokens(node) if parent_key in text_like_keys else 0
            if isinstance(node, list):
                return sum(_walk(item, parent_key) for item in node)
            if isinstance(node, dict):
                subtotal = 0
                for key, value in node.items():
                    key_name = str(key).strip().lower()
                    if isinstance(value, str):
                        if key_name in text_like_keys:
                            subtotal += estimate_tokens(value)
                    else:
                        subtotal += _walk(value, key_name)
                return subtotal
            return 0

        total = _walk(payload)
    else:
        total = 0

    if total <= 0:
        # Conservative fallback to avoid bypassing quota on unknown payload structures.
        raw_size = len(json.dumps(payload, ensure_ascii=False))
        total = max(raw_size // 4, 1)
    return total


def _override_model_in_request_payload(payload: Any, model: str) -> Any:
    if not isinstance(payload, dict):
        return payload
    updated = dict(payload)
    updated["model"] = model
    if isinstance(updated.get("input"), dict):
        input_payload = dict(updated["input"])
        input_payload.setdefault("model", model)
        updated["input"] = input_payload
    if isinstance(updated.get("config"), dict):
        config_payload = dict(updated["config"])
        configurable = config_payload.get("configurable")
        if isinstance(configurable, dict):
            new_configurable = dict(configurable)
            new_configurable["model"] = model
            config_payload["configurable"] = new_configurable
        updated["config"] = config_payload
    return updated


def _decode_json_body(body: bytes | None) -> Any | None:
    if not body:
        return None
    try:
        return json.loads(body.decode("utf-8"))
    except Exception:
        return None


def _encode_json_body(payload: Any) -> bytes | None:
    if payload is None:
        return None
    return json.dumps(payload, ensure_ascii=False).encode("utf-8")


def _should_apply_quota_policy(method: str, operation: str, path: str) -> bool:
    if method.upper() not in {"POST", "PUT", "PATCH"}:
        return False
    if operation.startswith("run_"):
        return True
    normalized_path = path.lower()
    return "/runs" in normalized_path


async def _enforce_model_allowlist(
    request: Request,
    service_name: str,
    user: UserContext,
    auth: AuthContext,
    model: str | None,
) -> None:
    api_key_info = getattr(request.state, "api_key_info", None)
    if not api_key_info:
        return
    allowed_models = _normalize_allowed_models(api_key_info.get("allowed_models"))
    if not allowed_models:
        return
    resolved_model = (model or "").strip()
    if _is_model_allowed(allowed_models, resolved_model):
        return

    await _record_security_event(
        event_type="auth_failed",
        tenant_id=auth.tenant_id or user.tenant_id or "public",
        user_id=user.user_id or auth.user_id or "anonymous",
        service_id=service_name,
    )
    raise HTTPException(
        status_code=403,
        detail=f"Permission denied: model '{resolved_model or '<empty>'}' not allowed for this API key",
    )


async def _apply_quota_policy(
    *,
    request: Request,
    user: UserContext,
    auth: AuthContext,
    service_name: str,
    operation: str,
    path: str,
    body: bytes | None,
    model_hint: str | None,
) -> tuple[bytes | None, str | None]:
    """
    Enforce quota governance policy before proxying.

    Returns:
        (possibly mutated body, final model to use)
    """
    if not _should_apply_quota_policy(request.method, operation, path):
        return body, model_hint

    quota_service = get_quota_service()
    if not quota_service or not quota_service.database:
        return body, model_hint

    payload = _decode_json_body(body)
    estimated_tokens = _estimate_tokens_from_payload(payload)

    try:
        check = await quota_service.check_quota(
            tenant_id=auth.tenant_id or user.tenant_id or "default",
            user_id=user.user_id or auth.user_id or "anonymous",
            estimated_tokens=estimated_tokens,
        )
    except Exception as exc:
        logger.warning(f"[ProxyQuota] quota check failed, skipping enforcement: {exc}")
        return body, model_hint

    if check.status == QuotaStatus.BLOCKED:
        await _record_security_event(
            event_type="quota_exceeded",
            tenant_id=auth.tenant_id or user.tenant_id,
            user_id=user.user_id,
            service_id=service_name,
        )
        raise HTTPException(
            status_code=403,
            detail={
                "message": check.message,
                "status": check.status.value,
                "policy": check.overage_strategy.value,
            },
        )

    if not check.can_proceed:
        status_code = 429 if check.overage_strategy == OverageStrategy.RATE_LIMIT else 403
        await _record_security_event(
            event_type="quota_exceeded",
            tenant_id=auth.tenant_id or user.tenant_id,
            user_id=user.user_id,
            service_id=service_name,
        )
        raise HTTPException(
            status_code=status_code,
            detail={
                "message": check.message,
                "status": check.status.value,
                "policy": check.overage_strategy.value,
            },
            headers={"Retry-After": "60"} if status_code == 429 else None,
        )

    final_model = (model_hint or "").strip() or None
    if check.status == QuotaStatus.EXCEEDED:
        await _record_security_event(
            event_type="quota_exceeded",
            tenant_id=auth.tenant_id or user.tenant_id,
            user_id=user.user_id,
            service_id=service_name,
        )
        if check.overage_strategy == OverageStrategy.DOWNGRADE_MODEL and check.downgraded_model:
            downgraded_model = check.downgraded_model.strip()
            if downgraded_model and payload is not None:
                payload = _override_model_in_request_payload(payload, downgraded_model)
                body = _encode_json_body(payload)
            final_model = downgraded_model
        elif check.overage_strategy == OverageStrategy.ALLOW_BUT_ALERT:
            with contextlib.suppress(Exception):
                await quota_service.create_alert(
                    tenant_id=auth.tenant_id or user.tenant_id or "default",
                    user_id=user.user_id or "anonymous",
                    alert_type="quota_allow_but_alert",
                    threshold_value=check.warning_threshold,
                    current_value=check.daily_tokens_used,
                    limit_value=check.daily_tokens_limit or 0,
                    message=check.message,
                )

    return body, final_model


async def _resolve_service_definition(registry, service_name: str):
    if not registry:
        return None
    service = await registry.get(service_name)
    if service:
        return service
    try:
        services = await registry.list()
    except Exception:
        return None
    for svc in services:
        if getattr(svc, "name", None) == service_name:
            return svc
    return None


def _service_allowed(allowed: list[str], candidates: set) -> bool:
    return service_scope_matches(allowed, list(candidates))


async def _load_service_access_constraints(
    request: Request,
    user: UserContext,
) -> tuple[list[tuple[str, list[str]]], ServiceAccessPolicy]:
    """Collect API key / tenant / user-level service access constraints."""
    allowed_sources: list[tuple[str, list[str]]] = []
    user_policy = ServiceAccessPolicy()

    db = getattr(request.app.state, "database", None)
    if not db or not getattr(db, "enabled", False) or not user.is_authenticated:
        return allowed_sources, user_policy

    api_key_info = getattr(request.state, "api_key_info", None)
    if api_key_info:
        api_allowed = _normalize_allowed_services(api_key_info.get("allowed_services"))
        if api_allowed:
            allowed_sources.append(("api_key", api_allowed))

    if user.tenant_id:
        try:
            tenant = await db.get_tenant(user.tenant_id)
            if tenant:
                tenant_allowed = _normalize_allowed_services(tenant.get("allowed_services"))
                if tenant_allowed:
                    allowed_sources.append(("tenant", tenant_allowed))
        except Exception as exc:
            logger.warning(
                "[ProxyAuth] Failed to load tenant allowed_services for tenant %s: %s",
                user.tenant_id,
                exc,
            )

    if user.user_id:
        try:
            user_record = await db.get_user(user.user_id)
            if user_record:
                user_policy = service_access_policy_from_metadata(user_record.get("metadata"))
        except Exception as exc:
            logger.warning(
                "[ProxyAuth] Failed to load user service access policy for user %s: %s",
                user.user_id,
                exc,
            )

    return allowed_sources, user_policy


async def _enforce_service_access_constraints(
    request: Request,
    service_name: str,
    service_aliases: set,
    user: UserContext,
    auth: AuthContext,
) -> None:
    allowed_sources, user_policy = await _load_service_access_constraints(request, user)

    for source, allowed in allowed_sources:
        if _service_allowed(allowed, service_aliases):
            continue
        logger.warning(
            f"[ProxyAuth] Service {service_name} blocked by {source} allowed_services for user {user.user_id}"
        )
        await _record_security_event(
            event_type="auth_failed",
            tenant_id=auth.tenant_id or user.tenant_id,
            user_id=user.user_id,
            service_id=service_name,
            metadata={
                "permission_check": {
                    "scope_source": source,
                    "scope": allowed,
                    "service_aliases": sorted(service_aliases),
                }
            },
        )
        raise HTTPException(
            status_code=403,
            detail=f"Permission denied: service {service_name} not in allowed services",
        )

    if "admin" in auth.roles:
        return

    permitted, reason = evaluate_service_access(user_policy, list(service_aliases))
    if permitted:
        return

    logger.warning(
        f"[ProxyAuth] Service {service_name} blocked by user service_access policy for user {user.user_id}: {reason}"
    )
    await _record_security_event(
        event_type="auth_failed",
        tenant_id=auth.tenant_id or user.tenant_id,
        user_id=user.user_id,
        service_id=service_name,
        metadata={
            "permission_check": {
                "scope_source": "user_policy",
                "reason": reason,
                "policy_mode": user_policy.mode.value,
                "allowed_services": list(user_policy.allowed_services),
                "denied_services": list(user_policy.denied_services),
                "service_aliases": sorted(service_aliases),
            }
        },
    )
    raise HTTPException(
        status_code=403,
        detail=f"Permission denied: service {service_name} blocked by user policy",
    )


async def check_service_authorization(
    request: Request,
    service_name: str,
    user: UserContext,
    auth: AuthContext,
) -> None:
    """
    检查用户对服务的访问权限

    验证顺序:
    1. 基础 capability 权限 (AgentInvoke)
    2. 服务级别认证配置 (allowed_roles, public)
    3. 用户/API Key 级别 allowed_services (如果配置)
    """
    # 1. Capability 检查（兼容 canonical + legacy alias）
    decision = check_capability(
        rbac=request.app.state.dispatcher.rbac,
        roles=auth.roles,
        permissions=auth.permissions,
        capability=Capability.AGENT_INVOKE,
    )
    if not decision.allowed:
        trace_id = _current_trace_id(request)
        await _record_security_event(
            event_type="auth_failed",
            tenant_id=auth.tenant_id or user.tenant_id,
            user_id=user.user_id,
            service_id=service_name,
            metadata={
                "permission_check": {
                    "required_capability": Capability.AGENT_INVOKE.value,
                    "required_permission": decision.required_permission,
                    "accepted_permissions": list(decision.accepted_permissions),
                    "trace_id": trace_id,
                }
            },
        )
        logger.warning(
            f"[ProxyAuth] User {user.user_id} lacks {decision.required_permission} "
            f"for {service_name} trace={trace_id}"
        )
        raise HTTPException(
            status_code=403,
            detail=build_permission_denied_detail(
                capability=Capability.AGENT_INVOKE,
                trace_id=trace_id,
            ),
        )

    # 2. 服务级别认证配置检查
    registry = getattr(request.app.state, "registry", None)
    service = await _resolve_service_definition(registry, service_name)
    service_aliases = {service_name}
    if service:
        request.state.service_id = getattr(service, "service_id", None) or service_name
        if getattr(service, "service_id", None):
            service_aliases.add(service.service_id)
        if getattr(service, "name", None):
            service_aliases.add(service.name)

        service_config = (
            service.get_service_config() if hasattr(service, "get_service_config") else None
        )
        if service_config and service_config.auth and service_config.auth.enabled:
            auth_config = service_config.auth

            # public 服务跳过服务级别检查，但仍需执行 allowed_services 约束
            if not auth_config.public:
                # require_auth
                if auth_config.require_auth and not user.is_authenticated:
                    await _record_security_event(
                        event_type="auth_failed",
                        tenant_id=auth.tenant_id or user.tenant_id,
                        user_id=user.user_id,
                        service_id=getattr(service, "service_id", None) or service_name,
                    )
                    raise HTTPException(
                        status_code=403,
                        detail=f"Permission denied: authentication required for {service_name}",
                    )

                # allowed_roles
                if auth_config.allowed_roles:
                    has_role = any(role in auth.roles for role in auth_config.allowed_roles)
                    # admin 始终有权限
                    if not has_role and "admin" not in auth.roles:
                        logger.warning(
                            f"[ProxyAuth] User {user.user_id} role not in allowed_roles for {service_name}"
                        )
                        await _record_security_event(
                            event_type="auth_failed",
                            tenant_id=auth.tenant_id or user.tenant_id,
                            user_id=user.user_id,
                            service_id=getattr(service, "service_id", None) or service_name,
                        )
                        raise HTTPException(
                            status_code=403,
                            detail=f"Permission denied: not authorized for service {service_name}",
                        )

                # allowed_api_keys (prefix allowlist)
                if auth_config.allowed_api_keys:
                    settings = getattr(request.app.state, "settings", None)
                    api_key_header = None
                    if settings and settings.authentication.api_key.enabled:
                        api_key_header = settings.authentication.api_key.header_name
                    api_key_value = request.headers.get(api_key_header) if api_key_header else None

                    if not api_key_value:
                        await _record_security_event(
                            event_type="auth_failed",
                            tenant_id=auth.tenant_id or user.tenant_id,
                            user_id=user.user_id,
                            service_id=getattr(service, "service_id", None) or service_name,
                        )
                        raise HTTPException(
                            status_code=403,
                            detail=f"Permission denied: API key required for service {service_name}",
                        )

                    if "*" not in auth_config.allowed_api_keys:
                        matched = any(
                            api_key_value.startswith(prefix)
                            for prefix in auth_config.allowed_api_keys
                        )
                        if not matched:
                            await _record_security_event(
                                event_type="auth_failed",
                                tenant_id=auth.tenant_id or user.tenant_id,
                                user_id=user.user_id,
                                service_id=getattr(service, "service_id", None) or service_name,
                            )
                            raise HTTPException(
                                status_code=403,
                                detail=f"Permission denied: API key not allowed for service {service_name}",
                            )

    # 3. 用户/API Key/租户级访问约束
    await _enforce_service_access_constraints(
        request=request,
        service_name=service_name,
        service_aliases=service_aliases,
        user=user,
        auth=auth,
    )


async def check_proxy_rate_limit(
    user: UserContext,
    rate_limiter: MultiDimensionRateLimiter | None,
    service_name: str,
    operation: str = "proxy",
    service_config: ProxyServiceConfig | None = None,
) -> None:
    """检查代理限流"""
    if not rate_limiter:
        return

    service_limit_enabled = bool(
        service_config
        and service_config.rate_limit_enabled
        and int(service_config.rate_limit_requests or 0) > 0
        and int(service_config.rate_limit_window or 0) > 0
    )

    if service_limit_enabled:
        # Service-level limit has higher priority than global multi-dimension policies.
        tenant_scope = (user.tenant_id or "public").strip() or "public"
        subject = (user.user_id or user.ip or "anonymous").strip() or "anonymous"
        safe_operation = str(operation or "proxy").strip() or "proxy"
        key = f"ratelimit:service:{service_name}:{tenant_scope}:{subject}:{safe_operation}"
        result = await rate_limiter.check_custom_limit(
            key=key,
            limit=int(service_config.rate_limit_requests),
            window=int(service_config.rate_limit_window),
            dimension=f"service:{service_name}",
        )
        _record_rate_limit_decision(result.dimension or "service", service_name, result.allowed)
        if not result.allowed:
            await _record_security_event(
                event_type="rate_limited",
                tenant_id=user.tenant_id,
                user_id=user.user_id,
                service_id=service_name,
                metadata={
                    "permission_check": {
                        "dimension": result.dimension,
                        "limit": result.limit,
                        "remaining": result.remaining,
                        "retry_after": result.retry_after,
                    }
                },
            )
            raise HTTPException(
                status_code=429,
                detail=RateLimitHeaders.build_exceeded_response(result),
                headers=RateLimitHeaders.build(result),
            )
        return

    context = RateLimitContext.from_user_context(
        user=user,
        assistant_id=service_name,  # 复用 assistant_id 作为服务标识
        operation=operation,
    )

    result = await rate_limiter.check(context)
    _record_rate_limit_decision(result.dimension or "composite", service_name, result.allowed)
    if not result.allowed:
        await _record_security_event(
            event_type="rate_limited",
            tenant_id=user.tenant_id,
            user_id=user.user_id,
            service_id=service_name,
        )
        raise HTTPException(
            status_code=429,
            detail=RateLimitHeaders.build_exceeded_response(result),
            headers=RateLimitHeaders.build(result),
        )


# ============ 主路由处理 ============


@router.api_route(
    "/{service_name}/{path:path}",
    methods=["GET", "POST", "PUT", "PATCH", "DELETE", "OPTIONS", "HEAD"],
    summary="透明代理",
    description="""
    透明代理路由，将请求转发至目标服务。

    - `service_name`: 服务名称（对应数据库中的 service_id 或 name）
    - `path`: 请求路径（将被转发至上游服务）

    支持：
    - 所有 HTTP 方法
    - SSE 流式响应（自动检测）
    - 请求体透传
    - 查询参数透传
    """,
)
async def transparent_proxy_handler(
    service_name: str,
    path: str,
    request: Request,
    proxy: TransparentProxy = Depends(get_transparent_proxy),
    config_loader: ProxyConfigLoader = Depends(get_proxy_config_loader),
    user: UserContext = Depends(get_user_context),
    auth: AuthContext = Depends(get_auth_context),
    rate_limiter: MultiDimensionRateLimiter | None = Depends(get_rate_limiter),
):
    """
    透明代理主处理函数

    处理流程：
    1. 权限检查（RBAC + 服务级别 + allowed_services）
    2. 限流检查
    3. 提取请求上下文
    4. 构建代理请求
    5. 执行代理并返回响应
    """
    if path == "_health":
        return await proxy_service_health(
            service_name=service_name,
            proxy=proxy,
        )
    if path == "_selftest":
        return await proxy_service_selftest(
            service_name=service_name,
            request=request,
            proxy=proxy,
            config_loader=config_loader,
            user=user,
            auth=auth,
            rate_limiter=rate_limiter,
        )

    # Performance timing
    t_start = time.perf_counter()
    t_auth_done = t_start  # User context already resolved via Depends

    # 1. 权限检查
    await check_service_authorization(request, service_name, user, auth)
    t_auth_check = time.perf_counter()

    # 2. 检测操作类型（用于限流）
    operation = TransparentProxy.detect_operation_type(request.method, path)

    # 3. 读取服务配置（用于限流覆盖、默认模型、策略判定）
    service_config = await config_loader.get_config(service_name)
    t_config = time.perf_counter()

    # 4. 限流检查
    await check_proxy_rate_limit(
        user=user,
        rate_limiter=rate_limiter,
        service_name=service_name,
        operation=operation,
        service_config=service_config,
    )
    t_rate_limit = time.perf_counter()

    # 5. 提取请求上下文
    context = _build_request_context(request, user)
    t_context = time.perf_counter()

    # 6. 读取请求体
    body = await request.body() if request.method in ("POST", "PUT", "PATCH") else None
    t_body = time.perf_counter()

    # 7. 解析请求模型并应用服务默认值
    request_payload = _decode_json_body(body)
    requested_model = extract_model(request_payload)
    if not requested_model and service_config:
        requested_model = service_config.default_model

    # 8. API Key 模型白名单
    await _enforce_model_allowlist(
        request=request,
        service_name=service_name,
        user=user,
        auth=auth,
        model=requested_model,
    )

    # 9. 配额策略预检查（可触发拒绝/降级/告警）
    body, effective_model = await _apply_quota_policy(
        request=request,
        user=user,
        auth=auth,
        service_name=service_name,
        operation=operation,
        path=path,
        body=body,
        model_hint=requested_model,
    )
    if effective_model:
        request.state.effective_model = effective_model
    t_policy = time.perf_counter()

    # 10. 检查是否期望流式响应
    wants_stream = _wants_streaming(request, path)

    # 11. 构建代理请求
    proxy_request = ProxyRequest(
        service_name=service_name,
        path=path,
        method=request.method,
        body=body,
        query_params=dict(request.query_params),
        context=context,
        stream=wants_stream,
    )
    t_build = time.perf_counter()

    # Performance logging
    logger.info(
        f"[ProxyRoute][TIMING] {request.method} /proxy/{service_name}/{path} "
        f"auth_check={((t_auth_check - t_auth_done) * 1000):.1f}ms "
        f"config={((t_config - t_auth_check) * 1000):.1f}ms "
        f"rate_limit={((t_rate_limit - t_config) * 1000):.1f}ms "
        f"context={((t_context - t_rate_limit) * 1000):.1f}ms "
        f"body_read={((t_body - t_context) * 1000):.1f}ms "
        f"policy={((t_policy - t_body) * 1000):.1f}ms "
        f"build={((t_build - t_policy) * 1000):.1f}ms "
        f"total_prep={((t_build - t_start) * 1000):.1f}ms"
    )

    # 10. 执行代理
    logger.info(
        f"[ProxyRoute] {request.method} /proxy/{service_name}/{path} "
        f"user={user.user_id} op={operation} stream={wants_stream}"
    )

    response = await proxy.proxy(proxy_request)
    t_proxy_done = time.perf_counter()
    logger.info(
        f"[ProxyRoute][TIMING] proxy_call={((t_proxy_done - t_build) * 1000):.1f}ms "
        f"total={((t_proxy_done - t_start) * 1000):.1f}ms "
        f"status={response.status_code} streaming={response.is_streaming}"
    )

    # 8. 处理网关内部错误（如服务不存在、配置错误）
    # 注意：上游 4xx/5xx 错误不在此处理，直接透传
    if response.error:
        raise HTTPException(
            status_code=response.status_code,
            detail=response.error,
        )

    # 9. 返回响应（包括上游的 4xx/5xx 错误，原样透传）
    if response.is_streaming and response.stream:
        return StreamingResponse(
            response.stream,
            status_code=response.status_code,
            headers=response.headers,
            media_type="text/event-stream",
        )
    else:
        # 确定 content-type，保留原始响应的 content-type
        content_type = response.headers.get("content-type", "application/json")

        # 错误透传：即使是 4xx/5xx，也原样返回上游的响应内容
        return Response(
            content=response.body or b"",
            status_code=response.status_code,
            headers=response.headers,
            media_type=content_type,
        )


@router.api_route(
    "/{service_name}",
    methods=["GET", "POST", "PUT", "PATCH", "DELETE", "OPTIONS", "HEAD"],
    summary="透明代理（根路径）",
    description="透明代理路由，转发至服务根路径。",
)
async def transparent_proxy_root_handler(
    service_name: str,
    request: Request,
    proxy: TransparentProxy = Depends(get_transparent_proxy),
    config_loader: ProxyConfigLoader = Depends(get_proxy_config_loader),
    user: UserContext = Depends(get_user_context),
    auth: AuthContext = Depends(get_auth_context),
    rate_limiter: MultiDimensionRateLimiter | None = Depends(get_rate_limiter),
):
    """处理根路径请求"""
    return await transparent_proxy_handler(
        service_name=service_name,
        path="",
        request=request,
        proxy=proxy,
        config_loader=config_loader,
        user=user,
        auth=auth,
        rate_limiter=rate_limiter,
    )


# ============ 服务发现端点 ============


@router.get(
    "",
    summary="列出代理服务",
    description="列出所有可用的透明代理服务。",
)
async def list_proxy_services(
    request: Request,
    config_loader: ProxyConfigLoader = Depends(get_proxy_config_loader),
    user: UserContext = Depends(get_user_context),
    auth: AuthContext = Depends(get_auth_context),
):
    """列出可用的代理服务"""
    # 权限检查：需要 AgentInvoke capability（兼容 legacy alias）
    decision = check_capability(
        rbac=request.app.state.dispatcher.rbac,
        roles=auth.roles,
        permissions=auth.permissions,
        capability=Capability.AGENT_INVOKE,
    )
    if not decision.allowed:
        raise HTTPException(
            status_code=403,
            detail=build_permission_denied_detail(
                capability=Capability.AGENT_INVOKE,
                trace_id=_current_trace_id(request),
            ),
        )

    services = await config_loader.list_services()
    is_admin = "admin" in auth.roles
    allowed_sources, user_policy = await _load_service_access_constraints(request, user)

    visible_services: list[dict[str, Any]] = []
    for svc in services:
        aliases = {
            str(getattr(svc, "service_id", "") or "").strip(),
            str(getattr(svc, "service_name", "") or "").strip(),
        }
        aliases = {alias for alias in aliases if alias}

        blocked = False
        for _, allowed in allowed_sources:
            if _service_allowed(allowed, aliases):
                continue
            blocked = True
            break
        if blocked:
            continue

        if not is_admin:
            permitted, _ = evaluate_service_access(user_policy, list(aliases))
            if not permitted:
                continue

        raw_metadata = svc.metadata if isinstance(svc.metadata, dict) else {}
        adapter_type = str(raw_metadata.get("adapter_type") or "").strip().lower()
        inferred_service_type = str(raw_metadata.get("service_type") or "").strip().lower()
        if not inferred_service_type:
            if adapter_type == "langgraph" or bool(
                (svc.assistant_id or "").strip() or (svc.graph_id or "").strip()
            ):
                inferred_service_type = "langgraph"
            else:
                inferred_service_type = "proxy"

        safe_metadata: dict[str, Any] = {}
        effective_adapter_type = adapter_type or (
            "langgraph" if inferred_service_type == "langgraph" else ""
        )
        if effective_adapter_type:
            safe_metadata["adapter_type"] = effective_adapter_type
        proxy_mode = str(raw_metadata.get("proxy_mode") or "").strip().lower()
        if not proxy_mode and inferred_service_type == "langgraph":
            proxy_mode = "transparent"
        if proxy_mode:
            safe_metadata["proxy_mode"] = proxy_mode
        ui_preferences = raw_metadata.get("ui_preferences")
        if isinstance(ui_preferences, dict):
            safe_metadata["ui_preferences"] = dict(ui_preferences)

        visible_services.append(
            {
                "service_id": svc.service_id,
                "service_name": svc.service_name,
                "service_type": inferred_service_type,
                "metadata": safe_metadata,
                # 仅管理员可见完整 URL
                "upstream_url": svc.upstream_url if is_admin else None,
                "assistant_id": svc.assistant_id if is_admin else None,
                "enabled": svc.enabled,
            }
        )

    # 非管理员只能看到基本信息，管理员可以看到完整信息
    return {
        "services": visible_services,
        "count": len(visible_services),
    }


@router.get(
    "/{service_name}/_health",
    summary="服务健康检查",
    description="检查指定服务的健康状态。",
)
async def proxy_service_health(
    service_name: str,
    proxy: TransparentProxy = Depends(get_transparent_proxy),
):
    """检查服务健康状态"""
    healthy, message = await proxy.health_check(service_name)

    if healthy:
        return {"status": "healthy", "service": service_name, "message": message}
    else:
        raise HTTPException(
            status_code=503,
            detail={"status": "unhealthy", "service": service_name, "message": message},
        )


@router.get(
    "/{service_name}/_selftest",
    summary="代理自检",
    description="验证鉴权头透传与 SSE 流式输出是否正常。",
)
async def proxy_service_selftest(
    service_name: str,
    request: Request,
    proxy: TransparentProxy = Depends(get_transparent_proxy),
    config_loader: ProxyConfigLoader = Depends(get_proxy_config_loader),
    user: UserContext = Depends(get_user_context),
    auth: AuthContext = Depends(get_auth_context),
    rate_limiter: MultiDimensionRateLimiter | None = Depends(get_rate_limiter),
):
    # 权限检查：仅管理员可访问 _selftest 端点
    if "admin" not in auth.roles:
        raise HTTPException(status_code=403, detail="Admin access required for selftest endpoint")

    config = await config_loader.get_config(service_name)
    await check_proxy_rate_limit(
        user=user,
        rate_limiter=rate_limiter,
        service_name=service_name,
        operation="proxy_selftest",
        service_config=config,
    )

    context = _build_request_context(request, user)
    auth_present = any(k.lower() == "authorization" for k in request.headers)

    result: dict[str, Any] = {
        "service": service_name,
        "auth_header_present": auth_present,
    }

    if config:
        # 脱敏处理：只显示部分信息，避免暴露敏感配置
        masked_assistant_id = (
            (config.assistant_id[:8] + "...")
            if config.assistant_id and len(config.assistant_id) > 8
            else config.assistant_id
        )
        # 只显示 host 部分
        masked_upstream = None
        if config.upstream_url:
            try:
                from urllib.parse import urlparse

                parsed = urlparse(config.upstream_url)
                masked_upstream = parsed.netloc
            except Exception:
                masked_upstream = "[masked]"
        result.update(
            {
                "assistant_id": masked_assistant_id,
                "upstream_host": masked_upstream,
                "enabled": config.enabled,
            }
        )

    # 1) Basic upstream auth/route check (assistants list)
    # LangGraph /assistants is POST in current Agent Server API.
    list_request = ProxyRequest(
        service_name=service_name,
        path="assistants/search",
        method="POST",
        body=json.dumps({}).encode("utf-8"),
        query_params={},
        context=context,
        stream=False,
    )
    list_response = await proxy.proxy(list_request)
    list_preview = None
    if list_response.body:
        try:
            list_preview = list_response.body[:200].decode("utf-8", errors="ignore")
        except Exception:
            list_preview = None
    result["assistant_list"] = {
        "status_code": list_response.status_code,
        "ok": list_response.status_code < 500 and not list_response.error,
        "error": list_response.error,
        "body_preview": list_preview,
    }

    # 2) Streaming check (runs/stream)
    payload = {
        "input": {"messages": [{"role": "user", "content": "ping"}]},
    }
    stream_request = ProxyRequest(
        service_name=service_name,
        path="runs/stream",
        method="POST",
        body=json.dumps(payload).encode("utf-8"),
        query_params={},
        context=context,
        stream=True,
    )

    stream_response = await proxy.proxy(stream_request)
    aiter = None
    if stream_response.is_streaming and stream_response.stream:
        t0 = time.perf_counter()
        try:
            aiter = stream_response.stream.__aiter__()
            chunk = await asyncio.wait_for(aiter.__anext__(), timeout=5.0)
            first_ms = (time.perf_counter() - t0) * 1000
            result["stream"] = {
                "ok": True,
                "first_chunk_ms": round(first_ms, 2),
                "chunk_bytes": len(chunk) if isinstance(chunk, (bytes, bytearray)) else None,
            }
        except StopAsyncIteration:
            result["stream"] = {"ok": False, "error": "no chunks"}
        except asyncio.TimeoutError:
            result["stream"] = {"ok": False, "error": "timeout waiting for first chunk"}
        except Exception as exc:
            result["stream"] = {"ok": False, "error": str(exc)}
        finally:
            # 显式关闭流，避免连接泄露
            if aiter and hasattr(aiter, "aclose"):
                with contextlib.suppress(Exception):
                    await aiter.aclose()
    else:
        result["stream"] = {
            "ok": False,
            "status_code": stream_response.status_code,
            "error": stream_response.error or "not streaming",
        }

    return result


# ============ 辅助函数 ============


def _build_request_context(request: Request, user: UserContext) -> RequestContext:
    """从请求构建上下文"""
    # 提取原始请求头
    original_headers = dict(request.headers)

    # 提取客户端 IP
    client_ip = ""
    if xff := request.headers.get("x-forwarded-for"):
        client_ip = xff.split(",")[0].strip()
    elif real_ip := request.headers.get("x-real-ip"):
        client_ip = real_ip
    elif request.client:
        client_ip = request.client.host

    # 从 request.state 获取追踪信息
    request_id = getattr(request.state, "request_id", "")
    trace_id = getattr(request.state, "trace_id", "")
    span_id = getattr(request.state, "span_id", "")
    api_key_id = str(getattr(request.state, "api_key_hash", "") or "")

    return RequestContext(
        user_id=user.user_id,
        api_key_id=api_key_id,
        tenant_id=user.tenant_id,
        user_tier=user.tier,
        is_authenticated=user.is_authenticated,
        roles=list(user.roles) if hasattr(user, "roles") else [],
        request_id=request_id,
        trace_id=trace_id,
        span_id=span_id,
        client_ip=client_ip,
        user_agent=request.headers.get("user-agent", ""),
        original_headers=original_headers,
    )


def _wants_streaming(request: Request, path: str) -> bool:
    """判断是否期望流式响应"""
    # 检查 Accept 头
    accept = request.headers.get("accept", "")
    if "text/event-stream" in accept:
        return True

    # 检查路径
    path_lower = path.lower()
    streaming_indicators = [
        "/stream",
        "/runs/stream",
        "/sse",
        "stream=true",
    ]

    for indicator in streaming_indicators:
        if indicator in path_lower:
            return True

    # 检查查询参数
    return request.query_params.get("stream") in ("true", "1", "yes")
