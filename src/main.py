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

from .adapters.registry import auto_register_builtin_adapters
from .api.router import api_router
from .config.settings import Settings
from .container import Container, create_container, get_container
from .core.errors import (
    setup_exception_handlers,
)

# 兼容旧的异常导入（向后兼容）
from ai_gateway_core.exceptions import (
    AuthError,
    CircuitBreakerOpenError,
    GatewayError,
    PermissionDeniedError,
    RateLimitExceededError,
    ServiceNotFoundError,
)
from .core.file_cleanup import get_cleanup_service

# 使用流式友好的纯 ASGI 中间件（替换 BaseHTTPMiddleware）
from .core.middleware.streaming import (
    StreamingAnonymousConfig,
    StreamingAnonymousMiddleware,
    StreamingAuthConfig,
    StreamingAuthMiddleware,
    StreamingLogConfig,
    StreamingLoggingMiddleware,
    StreamingRateLimitConfig,
    StreamingRateLimitMiddleware,
    StreamingTracingConfig,
    StreamingTracingMiddleware,
)
from ai_gateway_core.logging import configure_structured_logging, get_logger
from .core.observability.metrics import get_metrics
from .services.metrics.metrics_recorder import init_metrics_recorder
from .services.metrics.realtime_metrics import init_realtime_metrics

logger = get_logger(__name__)

