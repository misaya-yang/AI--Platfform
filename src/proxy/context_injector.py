"""
上下文注入器

在转发请求时注入网关上下文信息到 HTTP 头部：
- X-GW-User-ID: 用户标识
- X-GW-Request-ID: 请求追踪 ID
- X-GW-Tenant-ID: 租户标识
- X-GW-User-Tier: 用户层级
"""

from __future__ import annotations

import uuid
from dataclasses import dataclass, field
from typing import Any, Dict, List, Optional

from ..core.observability.logging import get_logger

logger = get_logger(__name__)


@dataclass
class RequestContext:
    """请求上下文"""
    
    # 用户信息
    user_id: str = ""
    tenant_id: str = ""
    user_tier: str = "anonymous"
    is_authenticated: bool = False
    roles: List[str] = field(default_factory=list)
    
    # 请求信息
    request_id: str = ""
    trace_id: str = ""
    span_id: str = ""
    
    # 客户端信息
    client_ip: str = ""
    user_agent: str = ""
    
    # 原始请求头（用于选择性转发）
    original_headers: Dict[str, str] = field(default_factory=dict)
    
    def __post_init__(self):
        if not self.request_id:
            self.request_id = str(uuid.uuid4())
        if not self.trace_id:
            self.trace_id = uuid.uuid4().hex


