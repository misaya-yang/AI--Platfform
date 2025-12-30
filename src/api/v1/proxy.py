"""
透明代理路由

提供通配符路由 /proxy/{service_name}/{path:path}，支持：
- 动态路由转发
- SSE 流式传输
- 鉴权和限流
- 上下文注入
"""

from __future__ import annotations

import json
from typing import Any, Dict, Optional

from fastapi import APIRouter, Depends, HTTPException, Request, Response
from fastapi.responses import StreamingResponse

from ..deps import (
    get_settings,
    get_user_context,
    get_rate_limiter,
)
from ...config.settings import Settings
from ...core.auth.user_resolver import UserContext
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


# ============ 限流检查 ============

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
    rate_limiter: Optional[MultiDimensionRateLimiter] = Depends(get_rate_limiter),
):
    """
    透明代理主处理函数
    
    处理流程：
    1. 限流检查
    2. 提取请求上下文
    3. 构建代理请求
    4. 执行代理并返回响应
    """
    # 1. 检测操作类型（用于限流）
    operation = TransparentProxy.detect_operation_type(request.method, path)

    # 2. 限流检查
    await check_proxy_rate_limit(user, rate_limiter, service_name, operation)

    # 3. 提取请求上下文
    context = _build_request_context(request, user)

    # 4. 读取请求体
    body = await request.body() if request.method in ("POST", "PUT", "PATCH") else None

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
    
    # 7. 执行代理
    logger.info(
        f"[ProxyRoute] {request.method} /proxy/{service_name}/{path} "
        f"user={user.user_id} op={operation} stream={wants_stream}"
    )

    response = await proxy.proxy(proxy_request)

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
    rate_limiter: Optional[MultiDimensionRateLimiter] = Depends(get_rate_limiter),
):
    """处理根路径请求"""
    return await transparent_proxy_handler(
        service_name=service_name,
        path="",
        request=request,
        proxy=proxy,
        user=user,
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
):
    """列出可用的代理服务"""
    services = await config_loader.list_services()
    
    return {
        "services": [
            {
                "service_id": svc.service_id,
                "service_name": svc.service_name,
                "upstream_url": svc.upstream_url,
                "assistant_id": svc.assistant_id,
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

