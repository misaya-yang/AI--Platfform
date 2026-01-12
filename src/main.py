"""
AI Service Gateway - Application Entry Point

Unified AI service gateway with multi-protocol adapters, rate limiting,
circuit breaker, and session management.
"""

from __future__ import annotations

# Load environment variables from .env file BEFORE importing other modules
# This ensures env vars are available for module-level configurations
from pathlib import Path
from dotenv import load_dotenv

# Load .env from project root (one level up from src/)
_env_file = Path(__file__).parent.parent / ".env"
load_dotenv(_env_file, override=False)

from fastapi import FastAPI, Request
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import JSONResponse, PlainTextResponse

from .api.router import api_router
from .config.settings import Settings
from .container import Container, create_container, get_container
from .core.errors import (
    GatewayException,
    setup_exception_handlers,
)
from .core.observability.logging import configure_structured_logging, get_logger
from .core.observability.metrics import get_metrics
from .services.metrics.metrics_recorder import init_metrics_recorder
from .services.metrics.realtime_metrics import init_realtime_metrics
from .adapters.registry import auto_register_builtin_adapters
from .core.file_cleanup import get_cleanup_service
# 使用流式友好的纯 ASGI 中间件（替换 BaseHTTPMiddleware）
from .core.middleware.streaming import (
    StreamingAnonymousMiddleware,
    StreamingAnonymousConfig,
    StreamingAuthMiddleware,
    StreamingAuthConfig,
    StreamingRateLimitMiddleware,
    StreamingRateLimitConfig,
    StreamingLoggingMiddleware,
    StreamingLogConfig,
    StreamingTracingMiddleware,
    StreamingTracingConfig,
)

# 兼容旧的异常导入（向后兼容）
from .core.exceptions import (
    AuthError,
    CircuitBreakerOpenError,
    GatewayError,
    PermissionDeniedError,
    RateLimitExceededError,
    ServiceNotFoundError,
)

logger = get_logger(__name__)