class ContextInjector:
    """
    上下文注入器
    
    负责在转发请求时注入网关上下文信息。
    """
    
    # 网关注入的头部前缀
    GW_HEADER_PREFIX = "X-GW-"
    
    # 标准头部映射（网关内部格式）
    HEADER_MAPPINGS = {
        "user_id": "X-GW-User-ID",
        "tenant_id": "X-GW-Tenant-ID",
        "request_id": "X-GW-Request-ID",
        "trace_id": "X-Trace-ID",
        "span_id": "X-Span-ID",
        "user_tier": "X-GW-User-Tier",
        "client_ip": "X-Forwarded-For",
    }

    # LangGraph 兼容头部映射
    # LangGraph auth 期望的头部格式（不带 X-GW- 前缀）
    LANGGRAPH_HEADER_MAPPINGS = {
        "user_id": "X-User-Id",
        "tenant_id": "X-Tenant-Id",
        "user_type": "X-User-Type",
        "user_tier": "X-User-Tier",
        "user_name": "X-User-Name",
        "user_permissions": "X-User-Permissions",
    }
    
    # 需要转发的原始头部（白名单）
    FORWARD_HEADERS = {
        "authorization",
        "content-type",
        "accept",
        "accept-language",
        "accept-encoding",
        "user-agent",
        "x-request-id",
        "x-trace-id",
        "x-correlation-id",
    }
    
    # 禁止转发的头部（黑名单）
    BLOCKED_HEADERS = {
        "host",
        "content-length",
        "transfer-encoding",
        "connection",
        "keep-alive",
        "proxy-authenticate",
        "proxy-authorization",
        "te",
        "trailers",
        "upgrade",
    }
    
    def __init__(
        self,
        inject_user_info: bool = True,
        inject_request_info: bool = True,
        forward_auth: bool = True,
        custom_headers: Optional[Dict[str, str]] = None,
    ):
        """
        初始化上下文注入器
        
        Args:
            inject_user_info: 是否注入用户信息
            inject_request_info: 是否注入请求追踪信息
            forward_auth: 是否转发 Authorization 头
            custom_headers: 自定义静态头部
        """
        self.inject_user_info = inject_user_info
        self.inject_request_info = inject_request_info
        self.forward_auth = forward_auth
        self.custom_headers = custom_headers or {}
    
    def build_headers(
        self,
        context: RequestContext,
        service_auth_token: Optional[str] = None,
    ) -> Dict[str, str]:
        """
        构建转发请求的头部
        
        Args:
            context: 请求上下文
            service_auth_token: 服务内部认证 token（覆盖原始 Authorization）
            
        Returns:
            构建好的请求头部字典
        """
        headers: Dict[str, str] = {}
        
        # 1. 转发原始头部（白名单过滤）
        for name, value in context.original_headers.items():
            name_lower = name.lower()
            
            # 检查黑名单
            if name_lower in self.BLOCKED_HEADERS:
                continue
            
            # 检查白名单
            if name_lower in self.FORWARD_HEADERS:
                # Authorization 特殊处理
                if name_lower == "authorization":
                    if self.forward_auth and not service_auth_token:
                        headers[name] = value
                else:
                    headers[name] = value
        
        # 2. 注入用户信息
        if self.inject_user_info:
            # 2a. 网关标准头部（X-GW- 前缀）
            if context.user_id:
                headers[self.HEADER_MAPPINGS["user_id"]] = context.user_id
            if context.tenant_id:
                headers[self.HEADER_MAPPINGS["tenant_id"]] = context.tenant_id
            if context.user_tier:
                headers[self.HEADER_MAPPINGS["user_tier"]] = context.user_tier

            # 用户角色（逗号分隔）
            if context.roles:
                headers["X-GW-User-Roles"] = ",".join(context.roles)

            # 认证状态
            headers["X-GW-Authenticated"] = "true" if context.is_authenticated else "false"

            # 2b. LangGraph 兼容头部（无前缀，LangGraph auth 期望的格式）
            if context.user_id:
                headers[self.LANGGRAPH_HEADER_MAPPINGS["user_id"]] = context.user_id
            if context.tenant_id:
                headers[self.LANGGRAPH_HEADER_MAPPINGS["tenant_id"]] = context.tenant_id
            if context.user_tier:
                headers[self.LANGGRAPH_HEADER_MAPPINGS["user_tier"]] = context.user_tier

            # X-User-Type: user/guest/anonymous
            user_type = "user" if context.is_authenticated else ("guest" if context.user_id else "anonymous")
            headers[self.LANGGRAPH_HEADER_MAPPINGS["user_type"]] = user_type

            # X-User-Name: 可选的用户名
            if context.user_id:
                headers[self.LANGGRAPH_HEADER_MAPPINGS["user_name"]] = f"User-{context.user_id}"

            # X-User-Permissions: 基于角色推断的权限
            permissions = ["read"]
            if context.is_authenticated:
                permissions.append("write")
            if "admin" in context.roles or context.user_tier == "admin":
                permissions.extend(["admin", "delete"])
            headers[self.LANGGRAPH_HEADER_MAPPINGS["user_permissions"]] = ",".join(permissions)
        
        # 3. 注入请求追踪信息
        if self.inject_request_info:
            headers[self.HEADER_MAPPINGS["request_id"]] = context.request_id
            if context.trace_id:
                headers[self.HEADER_MAPPINGS["trace_id"]] = context.trace_id
            if context.span_id:
                headers[self.HEADER_MAPPINGS["span_id"]] = context.span_id
        
        # 4. 客户端 IP（X-Forwarded-For）
        if context.client_ip:
            existing_xff = headers.get("X-Forwarded-For", "")
            if existing_xff:
                headers["X-Forwarded-For"] = f"{existing_xff}, {context.client_ip}"
            else:
                headers["X-Forwarded-For"] = context.client_ip
        
        # 5. 服务内部认证 token
        if service_auth_token:
            # 支持多种认证方式：
            # - Bearer token: Authorization: Bearer xxx
            # - API Key: X-Api-Key: xxx (LangGraph 使用此方式)
            if service_auth_token.startswith("Bearer "):
                headers["Authorization"] = service_auth_token
            elif ":" in service_auth_token:
                # 格式: "Header-Name:value"
                header_name, header_value = service_auth_token.split(":", 1)
                headers[header_name.strip()] = header_value.strip()
            else:
                # 默认作为 X-Api-Key（LangGraph 兼容）
                headers["X-Api-Key"] = service_auth_token
        
        # 6. 自定义静态头部
        headers.update(self.custom_headers)
        
        # 7. 确保 Content-Type
        if "content-type" not in {k.lower() for k in headers}:
            headers["Content-Type"] = "application/json"
        
        return headers
    
    @staticmethod
    def extract_context_from_scope(scope: Dict[str, Any]) -> RequestContext:
        """
        从 ASGI scope 提取请求上下文
        
        Args:
            scope: ASGI scope 字典
            
        Returns:
            RequestContext 对象
        """
        # 从 scope["state"] 获取中间件注入的信息
        state = scope.get("state", {})
        
        # 用户信息
        user_info = state.get("user_info", {})
        
        # 原始请求头
        raw_headers = scope.get("headers", [])
        original_headers = {
            name.decode("utf-8"): value.decode("utf-8")
            for name, value in raw_headers
        }
        
        # 客户端 IP
        client_ip = ""
        if xff := original_headers.get("x-forwarded-for"):
            client_ip = xff.split(",")[0].strip()
        elif real_ip := original_headers.get("x-real-ip"):
            client_ip = real_ip
        elif client := scope.get("client"):
            client_ip = client[0]
        
        return RequestContext(
            user_id=user_info.get("user_id", ""),
            tenant_id=user_info.get("tenant_id", ""),
            user_tier=user_info.get("tier", "anonymous"),
            is_authenticated=user_info.get("is_authenticated", False),
            roles=user_info.get("roles", []),
            request_id=state.get("request_id", str(uuid.uuid4())),
            trace_id=state.get("trace_id", ""),
            span_id=state.get("span_id", ""),
            client_ip=client_ip,
            user_agent=original_headers.get("user-agent", ""),
            original_headers=original_headers,
        )
    
    @staticmethod
    def extract_context_from_request(request) -> RequestContext:
        """
        从 FastAPI Request 对象提取请求上下文
        
        Args:
            request: FastAPI Request 对象
            
        Returns:
            RequestContext 对象
        """
        # 从 request.state 获取信息
        state = request.state._state if hasattr(request.state, "_state") else {}
        
        # 用户信息（可能来自 get_user_context 依赖）
        user_info = getattr(request.state, "user_info", {})
        if not user_info:
            user_info = state.get("user_info", {})
        
        # 原始请求头
        original_headers = dict(request.headers)
        
        # 客户端 IP
        client_ip = ""
        if xff := request.headers.get("x-forwarded-for"):
            client_ip = xff.split(",")[0].strip()
        elif real_ip := request.headers.get("x-real-ip"):
            client_ip = real_ip
        elif request.client:
            client_ip = request.client.host
        
        return RequestContext(
            user_id=user_info.get("user_id", "") if isinstance(user_info, dict) else getattr(user_info, "user_id", ""),
            tenant_id=user_info.get("tenant_id", "") if isinstance(user_info, dict) else getattr(user_info, "tenant_id", ""),
            user_tier=user_info.get("tier", "anonymous") if isinstance(user_info, dict) else getattr(user_info, "tier", "anonymous"),
            is_authenticated=user_info.get("is_authenticated", False) if isinstance(user_info, dict) else getattr(user_info, "is_authenticated", False),
            roles=user_info.get("roles", []) if isinstance(user_info, dict) else getattr(user_info, "roles", []),
            request_id=getattr(request.state, "request_id", str(uuid.uuid4())),
            trace_id=getattr(request.state, "trace_id", ""),
            span_id=getattr(request.state, "span_id", ""),
            client_ip=client_ip,
            user_agent=request.headers.get("user-agent", ""),
            original_headers=original_headers,
        )

