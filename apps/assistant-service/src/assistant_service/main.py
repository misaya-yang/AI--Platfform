"""
Assistant Service — independent FastAPI microservice.

Runs on port 8093. Provides AI chat, streaming, tools, RAG, memory, agents.
Trusts gateway-forwarded X-User-* headers for authentication.

Start: uvicorn assistant_service.main:app --host 0.0.0.0 --port 8093
"""

from __future__ import annotations

import logging
import os
from contextlib import asynccontextmanager

from fastapi import FastAPI
from fastapi.middleware.cors import CORSMiddleware

from .config import get_settings

logger = logging.getLogger("assistant-service")
logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s %(levelname)s %(name)s — %(message)s",
)

settings = get_settings()


@asynccontextmanager
async def lifespan(app: FastAPI):
    """Startup / shutdown lifecycle."""
    logger.info("Assistant Service starting...")
    app.state.settings = settings

    # ── Database ──
    database = None
    db_dsn = os.getenv("DATABASE_URL", settings.database.dsn)
    try:
        from src.persistence.database import DatabaseStorage
        database = DatabaseStorage(db_dsn, enabled=True, auto_init=False)
        await database.connect()
        app.state.database = database
        logger.info("Database connected")
    except Exception as e:
        logger.warning(f"Database init failed: {e}")

    # ── Redis ──
    redis_client = None
    redis_url = os.getenv("REDIS_URL", settings.redis.url)
    if redis_url and settings.redis.enabled:
        try:
            import redis.asyncio as aioredis
            redis_client = aioredis.from_url(redis_url, decode_responses=True)
            await redis_client.ping()
            app.state.redis = redis_client
            logger.info("Redis connected")
        except Exception as e:
            logger.warning(f"Redis init failed: {e}")

    # ── Model Registry ──
    from .core.models.model_registry import ModelRegistry, ModelProvider
    model_registry = ModelRegistry()

    providers_config = {
        "openai": ("OPENAI_API_KEY", "OPENAI_BASE_URL", "https://api.openai.com"),
        "anthropic": ("ANTHROPIC_API_KEY", "ANTHROPIC_BASE_URL", "https://api.anthropic.com"),
        "deepseek": ("DEEPSEEK_API_KEY", "DEEPSEEK_BASE_URL", "https://api.deepseek.com"),
        "dashscope": ("DASHSCOPE_API_KEY", None, "https://dashscope.aliyuncs.com/compatible-mode"),
        "google": ("GEMINI_API_KEY", None, "https://generativelanguage.googleapis.com"),
    }
    for pid, (env_key, env_url, default_url) in providers_config.items():
        api_key = os.environ.get(env_key, "")
        if pid == "google" and not api_key:
            api_key = os.environ.get("GOOGLE_API_KEY", "")
        if api_key:
            try:
                base_url = os.environ.get(env_url) if env_url else None
                model_registry.configure_provider(
                    ModelProvider(pid), api_key=api_key, base_url=base_url or default_url,
                )
                logger.info(f"Provider {pid} configured")
            except Exception as e:
                logger.warning(f"Provider {pid} failed: {e}")

    # Load models from database
    if database and getattr(database, "_pool", None):
        try:
            from .core.models.model_registry import ModelInfo
            async with database._pool.acquire() as conn:
                rows = await conn.fetch(
                    "SELECT model_id, display_name, provider_id, context_window, max_output_tokens "
                    "FROM llm_models WHERE tenant_id = $1 AND is_enabled = true", "default",
                )
                if rows:
                    model_registry._models.clear()
                    for r in rows:
                        try:
                            model_registry._models[r["model_id"]] = ModelInfo(
                                id=r["model_id"], provider=ModelProvider(r["provider_id"]),
                                name=r["display_name"] or r["model_id"],
                                context_window=r["context_window"] or 32000,
                                max_output_tokens=r["max_output_tokens"] or 4096,
                            )
                        except ValueError:
                            pass
                    logger.info(f"Loaded {len(rows)} models from DB")
        except Exception as e:
            logger.warning(f"DB model load failed: {e}")

    app.state.model_registry = model_registry

    # ── KB Proxy ──
    kb_proxy = None
    kb_url = os.getenv("KB_SERVICE_URL", settings.kb.url)
    try:
        from src.services.knowledge.kb_proxy_client import KBProxyClient
        kb_proxy = KBProxyClient(base_url=kb_url)
        logger.info(f"KB proxy → {kb_url}")
    except Exception as e:
        logger.warning(f"KB proxy init failed: {e}")

    # ── Memory Service ──
    memory_service = None
    if database:
        try:
            from .core.memory_service import MemoryService
            memory_service = MemoryService(database)
        except Exception as e:
            logger.warning(f"Memory service init failed: {e}")

    # ── Session Manager ──
    session_manager = None
    if database:
        try:
            from src.services.session.database_session_manager import DatabaseSessionManager
            session_manager = DatabaseSessionManager(database)
        except Exception as e:
            logger.warning(f"Session manager init failed: {e}")

    # ── Tool Registry ──
    from .core.tools import (
        TavilySearchTool, get_tool_registry, register_builtin_tools,
        register_document_generation_tool, register_pptx_generation_tool,
    )
    from .core.tools.image_generator_tool import register_image_generation_tool

    tavily_key = os.environ.get("TAVILY_API_KEY", "")
    tavily_tool = TavilySearchTool(api_key=tavily_key or None)

    register_builtin_tools(
        kb_service=kb_proxy, tavily_tool=tavily_tool,
        memory_service=memory_service, database=database,
    )
    register_document_generation_tool()
    try:
        register_pptx_generation_tool()
    except Exception:
        pass
    register_image_generation_tool()

    # ── Todo tools (Phase 5) — always on; exposes the per-session
    # WorkingMemory to the model for long-horizon task tracking.
    from .core.tools.todo_tools import register_todo_tools
    register_todo_tools()

    # ── Context management tool — always on; lets the model (or a user
    # /compact slash command) explicitly request history compression.
    from .core.tools.context_tools import register_context_tools
    register_context_tools()

    # ── Primitive tools (Phase 4) — env-gated opt-in ──
    # Exposes fs_read/fs_write/fs_glob/fs_grep to the model. Requires a writable
    # workspace root (default /tmp/ai-gateway-workspace, override with
    # ASSISTANT_WORKSPACE_ROOT). Off by default to keep legacy deployments
    # unchanged.
    if os.environ.get("ASSISTANT_ENABLE_PRIMITIVES", "").lower() in {"1", "true", "yes"}:
        from .core.tools.primitives import register_primitive_tools
        register_primitive_tools()
        logger.info("Primitive tools enabled (fs_read/fs_write/fs_glob/fs_grep)")

    # ── AssistantService ──
    from .core import AssistantService
    assistant_service = AssistantService(
        model_registry=model_registry,
        kb_service=None,
        kb_proxy=kb_proxy,
        tavily_api_key=tavily_key or None,
        session_manager=session_manager,
        redis_client=redis_client,
        memory_service=memory_service,
        db=database,
    )
    app.state.assistant_service = assistant_service
    app.state.session_manager = session_manager
    app.state.kb_proxy = kb_proxy
    app.state.memory_service = memory_service

    # ── Register DB-backed Confluence tools ──
    # Register ONCE with a database reference. The executors resolve
    # per-call credentials from `confluence_connections` using the
    # request's tenant_id. This avoids the old cross-tenant leak where
    # looping registrations left whichever tenant ran last in control
    # of the single process-global executor.
    #
    # We still do a startup sanity count so logs flag an unexpected
    # empty state loudly (separate from working auto-registration).
    if database:
        try:
            from .core.tools.confluence_tool import register_confluence_tools

            register_confluence_tools(database=database)
            # Sanity count with a single retry — the DB pool may still be
            # warming up when startup fires. If both attempts return 0,
            # we WARN loudly so ops can tell "nobody has connected yet"
            # apart from "the DB is broken".
            import asyncio as _asyncio
            count = -1
            for attempt in (1, 2):
                try:
                    rows = await database.list_confluence_connections(
                        status="active", limit=500
                    )
                    count = len(rows)
                    if count > 0 or attempt == 2:
                        break
                    await _asyncio.sleep(1.0)
                except Exception:
                    logger.exception(
                        "Confluence startup sanity query failed (attempt %d)",
                        attempt,
                    )
                    if attempt == 2:
                        break
                    await _asyncio.sleep(1.0)

            if count == 0:
                logger.warning(
                    "⚠️  Confluence tools registered (DB-backed), but "
                    "0 active connections in `confluence_connections` after "
                    "retry. Tool calls will reject until a tenant connects "
                    "via the Integrations panel. If you expected active "
                    "connections, check DB connectivity and the "
                    "`confluence_connections.status='active'` filter."
                )
            elif count > 0:
                logger.info(
                    "Confluence tools registered (DB-backed) — %d active tenant connection(s)",
                    count,
                )
        except Exception:
            logger.exception(
                "Confluence tool registration failed — all Confluence calls will error"
            )

    app.state._ready = True
    logger.info("Assistant Service ready ✓")

    yield  # ── Running ──

    # ── Shutdown ──
    if database:
        await database.close()
    if redis_client:
        await redis_client.close()
    logger.info("Assistant Service shut down")


# ── Create App ──

app = FastAPI(
    title="AI Assistant Service",
    version="0.1.0",
    lifespan=lifespan,
)

_origins = settings.cors.allow_origins
_credentials = "*" not in _origins
if not _credentials:
    logger.warning("CORS wildcard origin detected — credentials disabled. "
                   "Set ASSISTANT_CORS__ALLOW_ORIGINS to explicit origins.")
app.add_middleware(
    CORSMiddleware,
    allow_origins=_origins,
    allow_credentials=_credentials,
    allow_methods=["*"],
    allow_headers=["*"],
)


@app.get("/health")
async def health():
    ready = getattr(app.state, "_ready", False)
    return {"status": "ok" if ready else "starting", "service": "assistant", "version": "0.1.0"}


@app.middleware("http")
async def security_headers(request, call_next):
    response = await call_next(request)
    response.headers["X-Content-Type-Options"] = "nosniff"
    response.headers["X-Frame-Options"] = "DENY"
    response.headers["Strict-Transport-Security"] = "max-age=31536000; includeSubDomains"
    return response

# ── Register API routes ──
from .api.router import router as api_router
app.include_router(api_router, prefix="/api/v1/assistant")
