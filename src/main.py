"""
AI Service Gateway 应用入口

使用依赖注入容器和中间件架构重构后的应用入口。
"""

from __future__ import annotations

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
from .core.observability.tracing import TracingMiddleware
from .core.observability.metrics import get_metrics
from .adapters.registry import auto_register_builtin_adapters
from .core.auth.anonymous_middleware import AnonymousIdentityMiddleware
from .core.middleware.auth import AuthMiddleware, AuthConfig
from .core.middleware.rate_limit_http import RateLimitMiddleware, RateLimitConfig
from .core.middleware.request_logging import RequestLoggingMiddleware, RequestLogConfig

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

    # Stable anonymous identity for guest users (cookie/header)
    app.add_middleware(AnonymousIdentityMiddleware, settings=settings)

    # 统一鉴权中间件（支持 JWT、API Key、游客会话）
    auth_config = AuthConfig(
        jwt_enabled=settings.authentication.jwt.enabled if hasattr(settings, 'authentication') else False,
        jwt_secret=settings.authentication.jwt.secret if hasattr(settings, 'authentication') else "",
        jwt_algorithms=settings.authentication.jwt.algorithms if hasattr(settings, 'authentication') else ["HS256"],
        api_key_enabled=settings.authentication.api_key.enabled if hasattr(settings, 'authentication') else False,
        guest_session_enabled=True,
        anonymous_enabled=True,
        whitelist_paths=["/health", "/health/live", "/health/ready", "/metrics", "/docs", "/openapi.json"],
    )
    app.add_middleware(AuthMiddleware, config=auth_config)

    # HTTP 级别限流中间件
    rate_limit_config = RateLimitConfig(
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
    app.add_middleware(RateLimitMiddleware, config=rate_limit_config, redis_client=None)

    # 请求日志中间件
    request_log_config = RequestLogConfig(
        enabled=True,
        log_request_body=False,
        log_response_body=False,
        exclude_paths=["/health", "/health/live", "/health/ready", "/metrics"],
    )
    app.add_middleware(RequestLoggingMiddleware, config=request_log_config)

    # CORS 中间件
    app.add_middleware(
        CORSMiddleware,
        allow_origins=["*"],
        allow_credentials=True,
        allow_methods=["*"],
        allow_headers=["*"],
    )

    # 追踪中间件
    app.add_middleware(
        TracingMiddleware,
        service_name="gateway",
        log_requests=True,
        log_responses=True,
        exclude_paths=["/health", "/health/live", "/health/ready", "/metrics"],
    )
    
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

        # 启动 Knowledge Base (KBMS) 后台任务
        if getattr(settings, "knowledge", None) and settings.knowledge.enabled:
            from .services.knowledge.knowledge_service import KnowledgeService
            from .services.knowledge.worker import KnowledgeWorker

            app.state.knowledge_service = KnowledgeService(
                settings=settings, database=container.database
            )
            app.state.knowledge_worker = KnowledgeWorker(app.state.knowledge_service)
            await app.state.knowledge_worker.start(settings.knowledge.worker_concurrency)
        
        # 打印启动信息
        _print_startup_info(settings)
    
    @app.on_event("shutdown")
    async def shutdown():
        """应用关闭"""
        logger.info("正在关闭 AI Gateway...")
        # Stop KBMS worker/service first (if enabled)
        kb_worker = getattr(app.state, "knowledge_worker", None)
        if kb_worker is not None:
            await kb_worker.stop()
        kb_service = getattr(app.state, "knowledge_service", None)
        if kb_service is not None:
            await kb_service.close()
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

    # Knowledge Base (KBMS)
    app.state.knowledge_service = None
    app.state.knowledge_worker = None

    # 游客会话管理器
    from .services.session.guest_session_manager import GuestSessionManager, GuestSessionConfig
    app.state.guest_session_manager = GuestSessionManager(
        config=GuestSessionConfig(),
        redis_client=container.redis,
    )


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