def create_app() -> FastAPI:
    """
    创建 FastAPI 应用
    
    使用依赖注入容器管理所有组件。
    """
    # 加载配置
    settings = Settings()
    
    # 配置结构化日志
    configure_structured_logging(
        level="INFO",
        format_type="simple",  # 开发环境使用简单格式，生产可改为 "json"
    )
    
    # 创建依赖注入容器
    container = create_container(settings)
    
    # 自动注册内置适配器
    auto_register_builtin_adapters()
    
    # 创建 FastAPI 应用
    app = FastAPI(
        title="AI Service Gateway",
        version="2.0.0",
        description="统一 AI 服务网关，支持多协议适配、限流、熔断、会话管理等功能",
    )
    
    # ========== 中间件配置 ==========
    # 注意：中间件执行顺序与添加顺序相反（后添加的先执行）
    # 执行顺序：Tracing -> CORS -> RequestLogging -> RateLimit -> Auth -> AnonymousIdentity
    #
    # 重要：使用纯 ASGI 中间件替换 BaseHTTPMiddleware，避免缓冲 StreamingResponse
    # 这是解决首 token 延迟问题的关键

    # Stable anonymous identity for guest users (cookie/header) - 纯 ASGI
    anon_config = StreamingAnonymousConfig(
        enabled=getattr(settings.anonymous, 'enabled', True),
        header_name=getattr(settings.anonymous, 'header_name', 'X-AG-Anonymous-Id'),
        cookie_name=getattr(settings.anonymous, 'cookie_name', 'ag_anon_id'),
        ttl_days=getattr(settings.anonymous, 'ttl_days', 365),
        same_site=getattr(settings.anonymous, 'same_site', 'lax'),
    )
    app.add_middleware(StreamingAnonymousMiddleware, config=anon_config)

    # 统一鉴权中间件（支持 JWT、API Key、游客会话）- 纯 ASGI
    auth_config = StreamingAuthConfig(
        jwt_enabled=settings.authentication.jwt.enabled if hasattr(settings, 'authentication') else False,
        jwt_secret=settings.authentication.jwt.secret if hasattr(settings, 'authentication') else "",
        jwt_algorithms=settings.authentication.jwt.algorithms if hasattr(settings, 'authentication') else ["HS256"],
        api_key_enabled=settings.authentication.api_key.enabled if hasattr(settings, 'authentication') else False,
        guest_session_enabled=True,
        anonymous_enabled=True,
        whitelist_paths=["/health", "/health/live", "/health/ready", "/metrics", "/docs", "/openapi.json"],
    )
    app.add_middleware(StreamingAuthMiddleware, config=auth_config)

    # HTTP 级别限流中间件 - 纯 ASGI
    rate_limit_config = StreamingRateLimitConfig(
        enabled=True,
        global_limit=1000,
        global_window=60,
        user_limit=100,
        user_window=60,
        guest_limit=20,
        guest_window=60,
        ip_limit=50,
        ip_window=60,
        whitelist_paths=["/health", "/health/live", "/health/ready", "/metrics"],
    )
    app.add_middleware(StreamingRateLimitMiddleware, config=rate_limit_config)

    # 请求日志中间件 - 纯 ASGI
    request_log_config = StreamingLogConfig(
        enabled=True,
        log_request_body=False,
        log_response_body=False,
        exclude_paths=["/health", "/health/live", "/health/ready", "/metrics"],
    )
    app.add_middleware(StreamingLoggingMiddleware, config=request_log_config)

    # CORS 中间件（Starlette 内置，已经是纯 ASGI）
    app.add_middleware(
        CORSMiddleware,
        allow_origins=["*"],
        allow_credentials=True,
        allow_methods=["*"],
        allow_headers=["*"],
    )

    # 追踪中间件 - 纯 ASGI
    tracing_config = StreamingTracingConfig(
        service_name="gateway",
        log_requests=True,
        log_responses=True,
        exclude_paths={"/health", "/health/live", "/health/ready", "/metrics"},
    )
    app.add_middleware(StreamingTracingMiddleware, config=tracing_config)
    
    # ========== 存储组件引用到 app.state（向后兼容）==========
    
    # 注意：这些属性会在 startup 事件中设置，因为需要先初始化容器
    app.state.settings = settings
    app.state.container = container
    
    # ========== 路由 ==========
    
    app.include_router(api_router, prefix="/api/v1")
    # Compatibility alias for agents expecting `/v1/...`
    from .api.v1.knowledge import router as knowledge_router
    app.include_router(knowledge_router, prefix="/v1")
    
    # ========== 健康检查和指标端点 ==========
    
    @app.get("/health", tags=["Health"])
    async def health_check():
        """健康检查端点"""
        return {"status": "healthy", "version": "2.0.0"}
    
    @app.get("/health/live", tags=["Health"])
    async def liveness_check():
        """存活检查端点（K8s liveness probe）"""
        return {"status": "alive"}
    
    @app.get("/health/ready", tags=["Health"])
    async def readiness_check():
        """就绪检查端点（K8s readiness probe）"""
        container = get_container()
        
        checks = {"database": "unknown", "redis": "unknown"}
        healthy = True
        
        # 检查数据库
        if settings.database.enabled:
            try:
                database = container.database
                if database._pool:
                    checks["database"] = "healthy"
                else:
                    checks["database"] = "not_connected"
                    healthy = False
            except Exception as e:
                checks["database"] = f"error: {e}"
                healthy = False
        else:
            checks["database"] = "disabled"
        
        # 检查 Redis
        if settings.redis.enabled:
            try:
                redis = container.redis
                if await redis.ping():
                    checks["redis"] = "healthy"
                else:
                    checks["redis"] = "not_responding"
                    healthy = False
            except Exception as e:
                checks["redis"] = f"error: {e}"
                healthy = False
        else:
            checks["redis"] = "disabled"
        
        status_code = 200 if healthy else 503
        return JSONResponse(
            status_code=status_code,
            content={"status": "ready" if healthy else "not_ready", "checks": checks},
        )
    
    @app.get("/metrics", tags=["Observability"])
    async def metrics():
        """Prometheus 指标端点"""
        metrics_collector = get_metrics()
        return PlainTextResponse(
            content=metrics_collector.to_prometheus(),
            media_type="text/plain",
        )
    
    # ========== 生命周期事件 ==========
    
    @app.on_event("startup")
    async def startup():
        """应用启动"""
        logger.info("正在启动 AI Gateway...")
        
        # 初始化容器（连接数据库、Redis 等）
        await container.initialize()
        
        # 设置 app.state 属性（向后兼容）
        _setup_app_state(app, container)
        
        # 从数据库加载服务
        await _load_services_from_database(container, settings)
        
        # 启动后台任务
        await container.health_monitor.start()
        await container.task_worker.start(settings.task_worker_concurrency)

        # 启动计费拦截器（如果启用）
        if settings.proxy.enabled and settings.proxy.billing_enabled:
            billing_interceptor = container.billing_interceptor
            if billing_interceptor:
                await billing_interceptor.start()
                logger.info("计费拦截器已启动")

        # 启动 Knowledge Base (KBMS) 后台任务
        if getattr(settings, "knowledge", None) and settings.knowledge.enabled:
            from .services.knowledge.knowledge_service import KnowledgeService
            from .services.knowledge.worker import KnowledgeWorker

            # 初始化多模态嵌入服务（如果配置了 DashScope API Key）
            multimodal_embedding = None
            image_storage_service = None

            dashscope_key = getattr(
                getattr(settings, "knowledge", None), "dashscope", None
            )
            if dashscope_key and dashscope_key.api_key:
                try:
                    from .services.knowledge.embedding import DashScopeMultimodalEmbedding
                    multimodal_embedding = DashScopeMultimodalEmbedding(
                        model="qwen2.5-vl-embedding",
                        api_key=dashscope_key.api_key,
                    )
                    logger.info("多模态嵌入服务已初始化 (DashScope qwen2.5-vl-embedding)")
                except Exception as e:
                    logger.warning(f"多模态嵌入服务初始化失败: {e}")

            # 初始化图片存储服务（支持 S3/OSS/本地存储）
            # 独立于多模态嵌入初始化 - Confluence VLM 图片描述只需要存储服务
            if hasattr(settings, "storage") or dashscope_key:
                try:
                    from .services.storage.image_storage import (
                        ImageStorageService,
                        StorageConfig,
                        StorageBackend,
                    )
                    # 确定存储后端：优先使用配置的，否则使用本地存储
                    storage_backend = StorageBackend.LOCAL
                    if hasattr(settings, "storage"):
                        backend_str = getattr(settings.storage, "backend", "local")
                        try:
                            storage_backend = StorageBackend(backend_str)
                        except ValueError:
                            storage_backend = StorageBackend.LOCAL

                    storage_config = StorageConfig(
                        backend=storage_backend,
                        s3_bucket=getattr(getattr(settings, "storage", None), "s3", None) and getattr(settings.storage.s3, "bucket", "") or "",
                        s3_region=getattr(getattr(settings, "storage", None), "s3", None) and getattr(settings.storage.s3, "region", "") or "us-east-1",
                        s3_access_key=getattr(getattr(settings, "storage", None), "s3", None) and getattr(settings.storage.s3, "access_key", "") or "",
                        s3_secret_key=getattr(getattr(settings, "storage", None), "s3", None) and getattr(settings.storage.s3, "secret_key", "") or "",
                        s3_endpoint_url=getattr(getattr(settings, "storage", None), "s3", None) and getattr(settings.storage.s3, "endpoint_url", None) or None,
                        oss_bucket=getattr(getattr(settings, "storage", None), "oss", None) and getattr(settings.storage.oss, "bucket", "") or "",
                        oss_endpoint=getattr(getattr(settings, "storage", None), "oss", None) and getattr(settings.storage.oss, "endpoint", "") or "",
                        oss_access_key=getattr(getattr(settings, "storage", None), "oss", None) and getattr(settings.storage.oss, "access_key", "") or "",
                        oss_secret_key=getattr(getattr(settings, "storage", None), "oss", None) and getattr(settings.storage.oss, "secret_key", "") or "",
                        local_base_path=getattr(getattr(settings, "storage", None), "local_base_path", None) or "./data/images",
                        url_expiry_seconds=getattr(getattr(settings, "storage", None), "url_expiry_seconds", None) or 3600,
                    )
                    image_storage_service = ImageStorageService(storage_config)
                    app.state.image_storage_service = image_storage_service
                    logger.info(f"图片存储服务已初始化 (backend={storage_backend.value})")
                except Exception as e:
                    logger.warning(f"图片存储服务初始化失败: {e}")

            app.state.knowledge_service = KnowledgeService(
                settings=settings,
                database=container.database,
                multimodal_embedding=multimodal_embedding,
                image_storage_service=image_storage_service,
            )
            app.state.knowledge_worker = KnowledgeWorker(app.state.knowledge_service)
            await app.state.knowledge_worker.start(settings.knowledge.worker_concurrency)

            if multimodal_embedding:
                logger.info(f"知识库服务已启动 (支持图片嵌入, worker_concurrency={settings.knowledge.worker_concurrency})")
            else:
                logger.info(f"知识库服务已启动 (仅文本, worker_concurrency={settings.knowledge.worker_concurrency})")
        else:
            logger.warning(
                f"知识库服务未启动: knowledge={getattr(settings, 'knowledge', None)}, "
                f"enabled={getattr(getattr(settings, 'knowledge', None), 'enabled', None)}"
            )

        # 启动 Confluence 集成服务（如果启用）
        if getattr(settings, "confluence", None) and settings.confluence.enabled:
            from .services.knowledge.confluence.sync_service import ConfluenceSyncService
            from .services.knowledge.confluence.scheduler import ConfluenceScheduler

            # 复用 Knowledge Service 的图片处理服务
            confluence_image_storage = getattr(app.state, "image_storage_service", None)
            confluence_multimodal = None
            confluence_vlm_service = None

            # 如果 Knowledge Service 有多模态嵌入，从中获取
            if hasattr(app.state, "knowledge_service") and app.state.knowledge_service:
                confluence_multimodal = getattr(
                    app.state.knowledge_service, "multimodal_embedding", None
                )
                confluence_image_storage = getattr(
                    app.state.knowledge_service, "image_storage_service", None
                ) or confluence_image_storage

            # 初始化 VLM 服务用于图片描述生成
            dashscope_key = getattr(
                getattr(settings, "knowledge", None), "dashscope", None
            )
            if dashscope_key and dashscope_key.api_key and confluence_image_storage:
                try:
                    from .services.knowledge.vlm_service import DashScopeVLMService
                    confluence_vlm_service = DashScopeVLMService(
                        api_key=dashscope_key.api_key,
                        model="qwen-vl-max",
                    )
                    logger.info("Confluence VLM 服务已初始化 (qwen-vl-max)")
                except Exception as e:
                    logger.warning(f"Confluence VLM 服务初始化失败: {e}")

            app.state.confluence_sync_service = ConfluenceSyncService(
                settings=settings,
                database=container.database,
                knowledge_service=app.state.knowledge_service,
                knowledge_worker=app.state.knowledge_worker,
                image_storage_service=confluence_image_storage,
                multimodal_embedding=confluence_multimodal,
                vlm_service=confluence_vlm_service,
            )

            # 启动调度器（支持绑定级别的 sync_mode 配置）
            # 调度器会自动加载 sync_mode = "polling" 的绑定和页面
            polling_enabled = getattr(settings.confluence, "polling_enabled", False)

            if polling_enabled:
                app.state.confluence_scheduler = ConfluenceScheduler(
                    sync_service=app.state.confluence_sync_service,
                    max_concurrent=getattr(settings.confluence, "sync_max_concurrent", 3),
                    check_interval_seconds=getattr(
                        settings.confluence, "polling_check_interval_seconds", 30
                    ),
                    test_mode=getattr(settings.confluence, "test_mode", False),
                    test_interval_seconds=getattr(
                        settings.confluence, "test_polling_interval_seconds", 10
                    ),
                )
                await app.state.confluence_scheduler.start()
                logger.info("Confluence 调度器已启动 (支持绑定级别轮询)")
            else:
                app.state.confluence_scheduler = None
                logger.info("Confluence 调度器未启动 (polling_enabled=False)")

            logger.info("Confluence 集成服务已启动")

        # 启动文件清理服务
        file_cleanup_service = get_cleanup_service()
        await file_cleanup_service.start()
        app.state.file_cleanup_service = file_cleanup_service

        # 打印启动信息
        _print_startup_info(settings)
    
    @app.on_event("shutdown")
    async def shutdown():
        """应用关闭"""
        logger.info("正在关闭 AI Gateway...")

        # Stop Confluence scheduler/service first (if enabled)
        confluence_scheduler = getattr(app.state, "confluence_scheduler", None)
        if confluence_scheduler is not None:
            await confluence_scheduler.stop()

        # Stop KBMS worker/service (if enabled)
        kb_worker = getattr(app.state, "knowledge_worker", None)
        if kb_worker is not None:
            await kb_worker.stop()
        kb_service = getattr(app.state, "knowledge_service", None)
        if kb_service is not None:
            await kb_service.close()

        # Stop file cleanup service
        file_cleanup_service = getattr(app.state, "file_cleanup_service", None)
        if file_cleanup_service is not None:
            await file_cleanup_service.stop()

        await container.shutdown()
        logger.info("AI Gateway 已关闭")
    
    # ========== 异常处理 ==========
    
    # 设置新的异常处理器
    setup_exception_handlers(app, debug=False, include_trace_id=True)
    
    # 向后兼容：处理旧的 GatewayError 异常
    @app.exception_handler(GatewayError)
    async def handle_legacy_gateway_error(request: Request, exc: GatewayError):
        """处理旧版 GatewayError（向后兼容）"""
        status = 400
        if isinstance(exc, AuthError):
            status = 401
        elif isinstance(exc, PermissionDeniedError):
            status = 403
        elif isinstance(exc, RateLimitExceededError):
            status = 429
        elif isinstance(exc, ServiceNotFoundError):
            status = 404
        elif isinstance(exc, CircuitBreakerOpenError):
            status = 503
        
        return JSONResponse(
            status_code=status,
            content={"error": str(exc)},
        )
    
    return app


