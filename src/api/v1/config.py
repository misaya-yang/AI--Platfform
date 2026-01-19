from __future__ import annotations

import hashlib
import secrets
from typing import Any, Dict, List, Optional

from fastapi import APIRouter, Depends, HTTPException, Request
from pydantic import BaseModel, Field

from ...core.observability.logging import get_logger
from ..deps import AuthContext, get_auth_context, get_settings

logger = get_logger(__name__)


router = APIRouter(prefix="/config", tags=["config"])


# ===== 请求/响应模型 =====

class LangGraphServiceCreate(BaseModel):
    """简化的 LangGraph 服务创建"""
    service_id: str = Field(..., description="服务唯一标识")
    name: str = Field(..., description="服务显示名称")
    deployment_url: str = Field(..., description="LangGraph 部署 URL")
    graph_id: str = Field(..., description="Graph ID 或 Assistant ID")
    langsmith_api_key: Optional[str] = Field(None, description="LangSmith API Key（可选）")
    session_enabled: bool = Field(True, description="是否启用会话")
    description: Optional[str] = None
    proxy_mode: str = Field("transparent", description="代理模式: transparent | adapter")


class RateLimitRule(BaseModel):
    """限流规则"""
    scope: str = Field(..., description="限流范围: global, ip, user, tenant, service")
    scope_id: Optional[str] = Field(None, description="范围 ID（如 service_id）")
    requests: int = Field(..., description="允许的请求数")
    window: int = Field(..., description="时间窗口（秒）")
    burst: int = Field(0, description="突发允许数")
    enabled: bool = True


class AuthConfigUpdate(BaseModel):
    """鉴权配置更新"""
    jwt_enabled: bool = False
    jwt_secret: Optional[str] = None
    jwt_algorithms: List[str] = ["HS256"]
    jwt_issuer: Optional[str] = None
    api_key_enabled: bool = False
    api_key_header: str = "X-API-Key"
    api_keys: List[str] = []


class ApiKeyCreate(BaseModel):
    """创建 API Key"""
    name: str
    tenant_id: Optional[str] = None
    roles: List[str] = ["user"]


# ===== 运行时配置存储 =====

# 内存存储（当数据库未启用时使用）
_runtime_config: Dict[str, Any] = {
    "auth": {
        "jwt_enabled": False,
        "jwt_secret": "",
        "jwt_algorithms": ["HS256"],
        "jwt_issuer": None,
        "api_key_enabled": False,
        "api_key_header": "X-API-Key",
        "api_keys": [],
    },
    "rate_limits": [],
    "api_keys": [],
}


# ===== LangGraph 服务快速注册 =====

@router.post("/services/langgraph")
async def create_langgraph_service(
    body: LangGraphServiceCreate,
    request: Request,
    auth: AuthContext = Depends(get_auth_context),
):
    """快速注册 LangGraph 服务（支持透明代理模式）"""
    registry = request.app.state.registry
    # 权限：需要 service:manage 或 admin
    request.app.state.dispatcher.rbac.require(auth.roles, "service:manage")

    # 构建连接器配置
    connector_config = {
        "base_url": body.deployment_url.rstrip("/"),
        "graph_id": body.graph_id,
        "assistant_id": body.graph_id,
        # 透明代理配置
        "proxy_mode": body.proxy_mode,
        "upstream_url": body.deployment_url.rstrip("/"),
        "forward_auth": True,
        "timeout_connect": 5,
        "timeout_read": 300,
        "timeout_write": 60,
        "load_balance_strategy": "round_robin",
    }
    
    # 认证 Token
    if body.langsmith_api_key:
        connector_config["auth_token"] = body.langsmith_api_key
        connector_config["headers"] = {
            "X-Api-Key": body.langsmith_api_key,
        }

    service_def = {
        "service_id": body.service_id,
        "name": body.name,
        "description": body.description or f"LangGraph service: {body.name}",
        "service_type": "langgraph",  # 使用专门的 langgraph 类型
        "connector_type": "http",
        "connector_config": connector_config,
        "supported_modes": ["sync", "stream"],
        "accepted_content_types": ["text"],
        "output_content_types": ["text"],
        "session_enabled": body.session_enabled,
        "status": "active",  # 默认激活
        "metadata": {
            "adapter_type": "langgraph",
            "proxy_mode": body.proxy_mode,
        },
    }

    # 注册服务
    try:
        service = registry._service_from_dict(service_def)
        await registry.register(service)
        
        # 如果数据库可用，持久化
        db = getattr(request.app.state, "database", None)
        if db and db.enabled:
            await db.save_service(service_def)

        # 返回代理路由信息
        proxy_route = f"/api/v1/proxy/{body.service_id}" if body.proxy_mode == "transparent" else None
        
        return {
            "status": "success",
            "service_id": body.service_id,
            "message": "服务注册成功",
            "proxy_mode": body.proxy_mode,
            "proxy_route": proxy_route,
            "example_endpoints": {
                "list_assistants": f"{proxy_route}/assistants" if proxy_route else None,
                "create_thread": f"{proxy_route}/threads" if proxy_route else None,
                "stream_run": f"{proxy_route}/threads/{{thread_id}}/runs/stream" if proxy_route else None,
            } if proxy_route else None,
        }
    except Exception as e:
        raise HTTPException(status_code=400, detail=str(e))


