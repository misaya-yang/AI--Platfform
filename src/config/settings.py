from __future__ import annotations

import os
from typing import Any

from pydantic import BaseModel, Field, field_validator
from pydantic_settings import BaseSettings, SettingsConfigDict


def _get_int_env(key: str, default: int) -> int:
    """Get integer from environment variable."""
    val = os.getenv(key)
    if val:
        try:
            return int(val)
        except ValueError:
            pass
    return default


class DatabaseSettings(BaseModel):
    """PostgreSQL 数据库配置

    SECURITY NOTE:
    - DSN must be explicitly configured via environment variables
    - auto_init is disabled by default to prevent accidental schema changes
    """

    enabled: bool = False
    # SECURITY: No default credentials - must be configured via env
    dsn: str = "postgresql://localhost:5432/gateway"
    # SECURITY: Disabled by default - enable explicitly in development
    auto_init: bool = False
    # Permission cache TTL in seconds (0 to disable).
    permission_cache_ttl_seconds: int = 60
    # Connection pool settings
    pool_min_size: int = 2
    pool_max_size: int = 10


class RedisSettings(BaseModel):
    """Redis 缓存配置"""

    enabled: bool = False
    url: str = "redis://localhost:6379/0"


class AuthJWTSettings(BaseModel):
    enabled: bool = False
    secret: str = Field(default="")
    algorithms: list[str] = Field(default_factory=lambda: ["HS256"])
    audience: str | None = None
    issuer: str | None = None


class AuthAPIKeySettings(BaseModel):
    enabled: bool = False
    header_name: str = "X-API-Key"
    keys: list[str] = Field(default_factory=list)


class AuthenticationSettings(BaseModel):
    jwt: AuthJWTSettings = Field(default_factory=AuthJWTSettings)
    api_key: AuthAPIKeySettings = Field(default_factory=AuthAPIKeySettings)