def _setup_app_state(app: FastAPI, container: Container) -> None:
    """
    设置 app.state 属性

    为向后兼容，将容器中的组件暴露到 app.state。
    """
    app.state.registry = container.service_registry
    app.state.load_balancer = container.load_balancer
    app.state.dispatcher = container.dispatcher
    app.state.task_manager = container.task_manager
    app.state.session_manager = container.session_manager
    app.state.health_monitor = container.health_monitor
    app.state.task_worker = container.task_worker
    app.state.database = container.database
    app.state.redis = container.redis

    # LangGraph 相关
    app.state.langgraph_proxy = container.langgraph_proxy
    app.state.multi_rate_limiter = container.multi_rate_limiter
    app.state.user_resolver = container.user_resolver

    # 透明代理相关
    app.state.transparent_proxy = container.transparent_proxy
    app.state.proxy_config_loader = container.proxy_config_loader
    app.state.billing_interceptor = container.billing_interceptor
    app.state.context_injector = container.context_injector

    # Knowledge Base (KBMS)
    app.state.knowledge_service = None
    app.state.knowledge_worker = None

    # Confluence 集成
    app.state.confluence_sync_service = None
    app.state.confluence_scheduler = None

    # 游客会话管理器
    from .services.session.guest_session_manager import GuestSessionManager, GuestSessionConfig
    app.state.guest_session_manager = GuestSessionManager(
        config=GuestSessionConfig(),
        redis_client=container.redis,
    )

    # Initialize metrics recorder with Redis for dashboard
    init_metrics_recorder(container.redis)

    # Initialize realtime metrics service for LangSmith-style dashboard
    init_realtime_metrics(container.redis)