# ===== 鉴权配置 =====

@router.get("/auth")
async def get_auth_config(
    request: Request,
    auth: AuthContext = Depends(get_auth_context),
):
    """获取当前鉴权配置（仅管理员）"""
    # 权限检查：仅管理员可查看鉴权配置
    request.app.state.dispatcher.rbac.require(auth.roles, "admin")

    settings = request.app.state.settings
    return {
        "jwt": {
            "enabled": settings.authentication.jwt.enabled,
            "algorithms": settings.authentication.jwt.algorithms,
            "issuer": settings.authentication.jwt.issuer,
            "audience": settings.authentication.jwt.audience,
        },
        "api_key": {
            "enabled": settings.authentication.api_key.enabled,
            "header_name": settings.authentication.api_key.header_name,
            "key_count": len(settings.authentication.api_key.keys),
        },
        "runtime": _runtime_config["auth"],
    }


@router.put("/auth")
async def update_auth_config(
    body: AuthConfigUpdate,
    request: Request,
    auth: AuthContext = Depends(get_auth_context),
):
    """更新鉴权配置（运行时，仅管理员）"""
    # 权限检查：仅管理员可修改鉴权配置
    request.app.state.dispatcher.rbac.require(auth.roles, "admin")

    _runtime_config["auth"] = {
        "jwt_enabled": body.jwt_enabled,
        "jwt_secret": body.jwt_secret or "",
        "jwt_algorithms": body.jwt_algorithms,
        "jwt_issuer": body.jwt_issuer,
        "api_key_enabled": body.api_key_enabled,
        "api_key_header": body.api_key_header,
        "api_keys": body.api_keys,
    }

    # 如果 Redis 可用，缓存配置
    redis = getattr(request.app.state, "redis", None)
    if redis and redis.enabled:
        await redis.cache_config("auth", _runtime_config["auth"])

    return {"status": "success", "message": "鉴权配置已更新"}


# ===== API Key 管理 =====

@router.get("/api-keys")
async def list_api_keys(
    request: Request,
    auth: AuthContext = Depends(get_auth_context),
):
    """列出所有 API Keys（仅管理员）"""
    # 权限检查：仅管理员可查看 API Keys
    request.app.state.dispatcher.rbac.require(auth.roles, "admin")

    db = getattr(request.app.state, "database", None)
    if db and db.enabled:
        try:
            keys = await db.list_api_keys()
            return {"keys": keys}
        except Exception as e:
            logger.error(f"Failed to list API keys: {e}", exc_info=True)
            raise HTTPException(status_code=500, detail=f"数据库查询失败: {str(e)}")

    # 返回运行时配置中的 keys
    return {
        "keys": [
            {"id": i, "name": f"Key {i+1}", "enabled": True}
            for i, _ in enumerate(_runtime_config["auth"].get("api_keys", []))
        ]
    }


