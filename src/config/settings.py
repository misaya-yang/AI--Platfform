from __future__ import annotations

from typing import Any, Dict, List, Optional

from pydantic import BaseModel, Field
from pydantic_settings import BaseSettings, SettingsConfigDict


class DatabaseSettings(BaseModel):
    """PostgreSQL 数据库配置"""
    enabled: bool = False
    dsn: str = "postgresql://postgres:postgres@localhost:5432/gateway"
    # Development-friendly: automatically run `database/schema.sql` when core tables are missing.
    auto_init: bool = True


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
            # Anonymous/guest users. Keep minimal permissions; per-service auth
            # config should still be used to require authenticated roles where needed.
            "guest": ["service:invoke"],
            "user": ["service:invoke", "task:read"],
            "developer": ["service:invoke", "task:read", "service:manage", "knowledge:manage"],
            "admin": ["admin:*"],
        }
    )


class AnonymousIdentitySettings(BaseModel):
    """Anonymous/guest identity settings (for stable pre-auth sessions)."""

    enabled: bool = True
    cookie_name: str = "ag_anon_id"
    header_name: str = "X-AG-Anonymous-Id"
    ttl_days: int = 30
    same_site: str = "lax"  # lax | strict | none


class SessionSettings(BaseModel):
    """Session persistence settings."""

    # Default TTLs applied when the backing SessionManager supports expiry.
    anonymous_ttl_seconds: int = 86400  # 24h
    authenticated_ttl_seconds: int = 86400 * 7  # 7d


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


class KnowledgeQdrantSettings(BaseModel):
    enabled: bool = True
    url: str = "http://localhost:6333"
    api_key: Optional[str] = None
    prefer_grpc: bool = False
    timeout_seconds: float = 30.0


class KnowledgeProviderSettings(BaseModel):
    api_key: str = ""
    base_url: Optional[str] = None


class KnowledgeSettings(BaseModel):
    enabled: bool = True
    worker_concurrency: int = 2
    qdrant: KnowledgeQdrantSettings = Field(default_factory=KnowledgeQdrantSettings)
    openai: KnowledgeProviderSettings = Field(default_factory=KnowledgeProviderSettings)
    dashscope: KnowledgeProviderSettings = Field(default_factory=KnowledgeProviderSettings)


class Settings(BaseSettings):
    model_config = SettingsConfigDict(
        env_prefix="GATEWAY_",
        env_nested_delimiter="__",
        # Prefer repo-local env files (used by this project layout), but keep
        # compatibility with conventional root `.env` files as well.
        # Precedence (later overrides earlier):
        # - config/env.local: committed template/defaults
        # - config/.env: local actual config (e.g., port changes)
        # - .env / .env.local: conventional root env files (optional)
        env_file=("config/env.local", "config/.env", ".env", ".env.local"),
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

    # Anonymous identity + session policy
    anonymous: AnonymousIdentitySettings = Field(default_factory=AnonymousIdentitySettings)
    session: SessionSettings = Field(default_factory=SessionSettings)

    # 负载均衡
    load_balancer: LoadBalancerSettings = Field(default_factory=LoadBalancerSettings)

    # LangGraph 配置
    langgraph: LangGraphSettings = Field(default_factory=LangGraphSettings)

    # Knowledge Base (KBMS)
    knowledge: KnowledgeSettings = Field(default_factory=KnowledgeSettings)

    health_check_interval: int = 30
    task_worker_concurrency: int = 2
