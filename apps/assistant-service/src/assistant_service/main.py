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


def _env_truthy(name: str, *, default: bool = False) -> bool:
    raw = os.getenv(name)
    if raw is None:
        return default
    return raw.strip().lower() in {"1", "true", "yes", "on"}


@asynccontextmanager
async def lifespan(app: FastAPI):
    """Startup / shutdown lifecycle."""
    logger.info("Assistant Service starting...")
    app.state.settings = settings

    # Mandatory init failures (DB, Redis when REQUIRE_REDIS is set) raise
    # from lifespan so the container crashes instead of starting in a half-broken
    # state where /health returns 200 but every chat request 500s. Optional
    # integrations (Confluence, Tavily, etc.) further down still warn-and-continue.
    require_db = _env_truthy("ASSISTANT_REQUIRE_DB", default=True)
    require_redis = _env_truthy("ASSISTANT_REQUIRE_REDIS", default=False)

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
        if require_db:
            logger.error("Database init failed (ASSISTANT_REQUIRE_DB=true): %s", e)
            raise RuntimeError(
                f"Database is mandatory but failed to initialize: {e}. "
                "Either fix DATABASE_URL/connectivity or set ASSISTANT_REQUIRE_DB=false "
                "(dev only — production must not run without DB)."
            ) from e
        logger.warning(f"Database init failed (running without DB): {e}")

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
            if require_redis:
                logger.error("Redis init failed (ASSISTANT_REQUIRE_REDIS=true): %s", e)
                raise RuntimeError(
                    f"Redis is mandatory but failed: {e}. "
                    "Async image tasks fall back to in-process dict without Redis, "
                    "which breaks across container restarts and multi-replica deploys."
                ) from e
            logger.warning(f"Redis init failed (running without Redis): {e}")

    # ── Model Registry ──
    from ai_gateway_core.config import resolve_dashscope, resolve_google

    from .core.models.model_registry import ModelProvider, ModelRegistry
    model_registry = ModelRegistry()

    # Provider config must mirror gateway's (src/main.py). When the gateway
    # chat-stream route proxies to assistant-service, the request specifies a
    # ``model_id`` that ModelRegistry must resolve against a CONFIGURED provider
    # — a model routed to ``google-vertex`` or ``dashscope`` with the chat-key
    # override fails here if the provider isn't configured. Previously the
    # gateway ran assistant code in-process so its registry was used; now that
    # assistant-service is the real execution target, its registry has to know
    # every provider the gateway knows.
    #
    # DashScope + Google chat keys are resolved via the per-domain helper
    # (ai_gateway_core.config.endpoints) so chat / image / embedding can be
    # flipped between free and paid endpoints independently. See
    # packages/ai-gateway-core/src/ai_gateway_core/config/endpoints.py.
    dash_chat_key, dash_chat_url = resolve_dashscope("chat")
    google_chat_key, google_chat_url, google_backend = resolve_google("chat")
    # Vertex uses the shared vertex key fallback chain (the
    # google-vertex provider is the "use Vertex all the time" entry;
    # the legacy GOOGLE_API_BACKEND switch is honored via resolve_google).
    vertex_key = os.environ.get("VERTEX_API_KEY", "").strip() or (
        google_chat_key if google_backend == "vertex" else ""
    )

    providers_config = {
        "openai": (
            os.environ.get("OPENAI_API_KEY", ""),
            os.environ.get("OPENAI_BASE_URL") or "https://api.openai.com",
        ),
        "anthropic": (
            os.environ.get("ANTHROPIC_API_KEY", ""),
            os.environ.get("ANTHROPIC_BASE_URL") or "https://api.anthropic.com",
        ),
        "deepseek": (
            os.environ.get("DEEPSEEK_API_KEY", ""),
            os.environ.get("DEEPSEEK_BASE_URL") or "https://api.deepseek.com",
        ),
        "dashscope": (dash_chat_key, dash_chat_url),
        "google": (google_chat_key, google_chat_url),
        "google-vertex": (
            vertex_key,
            os.environ.get("VERTEX_BASE_URL") or "https://aiplatform.googleapis.com",
        ),
    }

    for pid, (api_key, base_url) in providers_config.items():
        if not api_key:
            continue

        try:
            kwargs = dict(api_key=api_key, base_url=base_url)
            if pid == "google":
                kwargs["backend"] = google_backend
            model_registry.configure_provider(ModelProvider(pid), **kwargs)
            logger.info(f"Provider {pid} configured")
        except ValueError:
            logger.warning(f"Provider {pid} unknown in ModelProvider enum — skipping")
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
        TavilySearchTool,
        register_builtin_tools,
        register_document_generation_tool,
        register_pptx_generation_tool,
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
    # Bucket-B wiring (Phase 4.2): fetch concrete recorders from the gateway's
    # src/ and inject into AssistantService. ``main.py`` is the only allowed
    # composition-root site for these src.* imports; anywhere else in
    # apps/assistant-service/ uses the Protocols from ai-gateway-core.
    # Storage stack itself moved to ai_gateway_core in Phase 5f Batch B, so
    # storage helpers no longer require a src.* import here.
    from ai_gateway_core.storage import (
        get_artifact_storage,
        get_file_storage,
        init_artifact_storage,
    )

    from src.services.metrics.realtime_metrics import get_realtime_metrics
    from src.services.metrics.usage_recorder import get_usage_recorder

    from .core import AssistantService

    realtime_metrics = get_realtime_metrics()
    usage_recorder = get_usage_recorder()

    # Artifact storage init — the ai_gateway_core singleton is per-process.
    # Gateway initializes it during its own startup; AS runs in a separate
    # container and must initialize its own instance from the GATEWAY_STORAGE__*
    # env vars (prod hands AS the same bucket so artifacts are visible to both
    # services).
    if get_artifact_storage() is None:
        try:
            from ai_gateway_core.storage.image_storage import StorageConfig

            storage_config = StorageConfig.from_env()
            init_artifact_storage(storage_config, database)
            logger.info(f"Artifact storage initialized (backend={storage_config.backend.value})")
        except Exception as e:
            logger.warning(f"Artifact storage init failed: {e}")

    artifact_storage = get_artifact_storage()
    try:
        file_storage = get_file_storage()
    except RuntimeError as e:
        logger.warning(f"File storage not configurable — falling back to NoOp: {e}")
        from ai_gateway_core.storage import NoOpFileStorage
        file_storage = NoOpFileStorage()

    assistant_service = AssistantService(
        model_registry=model_registry,
        kb_service=None,
        kb_proxy=kb_proxy,
        tavily_api_key=tavily_key or None,
        session_manager=session_manager,
        redis_client=redis_client,
        memory_service=memory_service,
        db=database,
        usage_recorder=usage_recorder,
        realtime_metrics=realtime_metrics,
        artifact_storage=artifact_storage,
        file_storage=file_storage,
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

# X-Request-Id middleware — bind incoming gateway request_id to request.state
# + REQUEST_ID_CTX contextvar so log lines can include it. When invoked
# directly (no gateway in front) mints `svc-<uuid>` so log aggregators can
# tell direct calls from gateway-fronted ones.
from ai_gateway_core.proxy import RequestIDMiddleware  # noqa: E402

app.add_middleware(RequestIDMiddleware)

# Phase 5a: reject traffic without a valid ``X-Gateway-Secret``. Closes
# Audit Finding H-4 (sibling-container SSRF → user impersonation). Skipped
# entirely when the secret env var is unset (local dev); in prod compose
# the env MUST be set and ``ASSISTANT_APP__ALLOW_ANONYMOUS`` MUST be
# ``false`` for the middleware to actively reject.
_gateway_secret_env = os.environ.get("GATEWAY_ASSISTANT_SHARED_SECRET", "").strip()
if _gateway_secret_env:
    from ai_gateway_core.auth.gateway_secret import GatewaySecret

    from .auth import GatewaySecretAuthMiddleware

    app.add_middleware(
        GatewaySecretAuthMiddleware,
        gateway_secret=GatewaySecret(secret=_gateway_secret_env),
        allow_anonymous=settings.app.allow_anonymous,
    )
    logger.info(
        "Gateway-secret middleware active (allow_anonymous=%s)",
        settings.app.allow_anonymous,
    )
elif not settings.app.allow_anonymous:
    # Fail hard. ``get_user_context`` trusts ``X-User-Id``/``X-Tenant-Id``
    # headers verbatim when they are present, so "no middleware + no
    # anonymous" does NOT mean "everything is rejected" — it means
    # "anyone on the docker bridge network can forge identity headers
    # and land a full impersonation". The original H-4 audit finding
    # is exactly this scenario. Refuse to start in that configuration.
    raise RuntimeError(
        "GATEWAY_ASSISTANT_SHARED_SECRET is unset AND "
        "ASSISTANT_APP__ALLOW_ANONYMOUS=false. This combination is a "
        "security hole: get_user_context trusts X-User-* headers with "
        "no HMAC check, so any sibling container can impersonate any "
        "user. Either set the secret (production) or enable "
        "allow_anonymous=true (dev only)."
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
