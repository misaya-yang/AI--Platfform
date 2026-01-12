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
import json
import time
from typing import Any, Dict, Optional, List

from fastapi import APIRouter, Depends, HTTPException, Request, Response
from fastapi.responses import StreamingResponse

from ..deps import (
    get_settings,
    get_user_context,
    get_auth_context,
    get_rate_limiter,
    AuthContext,
)
from ...config.settings import Settings
from ...core.auth.user_resolver import UserContext
from ...core.exceptions import PermissionDeniedError
from ...core.gateway.multi_dimension_rate_limiter import (
    MultiDimensionRateLimiter,
    RateLimitContext,
    RateLimitHeaders,
)
from ...core.observability.logging import get_logger
from ...proxy import (
    TransparentProxy,
    ProxyRequest,
    ProxyConfigLoader,
    ContextInjector,
    RequestContext,
)
from ...proxy.transparent_proxy import LANGGRAPH_OPERATION_TYPES

logger = get_logger(__name__)

router = APIRouter(prefix="/proxy", tags=["Transparent Proxy"])


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

def _normalize_allowed_services(value: Any) -> List[str]:
    if not value:
        return []
    if isinstance(value, str):
        return [value]
    if isinstance(value, list):
        return [str(v) for v in value if v]
    return []


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


def _service_allowed(allowed: List[str], candidates: set) -> bool:
    if not allowed:
        return True
    if "*" in allowed:
        return True
    return any(name in allowed for name in candidates)


async def check_service_authorization(
    request: Request,
    service_name: str,
    user: UserContext,
    auth: AuthContext,
) -> None:
    """
    检查用户对服务的访问权限

    验证顺序:
    1. 基本 RBAC 权限 (service:invoke)
    2. 服务级别认证配置 (allowed_roles, public)
    3. 用户/API Key 级别 allowed_services (如果配置)
    """
    # 1. RBAC 权限检查: 需要 service:invoke 权限
    try:
        request.app.state.dispatcher.rbac.require(auth.roles, "service:invoke")
    except PermissionDeniedError:
        logger.warning(
            f"[ProxyAuth] User {user.user_id} lacks service:invoke permission for {service_name}"
        )
        raise HTTPException(
            status_code=403,
            detail="Permission denied: service:invoke required"
        )

    # 2. 服务级别认证配置检查
    registry = getattr(request.app.state, "registry", None)
    service = await _resolve_service_definition(registry, service_name)
    service_aliases = {service_name}
    if service:
        if getattr(service, "service_id", None):
            service_aliases.add(service.service_id)
        if getattr(service, "name", None):
            service_aliases.add(service.name)

        service_config = service.get_service_config() if hasattr(service, "get_service_config") else None
        if service_config and service_config.auth and service_config.auth.enabled:
            auth_config = service_config.auth

            # public 服务跳过服务级别检查，但仍需执行 allowed_services 约束
            if not auth_config.public:
                # require_auth
                if auth_config.require_auth and not user.is_authenticated:
                    raise HTTPException(
                        status_code=403,
                        detail=f"Permission denied: authentication required for {service_name}"
                    )

                # allowed_roles
                if auth_config.allowed_roles:
                    has_role = any(role in auth.roles for role in auth_config.allowed_roles)
                    # admin 始终有权限
                    if not has_role and "admin" not in auth.roles:
                        logger.warning(
                            f"[ProxyAuth] User {user.user_id} role not in allowed_roles for {service_name}"
                        )
                        raise HTTPException(
                            status_code=403,
                            detail=f"Permission denied: not authorized for service {service_name}"
                        )

                # allowed_api_keys (prefix allowlist)
                if auth_config.allowed_api_keys:
                    settings = getattr(request.app.state, "settings", None)
                    api_key_header = None
                    if settings and settings.authentication.api_key.enabled:
                        api_key_header = settings.authentication.api_key.header_name
                    api_key_value = request.headers.get(api_key_header) if api_key_header else None

                    if not api_key_value:
                        raise HTTPException(
                            status_code=403,
                            detail=f"Permission denied: API key required for service {service_name}"
                        )

                    if "*" not in auth_config.allowed_api_keys:
                        matched = any(
                            api_key_value.startswith(prefix) for prefix in auth_config.allowed_api_keys
                        )
                        if not matched:
                            raise HTTPException(
                                status_code=403,
                                detail=f"Permission denied: API key not allowed for service {service_name}"
                            )

    # 3. 用户/API Key 级别 allowed_services 检查
    db = getattr(request.app.state, "database", None)
    if db and getattr(db, "enabled", False) and user.is_authenticated:
        try:
            allowed_sets: List[List[str]] = []

            api_key_info = getattr(request.state, "api_key_info", None)
            if api_key_info:
                api_allowed = _normalize_allowed_services(api_key_info.get("allowed_services"))
                if api_allowed:
                    allowed_sets.append(api_allowed)

            if user.tenant_id:
                tenant = await db.get_tenant(user.tenant_id)
                if tenant:
                    tenant_allowed = _normalize_allowed_services(tenant.get("allowed_services"))
                    if tenant_allowed:
                        allowed_sets.append(tenant_allowed)

            for allowed in allowed_sets:
                if not _service_allowed(allowed, service_aliases):
                    logger.warning(
                        f"[ProxyAuth] Service {service_name} not in allowed_services for user {user.user_id}"
                    )
                    raise HTTPException(
                        status_code=403,
                        detail=f"Permission denied: service {service_name} not in allowed services"
                    )
        except HTTPException:
            raise
        except Exception:
            # 如果方法不存在或查询失败，继续（不阻止访问）
            pass


