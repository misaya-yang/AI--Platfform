from __future__ import annotations

from typing import Any, Dict, List, Optional

from pydantic import BaseModel, Field
from pydantic_settings import BaseSettings, SettingsConfigDict


class DatabaseSettings(BaseModel):
    """PostgreSQL 数据库配置"""
    enabled: bool = False
    dsn: str = "postgresql://postgres:postgres@localhost:5432/gateway"


class RedisSettings(BaseModel):
    """Redis 缓存配置"""
    enabled: bool = False
    url: str = "redis://localhost:6379/0"


class AuthJWTSettings(BaseModel):
    enabled: bool = False
    secret: str = Field(default="")
    algorithms: List[str] = Field(default_factory=lambda: ["HS256"])
    audience: Optional[str] = None
    issuer: Optional[str] = None


class AuthAPIKeySettings(BaseModel):
    enabled: bool = False
    header_name: str = "X-API-Key"
    keys: List[str] = Field(default_factory=list)


class AuthenticationSettings(BaseModel):
    jwt: AuthJWTSettings = Field(default_factory=AuthJWTSettings)
    api_key: AuthAPIKeySettings = Field(default_factory=AuthAPIKeySettings)


class RBACSettings(BaseModel):
    roles: Dict[str, List[str]] = Field(
        default_factory=lambda: {
            "user": ["service:invoke", "task:read"],
            "developer": ["service:invoke", "task:read", "service:manage"],
            "admin": ["admin:*"],
        }
    )


class RateLimitSettings(BaseModel):
    enabled: bool = False
    global_limit: Optional[Dict[str, Any]] = None
    tenant_limits: Dict[str, Dict[str, Any]] = Field(default_factory=dict)
    user_limits: Dict[str, Dict[str, Any]] = Field(default_factory=dict)
    service_limits: Dict[str, Dict[str, Any]] = Field(default_factory=dict)
    ip_limits: Optional[Dict[str, Any]] = None


class LoadBalancerSettings(BaseModel):
    """负载均衡配置"""
    strategy: str = "round_robin"  # round_robin, random, weighted, consistent_hash


class LangGraphInstanceSettings(BaseModel):
    """LangGraph Server 实例配置"""
    instance_id: str
    url: str
    weight: int = 1


class LangGraphSettings(BaseModel):
    """LangGraph 配置"""
    enabled: bool = False
    
    # 负载均衡配置
    load_balancer_strategy: str = "least_connections"  # round_robin | least_connections | weighted | latency_based
    health_check_interval: int = 10
    
    # LangGraph Server 实例列表
    instances: List[LangGraphInstanceSettings] = Field(default_factory=list)
    
    # 也可以通过环境变量配置（逗号分隔的 URL 列表）
    # GATEWAY_LANGGRAPH__INSTANCE_URLS=http://langgraph-1:8000,http://langgraph-2:8000
    instance_urls: str = ""  # 逗号分隔的实例 URL 列表
    
    # 用户层级限流配置
    tier_limits: Dict[str, Dict[str, int]] = Field(default_factory=lambda: {
        "anonymous": {"requests": 10, "window": 60},
        "normal": {"requests": 60, "window": 60},
        "premium": {"requests": 300, "window": 60},
        "enterprise": {"requests": 1000, "window": 60},
    })


class Settings(BaseSettings):
    model_config = SettingsConfigDict(
        env_prefix="GATEWAY_",
        env_nested_delimiter="__",
        # Prefer repo-local env files (used by this project layout), but keep
        # compatibility with conventional root `.env` files as well.
        env_file=("config/.env", "config/env.local", ".env", ".env.local"),
        env_file_encoding="utf-8",
        extra="ignore",
    )

    host: str = "0.0.0.0"
    port: int = 8080
    services_path: str = "services"

    # 数据库和缓存（可选）
    database: DatabaseSettings = Field(default_factory=DatabaseSettings)
    redis: RedisSettings = Field(default_factory=RedisSettings)

    # 鉴权和权限
    authentication: AuthenticationSettings = Field(default_factory=AuthenticationSettings)
    rbac: RBACSettings = Field(default_factory=RBACSettings)
    rate_limits: RateLimitSettings = Field(default_factory=RateLimitSettings)

    # 负载均衡
    load_balancer: LoadBalancerSettings = Field(default_factory=LoadBalancerSettings)

    # LangGraph 配置
    langgraph: LangGraphSettings = Field(default_factory=LangGraphSettings)

    health_check_interval: int = 30
    task_worker_concurrency: int = 2