OPENAPI_TAGS = [
    {"name": "Health", "description": "Service health checks, readiness state, and provider connectivity."},
    {"name": "Auth", "description": "User authentication — login, logout, password management, token validation."},
    {"name": "Sessions", "description": "Conversation session management — CRUD, message history, per-user isolation."},
    {"name": "LangGraph", "description": "LangGraph Platform proxy — assistants, threads, runs (streaming/sync), and key-value store."},
    {"name": "Knowledge", "description": "Knowledge base proxy — dataset management, document upload, and RAG retrieval."},
    {"name": "Islamic Content", "description": "Quran / Hadith / Dua / Wahda content proxy to Islamic Content Service."},
    {"name": "Quiz", "description": "AI quiz system — generation, submission, scoring, and exam management."},
    {"name": "Dashboard", "description": "Real-time metrics, usage timeseries, and operational dashboard data."},
]


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
        description=(
            "Unified AI service gateway with multi-protocol adapters, rate limiting, circuit breaking, and session management.\n\n"
            "**Core features:**\n"
            "- JWT authentication with RBAC and multi-tenant isolation\n"
            "- LangGraph Platform proxy with load balancing and streaming SSE\n"
            "- Knowledge base management with hybrid RAG retrieval\n"
            "- Islamic content (Quran, Hadith, Dua) via dedicated microservice\n"
            "- AI quiz/exam system with scoring and analytics\n"
            "- Real-time usage metrics and billing\n\n"
            "**Auth:** All endpoints (except `/health` and `/api/v1/auth/login`) require `Authorization: Bearer <token>`."
        ),
        openapi_tags=OPENAPI_TAGS,
        contact={"name": "Hejaz AI Team", "email": "tech@hejazfs.com.au"},
        license_info={"name": "Proprietary"},
    )

    # ========== 中间件配置 ==========
    # 注意：中间件执行顺序与添加顺序相反（后添加的先执行）
    # 执行顺序：Tracing -> CORS -> RequestLogging -> Auth -> RateLimit -> AnonymousIdentity
    #
    # 重要：使用纯 ASGI 中间件替换 BaseHTTPMiddleware，避免缓冲 StreamingResponse
    # 这是解决首 token 延迟问题的关键

    # Stable anonymous identity for guest users (cookie/header) - 纯 ASGI
    anon_config = StreamingAnonymousConfig(
        enabled=getattr(settings.anonymous, "enabled", True),
        header_name=getattr(settings.anonymous, "header_name", "X-AG-Anonymous-Id"),
        cookie_name=getattr(settings.anonymous, "cookie_name", "ag_anon_id"),
        ttl_days=getattr(settings.anonymous, "ttl_days", 365),
        same_site=getattr(settings.anonymous, "same_site", "lax"),
    )
    app.add_middleware(StreamingAnonymousMiddleware, config=anon_config)

    # HTTP 级别限流中间件 - 纯 ASGI
    # Note: Limits should be generous enough for frontend usage patterns
    # (multiple concurrent API calls on page load)
    rate_limit_config = StreamingRateLimitConfig(
        enabled=True,
        global_limit=5000,  # High global limit for overall traffic
        global_window=60,
        user_limit=300,  # Authenticated users: 300/min
        user_window=60,
        guest_limit=200,  # Guests: 200/min (frontend makes many calls)
        guest_window=60,
        ip_limit=500,  # Per-IP: 500/min (shared IPs, proxies)
        ip_window=60,
        whitelist_paths=[
            "/health",
            "/health/live",
            "/health/ready",
            "/metrics",
            "/docs",
            "/openapi.json",
            # Frontend metadata endpoints (high frequency, low cost)
            "/api/v1/services",
            "/api/v1/assistant/config",
            "/api/v1/assistant/models",
            "/api/v1/assistant/datasets",
        ],
    )
    app.add_middleware(StreamingRateLimitMiddleware, config=rate_limit_config)

    # 统一鉴权中间件（支持 JWT、API Key、游客会话）- 纯 ASGI
    auth_config = StreamingAuthConfig(
        jwt_enabled=settings.authentication.jwt.enabled
        if hasattr(settings, "authentication")
        else False,
        jwt_secret=settings.authentication.jwt.secret
        if hasattr(settings, "authentication")
        else "",
        jwt_algorithms=settings.authentication.jwt.algorithms
        if hasattr(settings, "authentication")
        else ["HS256"],
        api_key_enabled=settings.authentication.api_key.enabled
        if hasattr(settings, "authentication")
        else False,
        guest_session_enabled=True,
        anonymous_enabled=True,
        whitelist_paths=[
            "/health",
            "/health/live",
            "/health/ready",
            "/metrics",
            "/docs",
            "/openapi.json",
        ],
    )
    app.add_middleware(StreamingAuthMiddleware, config=auth_config)

    # 请求日志中间件 - 纯 ASGI
    request_log_config = StreamingLogConfig(
        enabled=True,
        log_request_body=False,
        log_response_body=False,
        exclude_paths=["/health", "/health/live", "/health/ready", "/metrics"],
    )
    app.add_middleware(StreamingLoggingMiddleware, config=request_log_config)

    # CORS 中间件（Starlette 内置，已经是纯 ASGI）
    cors = getattr(settings, "cors", None)
    allow_origins = cors.allow_origins if cors else ["*"]
    allow_credentials = cors.allow_credentials if cors else True
    if allow_credentials and "*" in allow_origins:
        logger.warning(
            "CORS allow_origins includes '*' while allow_credentials is enabled; "
            "disabling credentials to avoid unsafe wildcard usage."
        )
        allow_credentials = False
    app.add_middleware(
        CORSMiddleware,
        allow_origins=allow_origins,
        allow_credentials=allow_credentials,
        allow_methods=cors.allow_methods if cors else ["*"],
        allow_headers=cors.allow_headers if cors else ["*"],
        expose_headers=cors.expose_headers if cors else None,
        max_age=cors.max_age if cors else 600,
        allow_origin_regex=cors.allow_origin_regex if cors else None,
    )

    # Graceful drain — flips a process-singleton flag on SIGTERM/SIGINT
    # (handlers installed in the startup event below) so new requests are
    # rejected with 503 + Retry-After while in-flight requests get to finish.
    # The shutdown event awaits ``DRAIN.wait_drained`` before tearing down
    # container singletons (DB pool, Redis, billing interceptor). Health-probe
    # paths bypass DRAIN entirely so the LB still sees readiness during drain.
    #
    # Cross-link: same install pattern lives in
    # ``apps/assistant-service/src/assistant_service/main.py`` and
    # ``apps/knowledge-service/src/knowledge_service/main.py``.
    from ai_gateway_core.proxy import DrainMiddleware
    app.add_middleware(DrainMiddleware)

    # OpenTelemetry inbound server-span middleware. Sits AFTER CORS (added
    # below; remember Starlette executes last-added FIRST, so CORS still
    # wraps OTel). Reads W3C ``traceparent`` from inbound headers, opens
    # a server span, and exposes the active context on ``request.state``
    # so the proxy layer forwards it intact to assistant-service / KS.
    # ``init_tracing`` is called in the startup handler below so the
    # middleware's tracer reference is valid by request time.
    from ai_gateway_core.tracing import OTelInboundMiddleware
    app.add_middleware(OTelInboundMiddleware)

    # Security response headers
    @app.middleware("http")
    async def security_headers(request, call_next):
        response = await call_next(request)
        response.headers["X-Content-Type-Options"] = "nosniff"
        response.headers["X-Frame-Options"] = "DENY"
        response.headers["Strict-Transport-Security"] = "max-age=31536000; includeSubDomains"
        response.headers["X-XSS-Protection"] = "1; mode=block"
        response.headers["Referrer-Policy"] = "strict-origin-when-cross-origin"
        return response

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

    # ========== Scalar API 文档（现代化 UI + 可调用）==========
    from scalar_fastapi import Layout, get_scalar_api_reference

    @app.get("/scalar", include_in_schema=False)
    async def scalar_html():
        return get_scalar_api_reference(
            openapi_url=app.openapi_url,
            title=f"{app.title} — API Reference",
            layout=Layout.MODERN,
            dark_mode=True,
        )

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

        # OpenTelemetry SDK bootstrap — must run BEFORE the DB/Redis init
        # below so AsyncPGInstrumentor can patch asyncpg before any pool
        # is created. Idempotent: calling init_tracing twice is a no-op.
        # OTLP endpoint resolved from OTEL_EXPORTER_OTLP_ENDPOINT env;
        # unset → no-op exporter (spans recorded in-process, dropped).
        from ai_gateway_core.tracing import init_tracing
        init_tracing("gateway")

        # Graceful drain — install SIGTERM/SIGINT handlers so the orchestrator's
        # "please stop" signal flips DRAIN. The matching DrainMiddleware was
        # registered above (just after CORSMiddleware); the shutdown handler
        # below awaits ``DRAIN.wait_drained`` before tearing down DB/Redis pools
        # so in-flight chat streams + tool calls get to finish.
        import asyncio as _asyncio_drain
        from ai_gateway_core.proxy.drain import install_signal_handlers
        install_signal_handlers(_asyncio_drain.get_running_loop())

        # 初始化容器（连接数据库、Redis 等）
        await container.initialize()

        # 设置 app.state 属性（向后兼容）
        _setup_app_state(app, container)

        # 从数据库加载服务
        await _load_services_from_database(container, settings)

        # 启动后台任务
        await container.health_monitor.start()
        await container.task_worker.start(settings.task_worker_concurrency)

        # 初始化 Redis 任务队列
        if settings.redis.enabled:
            from .core.tasks.queue import TaskQueue

            # Use native client for TaskQueue as it requires raw commands like brpop/lpush
            task_queue = TaskQueue(container.redis.get_native_client())

            # Phase 5d: ``process_file`` task handler removed. The pre-processing
            # pipeline (FileProcessor) now lives only in assistant-service, and
            # AS's chat path processes uploads on-demand on first reference. Any
            # ``process_file`` task enqueued by an old client would no-op here;
            # the frontend in ``src/api/v1/files.py`` no longer enqueues them.
            await task_queue.start_worker()
            app.state.task_queue = task_queue
            logger.info("Redis 任务队列已启动 (worker active, no handlers registered)")
        else:
            app.state.task_queue = None
            logger.info("Redis 未启用，文件上传仅走同步保存路径")

        # 启动计费拦截器（如果启用）
        if settings.proxy.enabled and settings.proxy.billing_enabled:
            billing_interceptor = container.billing_interceptor
            if billing_interceptor:
                await billing_interceptor.start()
                logger.info("计费拦截器已启动")

        # ========== 初始化存储服务 ==========
        # 存储服务独立于知识库服务，用于文档生成、图片生成、代码执行等功能
        # 即使没有启用知识库，也需要存储服务来保存生成的文件
        image_storage_service = None
        storage_config = None
        try:
            from ai_gateway_core.storage.image_storage import (
                ImageStorageService,
                StorageBackend,
                StorageConfig,
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
                s3_bucket=getattr(getattr(settings, "storage", None), "s3", None)
                and getattr(settings.storage.s3, "bucket", "")
                or "",
                s3_region=getattr(getattr(settings, "storage", None), "s3", None)
                and getattr(settings.storage.s3, "region", "")
                or "us-east-1",
                s3_access_key=getattr(getattr(settings, "storage", None), "s3", None)
                and getattr(settings.storage.s3, "access_key", "")
                or "",
                s3_secret_key=getattr(getattr(settings, "storage", None), "s3", None)
                and getattr(settings.storage.s3, "secret_key", "")
                or "",
                s3_endpoint_url=getattr(getattr(settings, "storage", None), "s3", None)
                and getattr(settings.storage.s3, "endpoint_url", None)
                or None,
                oss_bucket=getattr(getattr(settings, "storage", None), "oss", None)
                and getattr(settings.storage.oss, "bucket", "")
                or "",
                oss_endpoint=getattr(getattr(settings, "storage", None), "oss", None)
                and getattr(settings.storage.oss, "endpoint", "")
                or "",
                oss_access_key=getattr(getattr(settings, "storage", None), "oss", None)
                and getattr(settings.storage.oss, "access_key", "")
                or "",
                oss_secret_key=getattr(getattr(settings, "storage", None), "oss", None)
                and getattr(settings.storage.oss, "secret_key", "")
                or "",
                local_base_path=getattr(getattr(settings, "storage", None), "local_base_path", None)
                or "./data/artifacts",
                url_expiry_seconds=getattr(
                    getattr(settings, "storage", None), "url_expiry_seconds", None
                )
                or 3600,
                key_prefix=getattr(getattr(settings, "storage", None), "key_prefix", None) or "",
            )
            # Get signing key for local file URLs (security)
            signing_key = getattr(getattr(settings, "confluence", None), "encryption_key", "") or ""
            image_storage_service = ImageStorageService(storage_config, signing_key=signing_key)
            app.state.image_storage_service = image_storage_service
            logger.info(
                f"图片存储服务已初始化 (backend={storage_backend.value}, url_signing={'enabled' if signing_key else 'disabled'})"
            )

            # Initialize artifact storage service (for document/image generation, code execution)
            from ai_gateway_core.storage import init_artifact_storage

            artifact_storage = init_artifact_storage(storage_config, container.database)
            app.state.artifact_storage = artifact_storage
            logger.info(f"Artifact 存储服务已初始化 (backend={storage_backend.value})")

            # Initialize file storage service for user uploads
            from ai_gateway_core.storage import init_file_storage

            file_storage = init_file_storage(storage_config)
            app.state.file_storage = file_storage
            logger.info(f"文件存储服务已初始化 (backend={storage_backend.value})")
        except Exception as e:
            logger.warning(f"存储服务初始化失败: {e}")

        # ========== Knowledge Base ==========
        # KB now runs as an independent microservice on :8092. The gateway
        # only holds a thin HTTP client (`KBProxyClient`, initialised below
        # before assistant-service). All chunking / embedding / vector-store
        # / worker / sync logic lives in `apps/knowledge-service/`.
        #
        # The pre-K5b in-process initialisation (KnowledgeService, KnowledgeWorker,
        # VisionPDFProcessor, HierarchicalIndexer, VLMOCRService …) was gated
        # behind `if False:` and was removed in Phase K5b — see
        # plans/kb-fork-merge-report.md for the deletion rationale.
        logger.info(
            "Knowledge Base runs as a microservice (:8092); gateway uses KBProxyClient only."
        )

        # ========== Confluence Integration ==========
        # Phase K5c: Confluence scheduler + sync-service moved to
        # knowledge-service. The gateway no longer polls Confluence, does not
        # embed pages, does not upsert into Qdrant. All of that runs inside
        # apps/knowledge-service (see its lifespan + docker-compose).
        #
        # The REST API surface at src/api/v1/confluence.py is kept so the
        # frontend continues to resolve routes, but every endpoint that
        # depends on ``app.state.confluence_sync_service`` now returns 503
        # until the follow-up converts those routes to proxy to
        # knowledge-service. See plans/k5c-migration-plan.md "Deferred".
        logger.info(
            "Confluence integration runs in knowledge-service (Phase K5c); gateway no longer schedules polling."
        )

        # 启动文件清理服务
        file_cleanup_service = get_cleanup_service()
        await file_cleanup_service.start()
        app.state.file_cleanup_service = file_cleanup_service

        # 启动使用量调度器（聚合任务、配额重置）
        if settings.database.enabled:
            from .services.billing import init_usage_scheduler

            usage_scheduler = init_usage_scheduler(
                container.database,
                retention_days=30,  # 详细记录保留30天
                aggregation_hour=0,
                aggregation_minute=30,
                cleanup_hour=1,
                cleanup_minute=0,
                quota_check_interval=3600,  # 每小时检查配额
            )
            await usage_scheduler.start()
            app.state.usage_scheduler = usage_scheduler
            logger.info("使用量调度器已启动")

            # 启动使用量记录器后台任务
            from .services.metrics import get_usage_recorder

            usage_recorder = get_usage_recorder()
            if usage_recorder:
                await usage_recorder.start()
                app.state.usage_recorder = usage_recorder
                logger.info("使用量记录器已启动")

        # Initialize KB proxy client BEFORE assistant service (microservice mode)
        from ai_gateway_core.knowledge import KBProxyClient
        app.state.kb_proxy = KBProxyClient()

        # 初始化 Assistant Service (GPT-like 体验)
        await _init_assistant_service(app, settings)

        # Initialize Assistant TaskManager lifecycle explicitly
        from ai_gateway_core.tasks import init_task_manager

        app.state.assistant_task_manager = await init_task_manager()

        # 打印启动信息
        _print_startup_info(settings)

    @app.on_event("shutdown")
    async def shutdown():
        """应用关闭"""
        logger.info("正在关闭 AI Gateway...")

        # Wait for in-flight requests (chat streams, tool calls, KB proxy hops)
        # to finish before tearing down container singletons. ``DrainMiddleware``
        # is already 503-ing fresh traffic; this bound stops a hung handler from
        # blocking container exit indefinitely.
        from ai_gateway_core.proxy.drain import DRAIN
        if not await DRAIN.wait_drained(timeout=30.0):
            logger.warning(
                f"drain timeout — {DRAIN.inflight} request(s) still in flight"
            )

        # Phase K5c: KB worker + Confluence scheduler no longer run in the
        # gateway process — they're owned by knowledge-service. Nothing to
        # stop here; the attributes are never set.

        # Stop file cleanup service
        file_cleanup_service = getattr(app.state, "file_cleanup_service", None)
        if file_cleanup_service is not None:
            await file_cleanup_service.stop()

        # Stop usage scheduler
        usage_scheduler = getattr(app.state, "usage_scheduler", None)
        if usage_scheduler is not None:
            await usage_scheduler.stop()

        # Stop billing interceptor BEFORE usage_recorder — it routes records
        # through the recorder, so inflight flushes must drain first.
        # Phase 0 hotfix: without this, fire-and-forget _flush_buffer tasks
        # spawned by StreamProcessor get GC'd mid-write on shutdown.
        billing_interceptor = getattr(app.state, "billing_interceptor", None)
        if billing_interceptor is not None:
            try:
                await billing_interceptor.stop()
                logger.info("billing_interceptor stopped cleanly")
            except Exception:
                logger.exception("billing_interceptor.stop() failed")

        # Stop usage recorder (flush remaining records)
        usage_recorder = getattr(app.state, "usage_recorder", None)
        if usage_recorder is not None:
            await usage_recorder.stop()

        # Stop task queue
        task_queue = getattr(app.state, "task_queue", None)
        if task_queue is not None:
            await task_queue.stop_worker()

        # Stop Assistant Service
        assistant_service = getattr(app.state, "assistant_service", None)
        if assistant_service is not None:
            await assistant_service.close()

        # Islamic Content: now handled by microservice at :8091, no cleanup needed

        # Stop Assistant TaskManager lifecycle
        from ai_gateway_core.tasks import shutdown_task_manager

        await shutdown_task_manager()

        # Close file storage service
        file_storage = getattr(app.state, "file_storage", None)
        if file_storage is not None:
            await file_storage.close()

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
    app.state.memory_service = container.memory_service

    # LangGraph 相关
    app.state.langgraph_proxy = container.langgraph_proxy
    app.state.multi_rate_limiter = container.multi_rate_limiter
    app.state.user_resolver = container.user_resolver

    # 透明代理相关
    app.state.transparent_proxy = container.transparent_proxy
    app.state.proxy_config_loader = container.proxy_config_loader
    app.state.billing_interceptor = container.billing_interceptor
    app.state.context_injector = container.context_injector

    # Knowledge Base (KBMS) — microservice mode
    app.state.knowledge_service = None
    app.state.knowledge_worker = None
    # kb_proxy already initialized before assistant service

    # Confluence 集成
    app.state.confluence_sync_service = None
    app.state.confluence_scheduler = None

    # 游客会话管理器
    from .services.session.guest_session_manager import GuestSessionConfig, GuestSessionManager

    app.state.guest_session_manager = GuestSessionManager(
        config=GuestSessionConfig(),
        redis_client=container.redis,
    )

    # Initialize metrics recorder with Redis for dashboard
    init_metrics_recorder(
        container.redis,
        latency_sample_cap=container.settings.metrics.latency_sample_cap,
    )

    # Initialize realtime metrics service for LangSmith-style dashboard
    init_realtime_metrics(container.redis)

    # Initialize usage recorder for persistent usage metrics
    from .services.metrics import init_usage_recorder

    init_usage_recorder(container.database)

    # Initialize security event recorder for auth/rate limit aggregates
    from .services.metrics import init_security_event_recorder

    init_security_event_recorder(container.database)

    # Initialize billing services
    from .services.billing import init_pricing_service, init_quota_service

    init_quota_service(container.database)
    init_pricing_service(container.database)

    # Initialize LLM provider and model services
    import os

    from .services.llm import ModelService, ProviderService

    encryption_key = os.environ.get("GATEWAY_ENCRYPTION_KEY", "")
    app.state.provider_service = ProviderService(container.database, encryption_key)
    app.state.model_service = ModelService(container.database)


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

        # Auto-sync LangGraph service URLs from environment config
        # This ensures DB services always point to the correct upstream
        # (e.g., localhost:2024 in dev, imam-agent:8000 in Docker)
        if settings.langgraph.enabled and settings.langgraph.instance_urls:
            env_url = settings.langgraph.instance_urls[0]
            updated = 0
            for svc in db_services:
                if svc.get("service_type") == "langgraph" and svc.get("status") == "active":
                    cc = svc.get("connector_config") or {}
                    db_url = cc.get("upstream_url") or cc.get("base_url") or ""
                    if db_url and db_url != env_url:
                        new_cc = dict(cc, base_url=env_url, upstream_url=env_url)
                        await database.execute(
                            "UPDATE services SET connector_config = $1::jsonb WHERE service_id = $2",
                            [__import__("json").dumps(new_cc), svc["service_id"]],
                        )
                        logger.info(
                            f"Auto-synced service '{svc['service_id']}' URL: {db_url} -> {env_url}"
                        )
                        updated += 1
            if updated:
                logger.info(f"Synced {updated} LangGraph service URL(s) to match environment")
    except Exception as e:
        logger.warning(f"从数据库加载服务失败: {e}")