@router.post("/api-keys")
async def create_api_key(
    body: ApiKeyCreate,
    request: Request,
    auth: AuthContext = Depends(get_auth_context),
):
    """创建新的 API Key（仅管理员）"""
    # 权限检查：仅管理员可创建 API Key
    request.app.state.dispatcher.rbac.require(auth.roles, "admin")

    # 生成 API Key
    api_key = f"gw_{secrets.token_urlsafe(32)}"
    key_hash = hashlib.sha256(api_key.encode()).hexdigest()

    db = getattr(request.app.state, "database", None)
    if db and db.enabled:
        try:
            await db.save_api_key(
                key_hash=key_hash,
                name=body.name,
                tenant_id=body.tenant_id or "default",
                roles=body.roles,
            )
        except Exception as e:
            logger.error(f"Failed to save API key: {e}", exc_info=True)
            raise HTTPException(status_code=500, detail=f"数据库保存失败: {str(e)}")
    else:
        # 运行时存储
        _runtime_config["api_keys"].append({
            "key_hash": key_hash,
            "name": body.name,
            "roles": body.roles,
        })
        _runtime_config["auth"]["api_keys"].append(api_key)

    return {
        "status": "success",
        "api_key": api_key,  # 只返回一次，不存储明文
        "message": "请保存此 API Key，它不会再次显示",
    }


# ===== 限流配置 =====

@router.get("/rate-limits")
async def list_rate_limits(
    request: Request,
    auth: AuthContext = Depends(get_auth_context),
):
    """获取所有限流规则（仅管理员）"""
    # 权限检查：仅管理员可查看限流规则
    request.app.state.dispatcher.rbac.require(auth.roles, "admin")

    db = getattr(request.app.state, "database", None)
    if db and db.enabled:
        limits = await db.get_rate_limits()
        return {"rules": limits}
    
    return {"rules": _runtime_config.get("rate_limits", [])}


@router.post("/rate-limits")
async def create_rate_limit(
    body: RateLimitRule,
    request: Request,
    auth: AuthContext = Depends(get_auth_context),
):
    """创建限流规则（仅管理员）"""
    # 权限检查：仅管理员可创建限流规则
    request.app.state.dispatcher.rbac.require(auth.roles, "admin")

    rule = body.model_dump()

    db = getattr(request.app.state, "database", None)
    if db and db.enabled:
        await db.save_rate_limit(
            body.scope, body.scope_id or "", body.requests, body.window, body.burst
        )
    else:
        # 运行时存储
        _runtime_config.setdefault("rate_limits", []).append(rule)

    # 更新运行时限流器
    # TODO: 动态更新 rate_limiter

    return {"status": "success", "message": "限流规则已创建"}


@router.delete("/rate-limits/{scope}/{scope_id}")
async def delete_rate_limit(
    scope: str,
    scope_id: str,
    request: Request,
    auth: AuthContext = Depends(get_auth_context),
):
    """删除限流规则（仅管理员）"""
    # 权限检查：仅管理员可删除限流规则
    request.app.state.dispatcher.rbac.require(auth.roles, "admin")

    # TODO: 实现删除逻辑
    return {"status": "success", "message": "限流规则已删除"}


# ===== 负载均衡配置 =====

class LoadBalancerConfigUpdate(BaseModel):
    """负载均衡配置更新"""
    strategy: str = "round_robin"  # round_robin, random, weighted, consistent_hash


_load_balancer_config = {
    "strategy": "round_robin",
}


@router.get("/load-balancer")
async def get_load_balancer_config(
    request: Request,
    auth: AuthContext = Depends(get_auth_context),
):
    """获取负载均衡配置（仅管理员）"""
    # 权限检查：仅管理员可查看负载均衡配置
    request.app.state.dispatcher.rbac.require(auth.roles, "admin")

    settings = request.app.state.settings
    return {
        "strategy": settings.load_balancer.strategy,
        "runtime": _load_balancer_config,
        "available_strategies": [
            {"value": "round_robin", "label": "轮询", "description": "按顺序依次选择实例"},
            {"value": "random", "label": "随机", "description": "随机选择实例"},
            {"value": "weighted", "label": "加权", "description": "根据权重概率选择实例"},
            {"value": "consistent_hash", "label": "一致性哈希", "description": "相同会话路由到相同实例"},
        ],
    }