class RBACSettings(BaseModel):
    roles: dict[str, list[str]] = Field(
        default_factory=lambda: {
            # Keep fallback roles aligned with DB-backed RBAC defaults.
            # When DB is unavailable these mappings remain capability-compatible.
            "guest": ["console:dashboard:view"],
            "user": [
                "console:dashboard:view",
                "conversation:playground:access",
                "conversation:thread:create",
            ],
            "manager": [
                "console:dashboard:view",
                "console:services:view",
                "console:settings:view",
                "conversation:playground:access",
                "conversation:thread:create",
                "conversation:thread:delete",
                "knowledge:dataset:create",
                "knowledge:dataset:edit",
                "knowledge:dataset:view",
                "user:list",
            ],
            "cs_staff": [
                "console:dashboard:view",
                "conversation:playground:access",
                "conversation:thread:create",
                "conversation:thread:delete",
                "knowledge:dataset:view",
            ],
            "sales_staff": [
                "console:dashboard:view",
                "conversation:playground:access",
                "conversation:thread:create",
                "conversation:thread:delete",
                "knowledge:dataset:view",
            ],
            "developer": [
                "console:dashboard:view",
                "console:services:view",
                "console:services:edit",
                "console:settings:view",
                "conversation:playground:access",
                "conversation:thread:create",
                "conversation:thread:delete",
                "knowledge:dataset:create",
                "knowledge:dataset:edit",
                "knowledge:dataset:view",
            ],
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
    global_limit: dict[str, Any] | None = None
    tenant_limits: dict[str, dict[str, Any]] = Field(default_factory=dict)
    user_limits: dict[str, dict[str, Any]] = Field(default_factory=dict)
    service_limits: dict[str, dict[str, Any]] = Field(default_factory=dict)
    ip_limits: dict[str, Any] | None = None


class CORSSettings(BaseModel):
    """CORS configuration."""

    allow_origins: list[str] = Field(
        default_factory=lambda: ["http://localhost:3000", "http://127.0.0.1:3000"]
    )
    allow_credentials: bool = True
    allow_methods: list[str] = Field(default_factory=lambda: ["*"])
    allow_headers: list[str] = Field(default_factory=lambda: ["*"])
    expose_headers: list[str] = Field(default_factory=list)
    max_age: int = 600
    allow_origin_regex: str | None = None


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
    load_balancer_strategy: str = (
        "least_connections"  # round_robin | least_connections | weighted | latency_based
    )
    health_check_interval: int = 10

    # LangGraph Server 实例列表
    instances: list[LangGraphInstanceSettings] = Field(default_factory=list)

    # 也可以通过环境变量配置（逗号分隔的 URL 列表）
    # GATEWAY_LANGGRAPH__INSTANCE_URLS=http://langgraph-1:8000,http://langgraph-2:8000
    instance_urls: str = ""  # 逗号分隔的实例 URL 列表

    # 内部认证配置（用于网关与 LangGraph 服务通信）
    # 如果 LangGraph 服务配置了自定义认证，需要设置此 token
    # GATEWAY_LANGGRAPH__AUTH_TOKEN=your-internal-api-token
    auth_token: str = ""

    # 用户层级限流配置 (requests per window seconds)
    # 生产环境使用安全默认值
    # 开发环境可通过环境变量调整: RATE_LIMIT_ANONYMOUS_LIMIT=1000
    tier_limits: dict[str, dict[str, int]] = Field(
        default_factory=lambda: {
            "anonymous": {"requests": _get_int_env("RATE_LIMIT_ANONYMOUS_LIMIT", 10), "window": 60},
            "normal": {"requests": _get_int_env("RATE_LIMIT_NORMAL_LIMIT", 60), "window": 60},
            "premium": {"requests": _get_int_env("RATE_LIMIT_PREMIUM_LIMIT", 300), "window": 60},
            "enterprise": {
                "requests": _get_int_env("RATE_LIMIT_ENTERPRISE_LIMIT", 1000),
                "window": 60,
            },
        }
    )


class KnowledgeQdrantSettings(BaseModel):
    enabled: bool = True
    url: str = "http://localhost:6333"
    api_key: str | None = None
    prefer_grpc: bool = False
    # Increased timeout for large document processing
    # 7MB+ PDFs with hundreds of chunks need more time
    timeout_seconds: float = 120.0
    max_retries: int = 5
    retry_base_delay: float = 2.0  # Increased base delay for better recovery


class KnowledgeProviderSettings(BaseModel):
    api_key: str = ""
    base_url: str | None = None


class KnowledgeGeminiSettings(BaseModel):
    """Gemini Embedding API 配置"""

    api_key: str = ""
    model: str = "gemini-embedding-001"
    dimension: int = 1024
    base_url: str | None = None
    timeout_seconds: float = 30.0


class KnowledgeSiliconFlowSettings(BaseModel):
    """SiliconFlow Embedding API 配置

    支持模型：
    - BAAI/bge-m3: 1024 dimensions
    - Pro/BAAI/bge-m3: 1024 dimensions
    - BAAI/bge-large-zh-v1.5: 1024 dimensions
    - BAAI/bge-large-en-v1.5: 1024 dimensions
    - netease-youdao/bce-embedding-base_v1: 512 dimensions
    """

    api_key: str = ""
    base_url: str = "https://api.siliconflow.cn/v1"
    timeout_seconds: float = 30.0


class KnowledgeIslamicProfileSettings(BaseModel):
    """Islamic retrieval profile defaults (applied only on explicit Islamic opt-in)."""

    enabled: bool = True

    # Retrieval defaults
    top_k: int = 8
    score_threshold: float = 0.3
    rerank_enabled: bool = False
    rerank_provider: str = "bge"
    rerank_model: str = "bge-reranker-v2-m3"

    # Islamic retrieval enhancements
    multi_query: bool = False
    citation_format: bool = True
    authority_sort: bool = True
    strict_section_traceability: bool = True
    max_expanded_queries: int = 3


class KnowledgeSettings(BaseModel):
    enabled: bool = True
    worker_concurrency: int = 2
    qdrant: KnowledgeQdrantSettings = Field(default_factory=KnowledgeQdrantSettings)
    dashscope: KnowledgeProviderSettings = Field(default_factory=KnowledgeProviderSettings)
    gemini: KnowledgeGeminiSettings = Field(default_factory=KnowledgeGeminiSettings)
    siliconflow: KnowledgeSiliconFlowSettings = Field(default_factory=KnowledgeSiliconFlowSettings)
    islamic_profile: KnowledgeIslamicProfileSettings = Field(
        default_factory=KnowledgeIslamicProfileSettings
    )

    # =============================================
    # Text Embedding Configuration (for text-only datasets)
    # Uses Gemini by default for high-speed embedding
    # =============================================
    text_embedding_provider: str = "gemini"  # gemini | dashscope | siliconflow
    text_embedding_model: str = "gemini-embedding-001"
    text_embedding_dimension: int = 1024
    text_embedding_batch_size: int = 50  # Gemini supports 100, use 50 for safety
    text_embedding_max_concurrent: int = 5  # Reduced from 10 to avoid API overload

    # =============================================
    # Multimodal Embedding Configuration (for image+text datasets)
    # Uses DashScope for unified vector space (cross-modal search)
    # =============================================
    multimodal_embedding_provider: str = "dashscope"
    multimodal_embedding_model: str = "tongyi-embedding-vision-plus"
    multimodal_embedding_dimension: int = 1024  # Unified to 1024 as required
    multimodal_embedding_max_concurrent: int = 5

    # =============================================
    # VLM (Vision Language Model) Configuration
    # For image description (e.g. Confluence)
    # =============================================
    vlm_batch_size: int = 5
    vlm_max_concurrent: int = 8
    vlm_timeout_seconds: float = 90.0
    vlm_quality_threshold: float = 0.5

    # =============================================
    # Document Processing Configuration
    # =============================================
    document_worker_concurrency: int = 3  # Max concurrent document processing

    # Scanned PDF: min embeddable images to use image-only (no text extraction)
    scanned_min_images_for_image_only: int = 5

    # OCR fallback for scanned/low-text PDFs
    ocr_enabled: bool = True
    ocr_languages: str = "eng+ara"
    ocr_render_dpi: int = 200
    pdf_min_text_chars_for_ocr: int = 200
    ocr_tesseract_timeout_seconds: int = 60
    # Moderate OCR parallelism to speed up scanned PDFs without overloading the machine
    ocr_page_concurrency: int = 3

    # Image segments use a separate position range to avoid conflicts with text segments
    image_position_offset: int = 1_000_000

    # =============================================
    # Large File & Streaming Processing
    # =============================================
    # Size threshold for large files (bytes) - files larger than this use streaming
    large_file_threshold: int = 50 * 1024 * 1024  # 50MB

    # Maximum content size for memory-based processing (bytes)
    max_memory_processing_size: int = 100 * 1024 * 1024  # 100MB

    # Streaming batch size for large document processing (pages per batch)
    streaming_batch_size: int = 20

    # Streaming batch size limits
    streaming_min_batch_size: int = 5
    streaming_max_batch_size: int = 50

    # =============================================
    # Document Type Detection
    # =============================================
    # Number of pages to sample for document type detection
    detection_sample_pages: int = 5

    # Text coverage thresholds for PDF classification
    detection_native_pdf_threshold: float = 0.8  # > 80% pages with text -> native
    detection_scanned_pdf_threshold: float = 0.2  # < 20% pages with text -> scanned

    # Minimum characters per page to consider it "has text"
    detection_min_chars_per_page: int = 50

    # =============================================
    # Hierarchical Indexing (L1/L2/L3)
    # =============================================
    # Default chunk sizes for hierarchical indexing (in characters, ~4 chars per token)
    hierarchical_l2_chunk_size: int = 8000  # ~2000 tokens (section level)
    hierarchical_l2_chunk_overlap: int = 400
    hierarchical_l3_chunk_size: int = 2000  # ~500 tokens (paragraph level)
    hierarchical_l3_chunk_overlap: int = 200

    # Default top-k values for hierarchical retrieval
    hierarchical_l1_top_k: int = 5  # Documents to consider at L1 (summary level)
    hierarchical_l2_top_k: int = 10  # Sections to consider at L2 (section level)
    hierarchical_l3_top_k: int = 5  # Final results at L3 (paragraph level)

    @field_validator("ocr_languages")
    @classmethod
    def validate_ocr_languages(cls, v: str) -> str:
        """Validate OCR languages against whitelist.

        Supports single languages (e.g., "eng", "ara") and combinations (e.g., "eng+ara").
        """
        # Single language whitelist
        ALLOWED_SINGLE_LANGS = frozenset(
            {
                "eng",
                "ara",
                "chi_sim",
                "chi_tra",
                "fra",
                "deu",
                "spa",
                "rus",
                "jpn",
                "kor",
                "por",
                "ita",
                "nld",
                "tur",
                "vie",
                "tha",
            }
        )
        # Pre-defined combinations
        ALLOWED_COMBINATIONS = frozenset(
            {
                "eng+ara",
                "ara+eng",
                "chi_sim+eng",
                "jpn+eng",
                "kor+eng",
            }
        )

        if not v:
            return "eng+ara"

        # Check pre-defined combinations first
        if v in ALLOWED_COMBINATIONS:
            return v

        # Check single language
        if v in ALLOWED_SINGLE_LANGS:
            return v

        # Validate custom combinations (e.g., "eng+fra")
        if "+" in v:
            parts = v.split("+")
            if all(p.strip() in ALLOWED_SINGLE_LANGS for p in parts):
                return v
            raise ValueError(f"Invalid OCR language combination: {v}. Use format like 'eng+ara'.")

        raise ValueError(
            f"Invalid OCR language: {v}. Use single language code or combination like 'eng+ara'."
        )


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
    polling_min_interval_minutes: int = 1  # 最小轮询间隔（分钟），用于测试
    polling_check_interval_seconds: int = 30  # 调度器检查间隔（秒）

    # 测试模式配置
    test_mode: bool = False  # 启用测试模式（允许更短的轮询间隔）
    test_polling_interval_seconds: int = 10  # 测试模式下的轮询间隔（秒）

    # API 客户端配置
    request_timeout: float = 30.0  # 请求超时（秒）
    max_retries: int = 3  # 最大重试次数

    # Webhook 配置 (二期)
    webhook_enabled: bool = False
    webhook_callback_base_url: str = ""  # Webhook 回调基础 URL

    # 安全配置
    encryption_key: str = ""  # 用于加密 API Token 的密钥（推荐设置为 32 字符随机字符串）

    # 客户端缓存配置
    client_cache_ttl_seconds: int = 300  # 客户端缓存 TTL（秒），默认 5 分钟


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
    custom_headers: dict[str, str] = Field(default_factory=dict)


class StorageS3Settings(BaseModel):
    """S3 存储配置"""

    bucket: str = ""
    region: str = "us-east-1"
    access_key: str = ""
    secret_key: str = ""
    endpoint_url: str | None = None  # 用于 S3 兼容服务


class StorageOSSSettings(BaseModel):
    """阿里云 OSS 存储配置"""

    bucket: str = ""
    endpoint: str = ""  # e.g., oss-cn-beijing.aliyuncs.com
    access_key: str = ""
    secret_key: str = ""


class StorageSettings(BaseModel):
    """对象存储配置（用于 Confluence 图片等）"""

    backend: str = "local"  # local | s3 | oss
    local_base_path: str = "./data/images"
    url_expiry_seconds: int = 3600
    key_prefix: str = (
        "dev"  # 环境前缀，如 "dev", "staging", "prod"（与 .env.example 和 docker-compose.yml 一致）
    )

    s3: StorageS3Settings = Field(default_factory=StorageS3Settings)
    oss: StorageOSSSettings = Field(default_factory=StorageOSSSettings)


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
    cors: CORSSettings = Field(default_factory=CORSSettings)

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

    # 对象存储配置（用于 Confluence 图片等）
    storage: StorageSettings = Field(default_factory=StorageSettings)

    health_check_interval: int = 30
    task_worker_concurrency: int = 2