async def _init_assistant_service(app: FastAPI, settings: Settings) -> None:
    """
    初始化 Assistant Service (GPT-like 体验)

    从环境变量读取各 LLM 提供商的 API Key：
    - OPENAI_API_KEY
    - ANTHROPIC_API_KEY
    - DEEPSEEK_API_KEY
    - DASHSCOPE_API_KEY (或复用 knowledge.dashscope.api_key)
    - GOOGLE_API_KEY

    DashScope + Google chat routing (free/paid swap) is handled by
    ``ai_gateway_core.config.endpoints.resolve_dashscope("chat")`` and
    ``resolve_google("chat")`` — same helpers are used for image and
    embedding elsewhere, so chat / image / embedding can be flipped
    between free and paid endpoints independently.

    同时将配置同步到数据库，确保前端可以管理。
    """
    import os

    from ai_gateway_core.config import resolve_dashscope, resolve_google
    from ai_gateway_core.enums import ModelProvider

    # Phase 5e: gateway no longer builds an in-process ``ModelRegistry``.
    # Provider-config sync (env → DB seeding) still runs because the admin
    # UI expects to see the env-derived providers in ``llm_providers``;
    # that's a DB operation and doesn't need the registry.
    configured_providers: list[str] = []

    # 默认 provider 配置定义 — for providers whose endpoint selection is
    # straightforward (single key, single base_url env). DashScope and
    # Google are resolved below via the per-domain helper because they
    # have free/paid swap semantics.
    DEFAULT_PROVIDER_CONFIGS = {
        "openai": {
            "display_name": "OpenAI",
            "api_type": "openai",
            "base_url": "https://api.openai.com",
            "env_key": "OPENAI_API_KEY",
            "env_base_url": "OPENAI_BASE_URL",
        },
        "anthropic": {
            "display_name": "Anthropic",
            "api_type": "anthropic",
            "base_url": "https://api.anthropic.com",
            "env_key": "ANTHROPIC_API_KEY",
            "env_base_url": "ANTHROPIC_BASE_URL",
        },
        "deepseek": {
            "display_name": "DeepSeek",
            "api_type": "openai",
            "base_url": "https://api.deepseek.com",
            "env_key": "DEEPSEEK_API_KEY",
            "env_base_url": "DEEPSEEK_BASE_URL",
        },
        "dashscope": {
            "display_name": "Qwen/DashScope",
            "api_type": "openai",
            # base_url / env_key are resolved dynamically via
            # resolve_dashscope("chat") — the values below are just the
            # defaults the DB row sees if env vars are unset.
            "base_url": "https://dashscope.aliyuncs.com/compatible-mode",
            "env_key": "DASHSCOPE_API_KEY",
            "env_base_url": "DASHSCOPE_CHAT_BASE_URL",
        },
        "google": {
            "display_name": "Google Gemini",
            "api_type": "google",
            "base_url": "https://generativelanguage.googleapis.com",
            "env_key": "GEMINI_API_KEY",
            "env_base_url": None,
        },
        # Vertex AI as a first-class provider: same wire protocol as Google
        # Gemini but different host + key format (Express Mode ``AQ.xxx``).
        # Having its own entry means operators configure it through the
        # Service Management UI the same way they add any other provider;
        # the env-driven ``GOOGLE_API_BACKEND=vertex`` flip is now a
        # deprecated fallback (still works, logs a warning below).
        "google-vertex": {
            "display_name": "Google Vertex AI",
            "api_type": "google-vertex",
            "base_url": "https://aiplatform.googleapis.com",
            "env_key": "VERTEX_API_KEY",
            "env_base_url": None,
        },
    }

    # Deprecation warnings for the old env-driven Vertex switch. Kept
    # functional for one release (see ``_google_backend_for_model`` in
    # model_registry.py) so users with pre-existing deployments don't
    # break, but the preferred path is to add a ``google-vertex``
    # provider in the Service Management UI.
    _legacy_backend = os.environ.get("GOOGLE_API_BACKEND", "").strip().lower()
    _legacy_models = os.environ.get("GOOGLE_VERTEX_MODELS", "").strip()
    if _legacy_backend == "vertex":
        logger.warning(
            "GOOGLE_API_BACKEND=vertex is deprecated. Add a 'google-vertex' "
            "provider in the Service Management UI (or set VERTEX_API_KEY "
            "so the default google-vertex provider is seeded at startup) "
            "and remove GOOGLE_API_BACKEND from your environment."
        )
    if _legacy_models:
        logger.warning(
            "GOOGLE_VERTEX_MODELS is deprecated. Use the 'google-vertex' "
            "provider instead — models configured under that provider route "
            "to Vertex without needing per-model env overrides."
        )

    # 获取 provider_service 用于同步到数据库
    provider_service = getattr(app.state, "provider_service", None)
    tenant_id = "default"

    # 处理每个 provider
    for provider_id, config in DEFAULT_PROVIDER_CONFIGS.items():
        # Default: read the provider's single API key env + optional base_url env.
        api_key = os.environ.get(config["env_key"], "")
        base_url = None
        if config.get("env_base_url"):
            base_url = os.environ.get(config["env_base_url"])
        if not base_url:
            base_url = config["base_url"]
        google_backend = "ai_studio"

        # DashScope chat routing: domain-specific helper handles the CN ⇄
        # Intl swap (DASHSCOPE_CHAT_API_KEY / DASHSCOPE_CHAT_BASE_URL).
        # Legacy ``settings.knowledge.dashscope.api_key`` is still
        # honored as a last-resort fallback for pre-env-var installs.
        if provider_id == "dashscope":
            resolved_key, resolved_url = resolve_dashscope("chat")
            api_key = resolved_key
            base_url = resolved_url
            if not api_key:
                knowledge_dashscope = getattr(
                    getattr(settings, "knowledge", None), "dashscope", None
                )
                if knowledge_dashscope:
                    api_key = getattr(knowledge_dashscope, "api_key", "")

        # Google chat routing: domain-specific helper handles the
        # AI Studio ⇄ Vertex backend flip and picks the matching key
        # (GEMINI_API_KEY vs VERTEX_API_KEY / VERTEX_CHAT_API_KEY).
        # Per-model A/B via GOOGLE_VERTEX_MODELS is still resolved inside
        # ModelRegistry at request time; this only seeds the default.
        if provider_id == "google":
            resolved_key, resolved_url, google_backend = resolve_google("chat")
            api_key = resolved_key
            # Only override base_url when backend actually changed — an
            # explicit DB override (base_url column) would be lost
            # otherwise. AI Studio default matches config["base_url"],
            # so leaving base_url alone is correct in that case.
            if google_backend == "vertex":
                base_url = None  # let configure_provider pick VERTEX_BASE_URL

        # Phase 5e: gateway just records that the provider env is set
        # (used below for env → DB seeding). The in-memory ``configure_provider``
        # dance the old ModelRegistry did is assistant-service's job now.
        if api_key:
            try:
                ModelProvider(provider_id)  # validate it's a known provider
                configured_providers.append(provider_id)
                if provider_id == "google" and google_backend == "vertex":
                    logger.info("Google provider routed to Vertex (env-seeded)")
            except ValueError:
                logger.warning(f"Unknown provider enum: {provider_id}")

        # 同步到数据库（如果 provider_service 可用）
        if provider_service:
            try:
                existing = await provider_service.get_provider(tenant_id, provider_id)
                if not existing:
                    # 创建新 provider
                    await provider_service.create_provider(
                        tenant_id=tenant_id,
                        provider_id=provider_id,
                        display_name=config["display_name"],
                        api_type=config["api_type"],
                        base_url=base_url,
                        api_key=api_key if api_key else None,
                        is_enabled=True,
                    )
                    logger.info(f"Created provider {provider_id} in database")
                elif api_key and not existing.get("has_api_key"):
                    # 更新 API key（如果数据库中没有但环境变量有）
                    await provider_service.update_provider(
                        tenant_id=tenant_id,
                        provider_id=provider_id,
                        api_key=api_key,
                    )
                    logger.info(f"Updated API key for provider {provider_id}")
            except Exception as e:
                logger.warning(f"Failed to sync provider {provider_id} to database: {e}")

    # 从数据库加载 providers（用于加载数据库中用户配置的 providers）
    if provider_service:
        try:
            db_providers = await provider_service.list_providers(tenant_id, include_disabled=False)
            for p in db_providers:
                provider_id = p.get("provider_id", "")
                if provider_id in configured_providers:
                    continue  # 已从环境变量配置
                if not p.get("has_api_key"):
                    continue  # 没有 API key

                # Phase 5e: we no longer configure an in-memory registry
                # here — just track which providers the DB has so
                # ``/health/providers`` can enumerate them.
                try:
                    ModelProvider(provider_id)
                    configured_providers.append(provider_id)
                    logger.info(f"Provider {provider_id} loaded from database")
                except ValueError:
                    logger.debug(f"Custom provider {provider_id} not in enum, skipping")
        except Exception as e:
            logger.warning(f"Failed to load providers from database: {e}")

    # Get KB service if available
    kb_service = getattr(app.state, "knowledge_service", None)

    # Get VLM service from KB service for assistant (if KB is enabled)
    knowledge_vlm_service = getattr(kb_service, "vlm_service", None) if kb_service else None

    # Get session manager for conversation persistence
    session_manager = getattr(app.state, "session_manager", None)

    # Get Tavily API key for web search
    tavily_api_key = os.environ.get("TAVILY_API_KEY", "")

    # Phase 5d: Tavily + code-executor + VLM tool instances moved to
    # assistant-service. Gateway no longer builds them.

    # Phase 5d: Gateway no longer constructs an in-process AssistantService,
    # tool_registry, MCPManager, or tenant isolation services. All of those
    # live in the assistant-service container and are reached via HTTP proxy.
    # The gateway keeps ``app.state.model_registry`` because /chat/stream
    # runs an edge-side model-permission check, /generate-image resolves
    # providers from the registry, and /api/v1/models CRUD refreshes it
    # after admin writes. Tool invocation, MCP enumeration, tenant policy
    # enforcement — all run over the wire against assistant-service.
    _ = session_manager, tavily_api_key, knowledge_vlm_service  # kept for sibling code paths

    # Phase 5e: gateway uses a narrow ``GatewayModelMeta`` facade over
    # ModelService + ProviderService for the 3 routes that still need LLM
    # metadata (chat-stream permission check, /health/providers, model
    # CRUD). The full ModelRegistry lives in assistant-service.
    model_service = getattr(app.state, "model_service", None)
    provider_service = getattr(app.state, "provider_service", None)
    if model_service and provider_service:
        from .services.llm.gateway_model_meta import GatewayModelMeta
        app.state.model_meta = GatewayModelMeta(model_service, provider_service)
    else:
        app.state.model_meta = None
        logger.warning(
            "GatewayModelMeta not initialised — /chat/stream permission check "
            "will be bypassed and /health/providers will return {}"
        )

    # ``None`` placeholders so legacy getattr readers in /services and
    # /health see a consistent shape. Real implementations all live in
    # assistant-service now (Phase 5d + 5e).
    app.state.model_registry = None
    app.state.assistant_service = None
    app.state.assistant_gateway = None
    app.state.tool_registry = None
    app.state.assistant_client = None
    app.state.mcp_manager = None
    app.state.tenant_tool_policy = None
    app.state.tenant_mcp_config = None
    app.state.tool_audit = None

    # Sync model pricing from DB (no in-memory registry refresh required —
    # assistant-service refreshes its own registry on demand).
    if model_service:
        try:
            synced = await model_service.sync_pricing_from_llm_models(
                tenant_id="default",
                include_disabled=True,
            )
            logger.info(f"Synchronized {synced} model pricing records from llm_models")
        except Exception as e:
            logger.warning(f"Failed to sync llm_models pricing to model_pricing: {e}")

    features = []
    if configured_providers:
        features.append(f"providers: {', '.join(configured_providers)}")
    if session_manager:
        features.append("session persistence")
    if kb_service:
        features.append("KB tools")
    features.append("proxy → assistant-service (chat / tools / MCP)")

    if configured_providers:
        logger.info(f"Gateway启动完成: model_registry ready ({', '.join(features)})")
    else:
        logger.warning("Gateway 启动，但没有配置任何 LLM 提供商 API Key")

    # Startup diagnostics — log which provider keys are present so ops can
    # tell at a glance whether the gateway can proxy-sign KB calls + which
    # providers model_registry can route to.
    dashscope_key_present = bool(os.environ.get("DASHSCOPE_API_KEY"))
    if not dashscope_key_present:
        knowledge_dashscope = getattr(getattr(settings, "knowledge", None), "dashscope", None)
        dashscope_key_present = (
            bool(getattr(knowledge_dashscope, "api_key", "")) if knowledge_dashscope else False
        )

    google_key_present = bool(os.environ.get("GEMINI_API_KEY") or os.environ.get("GOOGLE_API_KEY"))

    logger.info(
        "[Gateway Startup] role=proxy keys={google:%s,dashscope:%s}",
        google_key_present,
        dashscope_key_present,
    )


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