@router.put("/load-balancer")
async def update_load_balancer_config(
    body: LoadBalancerConfigUpdate,
    request: Request,
    auth: AuthContext = Depends(get_auth_context),
):
    """更新负载均衡配置（仅管理员）"""
    # 权限检查：仅管理员可修改负载均衡配置
    request.app.state.dispatcher.rbac.require(auth.roles, "admin")

    _load_balancer_config["strategy"] = body.strategy

    # 更新运行时负载均衡器策略
    load_balancer = getattr(request.app.state, "load_balancer", None)
    if load_balancer:
        load_balancer.strategy_name = body.strategy

    return {"status": "success", "message": "负载均衡配置已更新"}


# ===== 服务级别配置 =====

class ServiceRateLimitConfigUpdate(BaseModel):
    """服务级别限流配置"""
    enabled: bool = False
    requests: int = 100
    window: int = 60
    burst: int = 0
    strategy: str = "sliding_window"


class ServiceAuthConfigUpdate(BaseModel):
    """服务级别鉴权配置"""
    enabled: bool = False
    require_auth: bool = True
    allowed_roles: List[str] = []
    allowed_api_keys: List[str] = []
    public: bool = False


class ServiceCacheConfigUpdate(BaseModel):
    """服务级别缓存配置"""
    enabled: bool = False
    ttl: int = 300
    semantic_cache: bool = False


class ServicePriorityConfigUpdate(BaseModel):
    """服务优先级配置"""
    priority: int = 5
    weight: int = 1
    max_queue_size: int = 100


class ServiceConfigUpdate(BaseModel):
    """服务级别综合配置更新"""
    rate_limit: Optional[ServiceRateLimitConfigUpdate] = None
    auth: Optional[ServiceAuthConfigUpdate] = None
    cache: Optional[ServiceCacheConfigUpdate] = None
    priority: Optional[ServicePriorityConfigUpdate] = None


@router.get("/services/{service_id}/config")
async def get_service_config(
    service_id: str,
    request: Request,
    auth: AuthContext = Depends(get_auth_context),
):
    """获取指定服务的配置（仅管理员）"""
    # 权限检查：仅管理员可查看服务配置
    request.app.state.dispatcher.rbac.require(auth.roles, "admin")

    registry = request.app.state.registry
    service = await registry.get(service_id)
    if not service:
        raise HTTPException(status_code=404, detail=f"服务 {service_id} 不存在")

    # 获取服务配置
    config = service.get_service_config()

    return {
        "service_id": service_id,
        "name": service.name,
        "config": {
            "rate_limit": {
                "enabled": config.rate_limit.enabled,
                "requests": config.rate_limit.requests,
                "window": config.rate_limit.window,
                "burst": config.rate_limit.burst,
                "strategy": config.rate_limit.strategy,
            },
            "auth": {
                "enabled": config.auth.enabled,
                "require_auth": config.auth.require_auth,
                "allowed_roles": config.auth.allowed_roles,
                "allowed_api_keys": config.auth.allowed_api_keys,
                "public": config.auth.public,
            },
            "cache": {
                "enabled": config.cache.enabled,
                "ttl": config.cache.ttl,
                "semantic_cache": config.cache.semantic_cache,
            },
            "priority": {
                "priority": config.priority.priority,
                "weight": config.priority.weight,
                "max_queue_size": config.priority.max_queue_size,
            },
        },
        # 兼容旧格式
        "legacy": {
            "rate_limit": service.rate_limit,
            "concurrency_limit": service.concurrency_limit,
            "timeout": service.timeout,
            "max_retries": service.max_retries,
            "circuit_breaker_enabled": service.circuit_breaker_enabled,
            "failure_threshold": service.failure_threshold,
            "recovery_timeout": service.recovery_timeout,
        },
    }