async def check_proxy_rate_limit(
    user: UserContext,
    rate_limiter: Optional[MultiDimensionRateLimiter],
    service_name: str,
    operation: str = "proxy",
) -> None:
    """检查代理限流"""
    if not rate_limiter:
        return

    context = RateLimitContext.from_user_context(
        user=user,
        assistant_id=service_name,  # 复用 assistant_id 作为服务标识
        operation=operation,
    )

    result = await rate_limiter.check(context)
    if not result.allowed:
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
    user: UserContext = Depends(get_user_context),
    auth: AuthContext = Depends(get_auth_context),
    rate_limiter: Optional[MultiDimensionRateLimiter] = Depends(get_rate_limiter),
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
    # Performance timing
    t_start = time.perf_counter()
    t_auth_done = t_start  # User context already resolved via Depends

    # 1. 权限检查
    await check_service_authorization(request, service_name, user, auth)
    t_auth_check = time.perf_counter()

    # 2. 检测操作类型（用于限流）
    operation = TransparentProxy.detect_operation_type(request.method, path)

    # 3. 限流检查
    await check_proxy_rate_limit(user, rate_limiter, service_name, operation)
    t_rate_limit = time.perf_counter()

    # 3. 提取请求上下文
    context = _build_request_context(request, user)
    t_context = time.perf_counter()

    # 4. 读取请求体
    body = await request.body() if request.method in ("POST", "PUT", "PATCH") else None
    t_body = time.perf_counter()

    # 5. 检查是否期望流式响应
    wants_stream = _wants_streaming(request, path)

    # 6. 构建代理请求
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
        f"rate_limit={((t_rate_limit - t_auth_check) * 1000):.1f}ms "
        f"context={((t_context - t_rate_limit) * 1000):.1f}ms "
        f"body_read={((t_body - t_context) * 1000):.1f}ms "
        f"build={((t_build - t_body) * 1000):.1f}ms "
        f"total_prep={((t_build - t_start) * 1000):.1f}ms"
    )

    # 7. 执行代理
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
    user: UserContext = Depends(get_user_context),
    auth: AuthContext = Depends(get_auth_context),
    rate_limiter: Optional[MultiDimensionRateLimiter] = Depends(get_rate_limiter),
):
    """处理根路径请求"""
    return await transparent_proxy_handler(
        service_name=service_name,
        path="",
        request=request,
        proxy=proxy,
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
    # 权限检查：需要 service:invoke 权限才能查看服务列表
    try:
        request.app.state.dispatcher.rbac.require(auth.roles, "service:invoke")
    except PermissionDeniedError:
        raise HTTPException(
            status_code=403,
            detail="Permission denied: service:invoke required"
        )

    services = await config_loader.list_services()
    is_admin = "admin" in auth.roles

    # 非管理员只能看到基本信息，管理员可以看到完整信息
    return {
        "services": [
            {
                "service_id": svc.service_id,
                "service_name": svc.service_name,
                # 仅管理员可见完整 URL
                "upstream_url": svc.upstream_url if is_admin else None,
                "assistant_id": svc.assistant_id if is_admin else None,
                "enabled": svc.enabled,
            }
            for svc in services
        ],
        "count": len(services),
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
    rate_limiter: Optional[MultiDimensionRateLimiter] = Depends(get_rate_limiter),
):
    # 权限检查：仅管理员可访问 _selftest 端点
    if "admin" not in auth.roles:
        raise HTTPException(status_code=403, detail="Admin access required for selftest endpoint")

    await check_proxy_rate_limit(user, rate_limiter, service_name, operation="proxy_selftest")

    context = _build_request_context(request, user)
    auth_present = any(k.lower() == "authorization" for k in request.headers.keys())

    result: Dict[str, Any] = {
        "service": service_name,
        "auth_header_present": auth_present,
    }

    config = await config_loader.get_config(service_name)
    if config:
        # 脱敏处理：只显示部分信息，避免暴露敏感配置
        masked_assistant_id = (config.assistant_id[:8] + "...") if config.assistant_id and len(config.assistant_id) > 8 else config.assistant_id
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
    list_request = ProxyRequest(
        service_name=service_name,
        path="assistants",
        method="GET",
        body=None,
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
                try:
                    await aiter.aclose()
                except Exception:
                    pass
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
    
    return RequestContext(
        user_id=user.user_id,
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
    if request.query_params.get("stream") in ("true", "1", "yes"):
        return True
    
    return False