async def _load_services_from_database(container: Container, settings: Settings) -> None:
    """从数据库加载服务"""
    if not settings.database.enabled:
        return
    
    database = container.database
    if not database._pool:
        return
    
    try:
        from .services.registry.database_storage import DatabaseRegistryStorage
        
        db_services = await database.list_services()
        registry = container.service_registry
        registry_storage = registry.storage
        
        loaded_count = 0
        for svc in db_services:
            try:
                if isinstance(registry_storage, DatabaseRegistryStorage):
                    service = registry_storage._dict_to_service(svc)
                    registry._cache[service.service_id] = service
                    loaded_count += 1
            except Exception as e:
                logger.warning(f"加载服务 {svc.get('service_id')} 失败: {e}")
        
        if loaded_count > 0:
            logger.info(f"从数据库加载了 {loaded_count} 个服务")
    except Exception as e:
        logger.warning(f"从数据库加载服务失败: {e}")


def _print_startup_info(settings: Settings) -> None:
    """打印启动信息"""
    print("\n" + "=" * 50)
    print("AI Gateway 已启动")
    print(f"   地址: http://{settings.host}:{settings.port}")
    print(f"   API: http://{settings.host}:{settings.port}/api/v1")
    print(f"   数据库: {'已启用' if settings.database.enabled else '未启用'}")
    print(f"   Redis: {'已启用' if settings.redis.enabled else '未启用'}")
    print("=" * 50 + "\n")


# 创建应用实例
app = create_app()
