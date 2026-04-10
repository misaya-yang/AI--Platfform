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
from .core.exceptions import (
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
from .core.observability.logging import configure_structured_logging, get_logger
from .core.observability.metrics import get_metrics
from .services.metrics.metrics_recorder import init_metrics_recorder
from .services.metrics.realtime_metrics import init_realtime_metrics

logger = get_logger(__name__)

OPENAPI_TAGS = [
    {
        "name": "Islamic Content",
        "description": "Quran / Hadith / Dua / Prayer / Qibla 对外内容接口，含 canonical 数据演示接口。",
    },
    {"name": "Health", "description": "服务健康检查与就绪状态。"},
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
        description="统一 AI 服务网关，支持多协议适配、限流、熔断、会话管理等功能",
        openapi_tags=OPENAPI_TAGS,
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
    try:
        from scalar_fastapi import Layout, get_scalar_api_reference

        @app.get("/scalar", include_in_schema=False)
        async def scalar_html():
            return get_scalar_api_reference(
                openapi_url=app.openapi_url,
                title=f"{app.title} — API Reference",
                layout=Layout.MODERN,
                dark_mode=True,
                hide_download_button=False,
                search_hot_key="k",
            )
    except ImportError:
        pass  # scalar-fastapi optional

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

        # 初始化 Redis 任务队列
        if settings.redis.enabled:
            from .core.tasks.queue import TaskQueue

            # Use native client for TaskQueue as it requires raw commands like brpop/lpush
            task_queue = TaskQueue(container.redis.get_native_client())

            # 注册任务处理器
            task_queue.register_handler(
                "process_file",
                _make_process_file_handler(app),
            )

            await task_queue.start_worker()
            app.state.task_queue = task_queue
            logger.info("Redis 任务队列已启动 (worker active)")
        else:
            app.state.task_queue = None
            logger.warning("Redis 未启用，异步文件预处理功能不可用")

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
            from .services.storage.image_storage import (
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
            from .services.storage import init_artifact_storage

            artifact_storage = init_artifact_storage(storage_config, container.database)
            app.state.artifact_storage = artifact_storage
            logger.info(f"Artifact 存储服务已初始化 (backend={storage_backend.value})")

            # Initialize file storage service for user uploads
            from .services.storage import init_file_storage

            file_storage = init_file_storage(storage_config)
            app.state.file_storage = file_storage
            logger.info(f"文件存储服务已初始化 (backend={storage_backend.value})")
        except Exception as e:
            logger.warning(f"存储服务初始化失败: {e}")

        # ========== Knowledge Base — now runs as independent microservice (:8092) ==========
        # KB Service handles: chunking, embedding, vector store, worker, Confluence sync.
        # Gateway proxies /api/v1/knowledge/* to KB Service transparently.
        # See: apps/knowledge-service/ and docker-compose.yml
        if False and getattr(settings, "knowledge", None) and settings.knowledge.enabled:
            from .services.knowledge.knowledge_service import KnowledgeService
            from .services.knowledge.worker import KnowledgeWorker

            # 初始化多模态嵌入服务（如果配置了 DashScope API Key）
            multimodal_embedding = None

            dashscope_key = getattr(getattr(settings, "knowledge", None), "dashscope", None)
            if dashscope_key and dashscope_key.api_key:
                try:
                    # Use UnifiedMultimodalEmbedding for consistent text-image vector space
                    # This ensures cross-modal retrieval works correctly
                    from .services.knowledge.embedding import UnifiedMultimodalEmbedding

                    multimodal_embedding = UnifiedMultimodalEmbedding(
                        model="tongyi-embedding-vision-plus",  # Best for unified cross-modal search
                        api_key=dashscope_key.api_key,
                    )
                    logger.info(
                        "多模态嵌入服务已初始化 (UnifiedMultimodalEmbedding tongyi-embedding-vision-plus)"
                    )
                except Exception as e:
                    logger.warning(f"多模态嵌入服务初始化失败: {e}")

            # Initialize VLM service for image description generation in knowledge service
            knowledge_vlm_service = None
            dashscope_config = getattr(settings.knowledge, "dashscope", None)
            if (
                dashscope_config
                and dashscope_config.api_key
                and multimodal_embedding
                and image_storage_service
            ):
                try:
                    from .services.knowledge.vlm_service import DashScopeVLMService

                    knowledge_vlm_service = DashScopeVLMService(
                        api_key=dashscope_config.api_key,
                        model="qwen-vl-max",
                    )
                    logger.info("Knowledge VLM 服务已初始化 (qwen-vl-max)")
                except Exception as e:
                    logger.warning(f"Knowledge VLM 服务初始化失败: {e}")

            app.state.knowledge_service = KnowledgeService(
                settings=settings,
                database=container.database,
                multimodal_embedding=multimodal_embedding,
                image_storage_service=image_storage_service,
                vlm_service=knowledge_vlm_service,
            )

            # Initialize VisionPDFProcessor for scanned document processing
            vision_processor = None
            if multimodal_embedding:
                try:
                    from .services.knowledge.vision_pdf_processor import VisionPDFProcessor

                    vision_processor = VisionPDFProcessor(
                        embedder=multimodal_embedding,
                        vector_store=app.state.knowledge_service.vector_store,
                        database=container.database,
                        position_offset=int(
                            getattr(settings.knowledge, "image_position_offset", 1_000_000)
                        ),
                    )
                    logger.info("VisionPDFProcessor initialized for scanned document support")
                except Exception as e:
                    logger.warning(f"Failed to initialize VisionPDFProcessor: {e}")

            # Initialize Document Type Detector for auto processing mode
            detector = None
            try:
                from .services.knowledge.document_detector import DocumentTypeDetector

                detector = DocumentTypeDetector(settings.knowledge)
                logger.info("DocumentTypeDetector initialized for auto processing mode")
            except Exception as e:
                logger.warning(f"Failed to initialize DocumentTypeDetector: {e}")

            # Initialize Hierarchical Indexer for large document processing
            hierarchical_indexer = None
            summary_generator = None
            try:
                from .services.knowledge.hierarchical_indexer import HierarchicalIndexer
                from .services.knowledge.summary_generator import SummaryGenerator

                summary_generator = SummaryGenerator(llm_service=container.llm_service)
                hierarchical_indexer = HierarchicalIndexer(
                    vector_store=app.state.knowledge_service.vector_store,
                    database=container.database,
                    embedder=app.state.knowledge_service.embedder,
                    summary_generator=summary_generator,
                    knowledge_settings=settings.knowledge,
                )
                logger.info("HierarchicalIndexer initialized for large document processing")
            except Exception as e:
                logger.warning(f"Failed to initialize HierarchicalIndexer: {e}")

            # Initialize VLM OCR service for high-accuracy scanned document text extraction
            # Supports Gemini (default, uses GEMINI_API_KEY) and DashScope backends
            import os
            vlm_ocr_service = None
            ocr_vlm_provider = getattr(settings.knowledge, "ocr_vlm_provider", "gemini")
            ocr_vlm_model = getattr(settings.knowledge, "ocr_vlm_model", "gemini-2.5-flash")

            # Pick the right API key based on provider
            ocr_api_key = None
            if ocr_vlm_provider == "gemini" or (
                ocr_vlm_provider == "auto" and ocr_vlm_model.startswith("gemini-")
            ):
                ocr_api_key = (
                    os.environ.get("GEMINI_API_KEY")
                    or os.environ.get("GOOGLE_API_KEY")
                    or getattr(getattr(settings.knowledge, "gemini", None), "api_key", "")
                )
            elif ocr_vlm_provider == "dashscope" or (
                ocr_vlm_provider == "auto" and not ocr_vlm_model.startswith("gemini-")
            ):
                ocr_api_key = getattr(dashscope_config, "api_key", "") if dashscope_config else ""

            if ocr_api_key:
                try:
                    from .services.knowledge.vlm_ocr_service import VLMOCRService

                    vlm_ocr_service = VLMOCRService(
                        api_key=ocr_api_key,
                        model=ocr_vlm_model,
                        provider=ocr_vlm_provider,
                        concurrency=getattr(settings.knowledge, "ocr_vlm_concurrency", 4),
                        timeout_seconds=getattr(settings.knowledge, "ocr_vlm_timeout_seconds", 30),
                    )
                    logger.info(
                        f"VLMOCRService initialized: provider={vlm_ocr_service.provider}, "
                        f"model={ocr_vlm_model}"
                    )
                except Exception as e:
                    logger.warning(f"Failed to initialize VLMOCRService: {e}")
            else:
                logger.info("VLMOCRService not initialized: no API key for OCR VLM provider")

            app.state.knowledge_worker = KnowledgeWorker(
                app.state.knowledge_service,
                vision_processor=vision_processor,
                detector=detector,
                hierarchical_indexer=hierarchical_indexer,
                vlm_ocr_service=vlm_ocr_service,
            )
            # In multi-worker deployments, only start the knowledge ingestion
            # worker in the first process to avoid duplicating embedding work
            # and to keep other worker processes free for API requests.
            import os as _os
            _is_main_worker = _os.environ.get("GATEWAY_KNOWLEDGE_WORKER_ENABLED", "true").lower() in ("true", "1", "yes")
            if _is_main_worker:
                await app.state.knowledge_worker.start(settings.knowledge.worker_concurrency)
            else:
                logger.info("Knowledge worker disabled in this process (GATEWAY_KNOWLEDGE_WORKER_ENABLED=false)")

            # 恢复服务重启前未完成的任务（仅在 knowledge worker 启动的进程中执行）
            if _is_main_worker:
                try:
                    recovery_result = await app.state.knowledge_service.recover_stuck_documents(
                        stuck_threshold_minutes=0, worker=app.state.knowledge_worker
                    )
                    if recovery_result["recovered_count"] > 0:
                        logger.info(
                            f"恢复了 {recovery_result['recovered_count']} 个未完成文档, "
                            f"重新入队 {recovery_result['requeued_count']} 个"
                        )
                except Exception as e:
                    logger.warning(f"恢复未完成文档失败: {e}")

            if multimodal_embedding:
                vlm_status = "with VLM" if knowledge_vlm_service else "no VLM"
                logger.info(
                    f"知识库服务已启动 (支持图片嵌入, {vlm_status}, worker_concurrency={settings.knowledge.worker_concurrency})"
                )
            else:
                logger.info(
                    f"知识库服务已启动 (仅文本, worker_concurrency={settings.knowledge.worker_concurrency})"
                )
        else:
            logger.warning(
                f"知识库服务未启动: knowledge={getattr(settings, 'knowledge', None)}, "
                f"enabled={getattr(getattr(settings, 'knowledge', None), 'enabled', None)}"
            )

        # 启动 Confluence 集成服务（如果启用）
        if getattr(settings, "confluence", None) and settings.confluence.enabled:
            from .services.knowledge.confluence.scheduler import ConfluenceScheduler
            from .services.knowledge.confluence.sync_service import ConfluenceSyncService

            # 复用 Knowledge Service 的图片处理服务和 VLM 服务
            confluence_image_storage = getattr(app.state, "image_storage_service", None)
            confluence_vlm_service = None

            # 如果 Knowledge Service 已初始化，复用其服务
            if hasattr(app.state, "knowledge_service") and app.state.knowledge_service:
                getattr(app.state.knowledge_service, "multimodal_embedding", None)
                confluence_image_storage = (
                    getattr(app.state.knowledge_service, "image_storage_service", None)
                    or confluence_image_storage
                )
                # 复用 VLM 服务以避免重复初始化
                confluence_vlm_service = getattr(app.state.knowledge_service, "vlm_service", None)
                if confluence_vlm_service:
                    logger.info("Confluence 复用 Knowledge Service 的 VLM 服务")

            # 如果没有从 Knowledge Service 获取到 VLM 服务，则单独初始化
            if not confluence_vlm_service and confluence_image_storage:
                dashscope_key = getattr(getattr(settings, "knowledge", None), "dashscope", None)
                if dashscope_key and dashscope_key.api_key:
                    try:
                        from .services.knowledge.vlm_service import DashScopeVLMService

                        confluence_vlm_service = DashScopeVLMService(
                            api_key=dashscope_key.api_key,
                            model="qwen-vl-max",
                        )
                        logger.info("Confluence VLM 服务已初始化 (qwen-vl-max)")
                    except Exception as e:
                        logger.warning(f"Confluence VLM 服务初始化失败: {e}")

            # Text-First RAG: No multimodal embedding needed
            # Embeddings are generated by sync_service using dataset's configured provider (Gemini)
            app.state.confluence_sync_service = ConfluenceSyncService(
                settings=settings,
                database=container.database,
                knowledge_service=app.state.knowledge_service,
                knowledge_worker=app.state.knowledge_worker,
                image_storage_service=confluence_image_storage,
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
        from .services.knowledge.kb_proxy_client import KBProxyClient
        app.state.kb_proxy = KBProxyClient()

        # 初始化 Assistant Service (GPT-like 体验)
        await _init_assistant_service(app, settings)

        # Initialize Assistant TaskManager lifecycle explicitly
        from .services.assistant.tasks.task_manager import init_task_manager

        app.state.assistant_task_manager = await init_task_manager()

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

        # Stop usage scheduler
        usage_scheduler = getattr(app.state, "usage_scheduler", None)
        if usage_scheduler is not None:
            await usage_scheduler.stop()

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
        from .services.assistant.tasks.task_manager import shutdown_task_manager

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


def _make_process_file_handler(app: FastAPI, process_file_task=None):
    """Create a task handler that resolves assistant_service from app.state at runtime."""
    if process_file_task is None:
        from .services.assistant.tasks.task_types import process_file_task as _process_file_task

        process_file_task = _process_file_task

    async def _process_file_wrapper(payload):
        assistant_service = getattr(app.state, "assistant_service", None)
        file_processor = getattr(assistant_service, "file_processor", None)
        if not file_processor:
            logger.error("Assistant service not initialized; skipping process_file task")
            return
        await process_file_task(payload, file_processor)

    return _process_file_wrapper


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
    init_metrics_recorder(container.redis)

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

    同时将配置同步到数据库，确保前端可以管理。
    """
    import os

    from .services.assistant import AssistantService, ModelProvider, ModelRegistry

    model_registry = ModelRegistry()
    configured_providers = []

    # 默认 provider 配置定义
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
            "base_url": "https://dashscope.aliyuncs.com/compatible-mode",
            "env_key": "DASHSCOPE_API_KEY",
            "env_base_url": None,
        },
        "google": {
            "display_name": "Google Gemini",
            "api_type": "google",
            "base_url": "https://generativelanguage.googleapis.com",
            # Prefer GEMINI_API_KEY, fallback to GOOGLE_API_KEY for backward compatibility.
            "env_key": "GEMINI_API_KEY",
            "env_base_url": None,
        },
    }

    # 获取 provider_service 用于同步到数据库
    provider_service = getattr(app.state, "provider_service", None)
    tenant_id = "default"

    # 处理每个 provider
    for provider_id, config in DEFAULT_PROVIDER_CONFIGS.items():
        # 从环境变量获取 API key
        api_key = os.environ.get(config["env_key"], "")

        # DashScope 特殊处理：fallback 到 knowledge.dashscope.api_key
        if provider_id == "dashscope" and not api_key:
            knowledge_dashscope = getattr(getattr(settings, "knowledge", None), "dashscope", None)
            if knowledge_dashscope:
                api_key = getattr(knowledge_dashscope, "api_key", "")

        # Google Gemini: backward-compatible fallback to GOOGLE_API_KEY
        if provider_id == "google" and not api_key:
            api_key = os.environ.get("GOOGLE_API_KEY", "")

        # 获取自定义 base_url
        base_url = None
        if config.get("env_base_url"):
            base_url = os.environ.get(config["env_base_url"])
        if not base_url:
            base_url = config["base_url"]

        # 配置 ModelRegistry（内存中）
        if api_key:
            try:
                model_registry.configure_provider(
                    ModelProvider(provider_id),
                    api_key=api_key,
                    base_url=base_url,
                )
                configured_providers.append(provider_id)
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

                # 从数据库加载 API key 并配置 ModelRegistry
                api_key = await provider_service._get_api_key(tenant_id, provider_id)
                if api_key:
                    try:
                        provider_enum = ModelProvider(provider_id)
                        model_registry.configure_provider(
                            provider_enum,
                            api_key=api_key,
                            base_url=p.get("base_url"),
                        )
                        configured_providers.append(provider_id)
                        logger.info(f"Loaded provider {provider_id} from database")
                    except ValueError:
                        # 自定义 provider，目前不支持
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

    # Create Tavily tool instance
    from .services.assistant.tools import TavilySearchTool

    tavily_tool = TavilySearchTool(api_key=tavily_api_key or None)

    # Initialize code executor if Docker is available
    from .services.assistant.code_executor import CodeExecutionConfig, CodeExecutorService

    code_executor = None
    try:
        code_executor = CodeExecutorService(
            config=CodeExecutionConfig(
                image="ai-gateway-code-interpreter:latest",
            )
        )
        if code_executor.is_docker_available():
            logger.info("Code executor initialized with Docker support")
        else:
            code_executor = None
            logger.warning("Docker not available, code execution disabled")
    except Exception as e:
        logger.warning(f"Failed to initialize code executor: {e}")

    # Create assistant service with session persistence
    assistant_vlm_service = knowledge_vlm_service

    # If no VLM service but DashScope is configured for assistant, try to initialize it
    if not assistant_vlm_service:
        dashscope_config = model_registry._configs.get(ModelProvider.DASHSCOPE)
        if dashscope_config and dashscope_config.api_key:
            try:
                from .services.knowledge.vlm_service import DashScopeVLMService

                assistant_vlm_service = DashScopeVLMService(
                    api_key=dashscope_config.api_key,
                    model="qwen-vl-max",
                )
                logger.info("Assistant VLM 服务已独立初始化 (qwen-vl-max)")
            except Exception as e:
                logger.warning(f"Assistant VLM 服务初始化失败: {e}")

    # Get memory service for long-term memory
    memory_service = getattr(app.state, "memory_service", None)

    assistant_service = AssistantService(
        model_registry=model_registry,
        kb_service=kb_service,
        kb_proxy=getattr(app.state, "kb_proxy", None),
        tavily_api_key=tavily_api_key or None,
        session_manager=session_manager,
        code_executor=code_executor,
        vlm_service=assistant_vlm_service,
        redis_client=app.state.redis,
        memory_service=memory_service,
        db=app.state.database,
    )

    # Initialize Tool Registry (Phase 2)
    from .services.assistant.tools import (
        get_tool_registry,
        register_builtin_tools,
        register_code_executor_tool,
        register_document_generation_tool,
        register_pptx_generation_tool,
    )
    from .services.assistant.tools.image_generator_tool import register_image_generation_tool

    tool_registry = get_tool_registry()
    effective_kb_service = kb_service or getattr(app.state, "kb_proxy", None)
    register_builtin_tools(
        kb_service=effective_kb_service, tavily_tool=tavily_tool, memory_service=memory_service,
        database=getattr(app.state, "database", None),
    )

    # Register code executor tool if available
    if code_executor:
        register_code_executor_tool(code_executor=code_executor)

    # Register image generation tool if at least one provider is configured (Gemini preferred, DashScope fallback)
    image_gen_registered = register_image_generation_tool()

    # Register document generation tool (always available)
    doc_gen_registered = register_document_generation_tool()

    # Register PowerPoint generation tool (requires python-pptx)
    register_pptx_generation_tool()

    # Register quiz generation tool (KB → LLM → interactive quiz)
    from .services.assistant.tools.quiz_tool import register_quiz_tool
    register_quiz_tool(
        kb_service=kb_service,
        model_registry=model_registry,
        database=getattr(app.state, "database", None),
        kb_proxy=getattr(app.state, "kb_proxy", None),
    )

    # Register sub-agent tool (ADR-003)
    from .services.assistant.tools.subagent_tool import register_subagent_tool
    register_subagent_tool()
    logger.info("Registered spawn_subagent tool")

    # Store in app.state
    app.state.model_registry = model_registry
    app.state.assistant_service = assistant_service
    app.state.assistant_gateway = assistant_service.execution_gateway
    app.state.tool_registry = tool_registry

    # Create assistant client (Protocol-based abstraction for future microservice extraction)
    from .services.assistant.client import create_assistant_client
    assistant_mode = os.environ.get("ASSISTANT_MODE", "in_process")
    assistant_remote_url = os.environ.get("ASSISTANT_SERVICE_URL", "http://assistant-service:8093")
    app.state.assistant_client = create_assistant_client(
        mode=assistant_mode,
        service=assistant_service if assistant_mode == "in_process" else None,
        remote_url=assistant_remote_url,
    )

    # Initialize MCP (Model Context Protocol) connections
    try:
        from .services.assistant.mcp import MCPManager, load_mcp_config
        mcp_configs = load_mcp_config()
        if mcp_configs:
            mcp_manager = MCPManager(configs=mcp_configs)
            results = await mcp_manager.initialize_all()
            app.state.mcp_manager = mcp_manager
            total_tools = sum(v for v in results.values() if v > 0)
            logger.info(f"MCP: {len(results)} servers, {total_tools} tools registered")
        else:
            app.state.mcp_manager = None
    except Exception as e:
        logger.warning(f"MCP initialization failed: {e}")
        app.state.mcp_manager = None

    # ADR-002 Phase 1: Initialize tenant isolation services
    database = getattr(app.state, "database", None)
    try:
        from .services.assistant.tools.tenant_tool_policy import TenantToolPolicyService
        from .services.assistant.mcp.tenant_mcp_config import TenantMCPConfigService
        from .services.assistant.audit import ToolAuditService

        app.state.tenant_tool_policy = TenantToolPolicyService(database=database)
        app.state.tool_audit = ToolAuditService(database=database)

        mcp_server_names = []
        mcp_mgr = getattr(app.state, "mcp_manager", None)
        if mcp_mgr:
            mcp_server_names = mcp_mgr.server_names
        app.state.tenant_mcp_config = TenantMCPConfigService(
            database=database,
            all_server_names=mcp_server_names,
        )
        # Inject into AssistantService (created before MCP init)
        assistant_service.tenant_tool_policy = app.state.tenant_tool_policy
        assistant_service.tenant_mcp_config = app.state.tenant_mcp_config
        assistant_service.tool_audit = app.state.tool_audit
        # Rebuild execution_gateway tool_invoker with tenant services
        if hasattr(assistant_service, "execution_gateway") and assistant_service.execution_gateway:
            from .services.assistant.tool_invoker import create_tool_invoker
            assistant_service.execution_gateway.tool_invoker = create_tool_invoker(
                tenant_tool_policy=app.state.tenant_tool_policy,
                tenant_mcp_config=app.state.tenant_mcp_config,
                tool_audit=app.state.tool_audit,
            )
        logger.info("ADR-002: Tenant isolation services initialized")
    except Exception as e:
        logger.warning(f"Tenant isolation init failed (non-fatal): {e}")
        app.state.tenant_tool_policy = None
        app.state.tenant_mcp_config = None
        app.state.tool_audit = None

    # Load models from database (if available)
    model_service = getattr(app.state, "model_service", None)
    if model_service:
        loaded = await model_registry.load_models_from_database(model_service, tenant_id="default")
        if loaded > 0:
            logger.info(f"Loaded {loaded} models from database into registry")
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
    if tavily_api_key:
        features.append("web search")
    if kb_service:
        features.append("KB tools")
    if code_executor:
        features.append("code execution")
    if image_gen_registered:
        features.append("image generation")
    if doc_gen_registered:
        features.append("document generation")

    registered_tools = len(tool_registry.list_tools())
    if registered_tools > 0:
        features.append(f"{registered_tools} tools")

    if features:
        logger.info(f"Assistant Service 已初始化 ({', '.join(features)})")
    else:
        logger.warning("Assistant Service 已初始化，但没有配置任何 LLM 提供商 API Key")

    # One-time startup diagnostics for Assistant (engine/tools/keys) to aid debugging.
    try:
        tool_names = [t.name for t in tool_registry.list_tools()]
    except Exception:
        tool_names = []

    dashscope_key_present = bool(os.environ.get("DASHSCOPE_API_KEY"))
    if not dashscope_key_present:
        knowledge_dashscope = getattr(getattr(settings, "knowledge", None), "dashscope", None)
        dashscope_key_present = (
            bool(getattr(knowledge_dashscope, "api_key", "")) if knowledge_dashscope else False
        )

    google_key_present = bool(os.environ.get("GEMINI_API_KEY") or os.environ.get("GOOGLE_API_KEY"))

    logger.info(
        "[Assistant Startup] default_engine=agent_loop streaming_first_mode=%s tools=%s keys={google:%s,dashscope:%s,tavily:%s}",
        True,
        tool_names,
        google_key_present,
        dashscope_key_present,
        bool(tavily_api_key),
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