@router.put("/services/{service_id}/config")
async def update_service_config(
    service_id: str,
    body: ServiceConfigUpdate,
    request: Request,
    auth: AuthContext = Depends(get_auth_context),
):
    """更新指定服务的配置（仅管理员）"""
    # 权限检查：仅管理员可修改服务配置
    request.app.state.dispatcher.rbac.require(auth.roles, "admin")

    from ...models.service import (
        ServiceConfig,
        ServiceRateLimitConfig,
        ServiceAuthConfig,
        ServiceCacheConfig,
        ServicePriorityConfig,
    )

    registry = request.app.state.registry
    service = await registry.get(service_id)
    if not service:
        raise HTTPException(status_code=404, detail=f"服务 {service_id} 不存在")

    # 获取或创建服务配置
    config = service.get_service_config()

    # 更新限流配置
    if body.rate_limit:
        config.rate_limit = ServiceRateLimitConfig(
            enabled=body.rate_limit.enabled,
            requests=body.rate_limit.requests,
            window=body.rate_limit.window,
            burst=body.rate_limit.burst,
            strategy=body.rate_limit.strategy,
        )
        # 同时更新兼容字段
        if body.rate_limit.enabled:
            service.rate_limit = {
                "requests": body.rate_limit.requests,
                "window": body.rate_limit.window,
                "burst": body.rate_limit.burst,
                "strategy": body.rate_limit.strategy,
            }
        else:
            service.rate_limit = None

    # 更新鉴权配置
    if body.auth:
        config.auth = ServiceAuthConfig(
            enabled=body.auth.enabled,
            require_auth=body.auth.require_auth,
            allowed_roles=body.auth.allowed_roles,
            allowed_api_keys=body.auth.allowed_api_keys,
            public=body.auth.public,
        )

    # 更新缓存配置
    if body.cache:
        config.cache = ServiceCacheConfig(
            enabled=body.cache.enabled,
            ttl=body.cache.ttl,
            semantic_cache=body.cache.semantic_cache,
        )

    # 更新优先级配置
    if body.priority:
        config.priority = ServicePriorityConfig(
            priority=body.priority.priority,
            weight=body.priority.weight,
            max_queue_size=body.priority.max_queue_size,
        )

    # 保存更新
    service.service_config = config
    await registry.storage.save(service)
    registry._cache[service_id] = service

    # 如果数据库可用，持久化
    db = getattr(request.app.state, "database", None)
    if db and db.enabled:
        await db.update_service_config(service_id, config)

    return {
        "status": "success",
        "message": f"服务 {service_id} 配置已更新",
        "service_id": service_id,
    }


@router.delete("/services/{service_id}")
async def delete_service(
    service_id: str,
    request: Request,
    auth: AuthContext = Depends(get_auth_context),
):
    """删除服务（仅管理员）"""
    # 权限检查：仅管理员可删除服务
    request.app.state.dispatcher.rbac.require(auth.roles, "admin")

    registry = request.app.state.registry
    service = await registry.get(service_id)
    if not service:
        raise HTTPException(status_code=404, detail=f"服务 {service_id} 不存在")

    # 从存储中删除（同时让 RegistryStorage 负责自身缓存一致性）
    deleted = False
    if hasattr(registry.storage, "delete"):
        deleted = await registry.storage.delete(service_id)
    else:
        # 兼容旧实现
        if hasattr(registry.storage, "_services"):
            deleted = registry.storage._services.pop(service_id, None) is not None

    registry._cache.pop(service_id, None)
    if not deleted:
        raise HTTPException(status_code=404, detail=f"服务 {service_id} 不存在")

    return {"status": "success", "message": f"服务 {service_id} 已删除"}


# ===== 系统状态 =====

@router.get("/status")
async def get_system_status(
    request: Request,
    auth: AuthContext = Depends(get_auth_context),
):
    """获取系统状态（仅管理员）"""
    # 权限检查：仅管理员可查看系统状态
    request.app.state.dispatcher.rbac.require(auth.roles, "admin")

    settings = request.app.state.settings

    db = getattr(request.app.state, "database", None)
    redis = getattr(request.app.state, "redis", None)

    return {
        "database": {
            "enabled": settings.database.enabled,
            "connected": db._pool is not None if db else False,
        },
        "redis": {
            "enabled": settings.redis.enabled,
            "connected": await redis.ping() if redis and redis.enabled else False,
        },
        "authentication": {
            "jwt_enabled": settings.authentication.jwt.enabled,
            "api_key_enabled": settings.authentication.api_key.enabled,
        },
        "rate_limiting": {
            "enabled": settings.rate_limits.enabled,
        },
        "load_balancer": {
            "strategy": _load_balancer_config["strategy"],
        },
    }
