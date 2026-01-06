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
    # Permission cache TTL in seconds (0 to disable).
    permission_cache_ttl_seconds: int = 60


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

    # 内部认证配置（用于网关与 LangGraph 服务通信）
    # 如果 LangGraph 服务配置了自定义认证，需要设置此 token
    # GATEWAY_LANGGRAPH__AUTH_TOKEN=your-internal-api-token
    auth_token: str = ""

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


class ConfluenceSettings(BaseModel):
    """Confluence 集成配置"""
    enabled: bool = False

    # 同步配置
    sync_batch_size: int = 25  # 每批处理的页面数
    sync_max_concurrent: int = 3  # 最大并发同步任务数
    sync_retry_max: int = 3  # 最大重试次数
    sync_retry_delay: float = 5.0  # 重试延迟（秒）

    # 定时轮询配置
    polling_enabled: bool = False  # 是否启用定时轮询
    polling_default_interval_minutes: int = 60  # 默认轮询间隔（分钟）

    # API 客户端配置
    request_timeout: float = 30.0  # 请求超时（秒）
    max_retries: int = 3  # 最大重试次数

    # Webhook 配置 (二期)
    webhook_enabled: bool = False
    webhook_callback_base_url: str = ""  # Webhook 回调基础 URL


class ProxySettings(BaseModel):
    """透明代理配置"""
    enabled: bool = True
    
    # 默认超时配置
    timeout_connect: float = 5.0
    timeout_read: float = 300.0
    timeout_write: float = 60.0
    timeout_pool: float = 60.0
    
    # 配置缓存 TTL（秒）
    config_cache_ttl: float = 60.0
    
    # 计费配置
    billing_enabled: bool = True
    billing_buffer_size: int = 100
    billing_flush_interval: float = 5.0
    
    # 上下文注入配置
    inject_user_info: bool = True
    inject_request_info: bool = True
    forward_auth: bool = True
    forward_all_headers: bool = True
    
    # 自定义头部（所有请求都会注入）
    custom_headers: Dict[str, str] = Field(default_factory=dict)


class Settings(BaseSettings):
    """
    Application settings loaded from environment variables.

    Configuration sources (in order of precedence):
    1. Environment variables (highest priority)
    2. .env file in project root

    All settings use GATEWAY_ prefix with double underscore for nested values.
    Example: GATEWAY_DATABASE__ENABLED=true
    """
    model_config = SettingsConfigDict(
        env_prefix="GATEWAY_",
        env_nested_delimiter="__",
        env_file=".env",
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

    # Confluence 集成
    confluence: ConfluenceSettings = Field(default_factory=ConfluenceSettings)

    # 透明代理配置
    proxy: ProxySettings = Field(default_factory=ProxySettings)

    health_check_interval: int = 30
    task_worker_concurrency: int = 2
