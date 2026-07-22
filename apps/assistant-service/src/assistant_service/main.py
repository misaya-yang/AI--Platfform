"""
Assistant Service — independent FastAPI microservice.

Runs on port 8093. Provides AI chat, streaming, tools, RAG, memory, agents.
Trusts gateway-forwarded X-User-* headers for authentication.

Start: uvicorn assistant_service.main:app --host 0.0.0.0 --port 8093
"""

from __future__ import annotations

import asyncio
import logging
import os
from contextlib import asynccontextmanager, suppress

from ai_gateway_core.logging import configure_structured_logging
from fastapi import FastAPI
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import JSONResponse

from .config import get_settings

# PR-3: structured logging via the shared bridge so every record carries
# request_id (from REQUEST_ID_CTX), trace_id/span_id (when an OTel span is
# active), and the ``service`` tag. Prod (ENVIRONMENT=production) →
# single-line JSON; dev → human-readable "simple". LOG_FORMAT env wins.
_log_format = os.environ.get("LOG_FORMAT")
if not _log_format:
    _log_format = "json" if os.environ.get("ENVIRONMENT", "").lower() == "production" else "simple"
configure_structured_logging(
    level="INFO",
    format_type=_log_format,
    service="assistant-service",
    log_to_file=False,
)
logger = logging.getLogger("assistant-service")

settings = get_settings()


def _env_truthy(name: str, *, default: bool = False) -> bool:
    raw = os.getenv(name)
    if raw is None:
        return default
    return raw.strip().lower() in {"1", "true", "yes", "on"}


def _register_subagent_tool_if_enabled() -> bool:
    """Register delegation only when the rollout flag explicitly enables it."""

    if not _env_truthy("ASSISTANT_SUBAGENTS_ENABLED"):
        return False
    from .core.tools.subagent_tool import register_subagent_tool

    register_subagent_tool()
    return True


def _configure_agent_runtime_resource_policies(app: FastAPI, database):
    """Wire the real DB-backed policy services used by Agent runtime mapping."""

    app.state.agent_runtime_resource_policy = None
    if database is None:
        return None

    from .core.tools.tenant_tool_policy import (
        AgentRuntimeResourcePolicyService,
        TenantToolPolicyService,
    )

    tenant_tool_policy = TenantToolPolicyService(database)
    app.state.agent_runtime_resource_policy = AgentRuntimeResourcePolicyService(database)
    return tenant_tool_policy


@asynccontextmanager
async def lifespan(app: FastAPI):
    """Startup / shutdown lifecycle."""
    logger.info("Assistant Service starting...")
    app.state.settings = settings

    # ── OpenTelemetry SDK bootstrap — must run BEFORE database init below
    # so AsyncPGInstrumentor patches asyncpg before any pool is created.
    # Idempotent: a duplicate call is a debug-log no-op. Endpoint comes
    # from OTEL_EXPORTER_OTLP_ENDPOINT env; unset → in-process spans only.
    from ai_gateway_core.tracing import init_tracing

    init_tracing("assistant-service")

    # ── Graceful drain — install SIGTERM/SIGINT handlers so the orchestrator's
    # "please stop" signal flips ``DRAIN`` and the shutdown path below can wait
    # for in-flight requests to finish. The middleware that consumes ``DRAIN``
    # is registered after the FastAPI() instance is built (see below — placed
    # BEFORE CORSMiddleware so the 503 short-circuit fires first).
    from ai_gateway_core.proxy.drain import DRAIN, install_signal_handlers

    install_signal_handlers(asyncio.get_running_loop())

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
        from ai_gateway_core.persistence import DatabaseStorage

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

    tenant_tool_policy = _configure_agent_runtime_resource_policies(app, database)

    # ── Redis ──
    redis_client = None
    redis_storage = None  # RedisStorage wrapper for shared session cache
    redis_url = os.getenv("REDIS_URL", settings.redis.url)
    if redis_url and settings.redis.enabled:
        try:
            import redis.asyncio as aioredis

            redis_client = aioredis.from_url(redis_url, decode_responses=True)
            await redis_client.ping()
            app.state.redis = redis_client
            logger.info("Redis connected")

            # Build a RedisStorage wrapper that DatabaseSessionManager
            # uses for cache. Shared with the gateway via the same Redis
            # instance — mandatory for cache-coherence across processes.
            # Without this, AS writes invalidate only its own in-process
            # dict, leaving the gateway's Redis cache stale (incident
            # 2026-04-28: chat sessions appearing empty after AS reload).
            from ai_gateway_core.persistence import RedisStorage

            redis_storage = RedisStorage(url=redis_url, enabled=True)
            await redis_storage.connect()
            app.state.redis_storage = redis_storage
            logger.info("RedisStorage wrapper connected (shared session cache)")
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
            kwargs = {"api_key": api_key, "base_url": base_url}
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
                    "FROM llm_models WHERE tenant_id = $1 AND is_enabled = true "
                    "ORDER BY sort_order ASC, model_id ASC",
                    "default",
                )
                if rows:
                    model_registry._models.clear()
                    for r in rows:
                        with suppress(ValueError):
                            model_registry._models[r["model_id"]] = ModelInfo(
                                id=r["model_id"],
                                provider=ModelProvider(r["provider_id"]),
                                name=r["display_name"] or r["model_id"],
                                context_window=r["context_window"] or 32000,
                                max_output_tokens=r["max_output_tokens"] or 4096,
                            )
                    logger.info(f"Loaded {len(rows)} models from DB")
        except Exception as e:
            logger.warning(f"DB model load failed: {e}")

    app.state.model_registry = model_registry

    # ── Tenant MCP registry/runtime ──
    mcp_runtime = None
    mcp_repository = None
    mcp_secret_resolver = None
    mcp_enabled = os.getenv("AGENT_STUDIO_MCP_ENABLED", "true").strip().lower() in {
        "1",
        "true",
        "yes",
        "on",
    }
    app.state.agent_studio_mcp_enabled = mcp_enabled
    if database and mcp_enabled:
        try:
            from ai_gateway_core.persistence.repositories.mcp_repository import (
                DatabaseMCPRepository,
            )

            from .core.mcp.runtime import (
                ConfiguredEnvironmentSecretResolver,
                MCPDiscoveryService,
                MCPRuntimeService,
            )

            mcp_repository = DatabaseMCPRepository(database)
            mcp_secret_resolver = ConfiguredEnvironmentSecretResolver.from_env()
            mcp_runtime = MCPRuntimeService(
                repository=mcp_repository,
                secret_resolver=mcp_secret_resolver,
            )
            app.state.mcp_repository = mcp_repository
            app.state.mcp_runtime = mcp_runtime
            app.state.mcp_discovery_service = MCPDiscoveryService(
                secret_resolver=mcp_secret_resolver,
            )
            logger.info("Tenant MCP registry/runtime initialized")
        except Exception as e:
            # MCP is optional for the built-in Assistant. Agent-bound MCP will
            # remain absent and fail closed until this adapter is available.
            logger.warning("Tenant MCP runtime init failed: %s", e)
    elif not mcp_enabled:
        logger.info("Tenant MCP registry/runtime disabled by feature flag")

    # ── KB Proxy ──
    kb_proxy = None
    kb_url = os.getenv("KB_SERVICE_URL", settings.kb.url)
    try:
        from ai_gateway_core.knowledge import KBProxyClient

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
            from ai_gateway_core.session import DatabaseSessionManager

            session_manager = DatabaseSessionManager(database, redis=redis_storage)
        except Exception as e:
            logger.warning(f"Session manager init failed: {e}")

    # ── Tool Registry ──
    # `search_web` (Tavily) was deleted in PR-2 — capable models use their own
    # native search (Qwen `enable_search`, Anthropic `web_search_20250305`),
    # `web_fetch` is the URL-fetch fallback for everything else.
    from .core.tools import (
        register_builtin_tools,
        register_document_generation_tool,
        register_pptx_generation_tool,
    )
    from .core.tools.image_generator_tool import register_image_generation_tool

    register_builtin_tools(
        kb_service=kb_proxy,
        memory_service=memory_service,
        database=database,
    )
    register_document_generation_tool()
    with suppress(Exception):
        register_pptx_generation_tool()
    register_image_generation_tool()

    # ── Todo tools (Phase 5) — always on; exposes the per-session
    # WorkingMemory to the model for long-horizon task tracking.
    from .core.tools.todo_tools import register_todo_tools

    register_todo_tools()

    # ── Context management tool — always on; lets the model (or a user
    # /compact slash command) explicitly request history compression.
    from .core.tools.context_tools import register_context_tools

    register_context_tools()

    if _register_subagent_tool_if_enabled():
        logger.info("Sub-agent delegation enabled")

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
    from ai_gateway_core.metrics import get_realtime_metrics, get_usage_recorder
    from ai_gateway_core.storage import (
        get_artifact_storage,
        get_file_storage,
        init_artifact_storage,
        init_file_storage,
    )

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
            init_file_storage(storage_config)
            logger.info(f"Artifact storage initialized (backend={storage_config.backend.value})")
        except Exception as e:
            logger.warning(f"Artifact storage init failed: {e}")

    artifact_storage = get_artifact_storage()
    try:
        file_storage = get_file_storage()
    except RuntimeError as e:
        try:
            from ai_gateway_core.storage.image_storage import StorageConfig

            file_storage = init_file_storage(StorageConfig.from_env())
            logger.info(f"File storage initialized (backend={file_storage.config.backend.value})")
        except Exception:
            logger.warning(f"File storage not configurable — falling back to NoOp: {e}")
            from ai_gateway_core.storage import NoOpFileStorage

            file_storage = NoOpFileStorage()

    assistant_service = AssistantService(
        model_registry=model_registry,
        kb_service=None,
        kb_proxy=kb_proxy,
        session_manager=session_manager,
        redis_client=redis_client,
        memory_service=memory_service,
        db=database,
        usage_recorder=usage_recorder,
        realtime_metrics=realtime_metrics,
        artifact_storage=artifact_storage,
        file_storage=file_storage,
        mcp_runtime=mcp_runtime,
        tenant_tool_policy=tenant_tool_policy,
    )
    app.state.assistant_service = assistant_service
    app.state.session_manager = session_manager
    app.state.kb_proxy = kb_proxy
    app.state.memory_service = memory_service
    try:
        from .core.runtime.memory.governance_cleanup import (
            AgentRuntimeMemoryCleanupService,
        )

        app.state.runtime_memory_cleanup_service = AgentRuntimeMemoryCleanupService.from_env(
            database=database
        )
    except Exception as exc:
        app.state.runtime_memory_cleanup_service = None
        logger.warning(
            "Agent runtime-memory cleanup service initialization failed (%s)",
            type(exc).__name__,
        )

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

            register_confluence_tools(
                database=database,
                credential_repository=mcp_repository,
                secret_resolver=mcp_secret_resolver,
            )
            # Sanity count with a single retry — the DB pool may still be
            # warming up when startup fires. If both attempts return 0,
            # we WARN loudly so ops can tell "nobody has connected yet"
            # apart from "the DB is broken".
            import asyncio as _asyncio

            count = -1
            for attempt in (1, 2):
                try:
                    rows = await database.list_confluence_connections(status="active", limit=500)
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
    # Wait for in-flight requests to finish before closing pools. New requests
    # were already rejected with 503 by ``DrainMiddleware`` once SIGTERM
    # flipped ``DRAIN``. Bound the wait so a hung handler doesn't block
    # container exit forever.
    if not await DRAIN.wait_drained(timeout=30.0):
        logger.warning(
            "drain timeout — %d request(s) still in flight at shutdown",
            DRAIN.inflight,
        )

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
    logger.warning(
        "CORS wildcard origin detected — credentials disabled. "
        "Set ASSISTANT_CORS__ALLOW_ORIGINS to explicit origins."
    )
app.add_middleware(
    CORSMiddleware,
    allow_origins=_origins,
    allow_credentials=_credentials,
    allow_methods=["*"],
    allow_headers=["*"],
)

# Graceful drain — when the orchestrator sends SIGTERM, ``install_signal_handlers``
# (called inside ``lifespan`` startup) flips a process-singleton flag. After
# that, this middleware short-circuits non-probe requests with 503 + Retry-After
# while the lifespan shutdown phase awaits in-flight requests. Probe paths
# (``/health*``, ``/metrics``) still answer so the LB sees the readiness flip
# and stops routing fresh traffic.
#
# Cross-link: ``RequestIDMiddleware`` immediately below — same install pattern
# (single ``app.add_middleware`` line, no constructor args needed since the
# default ``DRAIN`` singleton is what we want here).
#
# Stacking note: Starlette runs the LAST-added middleware FIRST, so adding
# DrainMiddleware here (after CORS) makes it outermost — drain gating fires
# before CORS preflight handling. Move it earlier in source order to invert.
# OTel inbound middleware sits BETWEEN CORS and Drain in source order so
# its execution wraps Drain → RequestID (it sees request.state.request_id
# set by RequestIDMiddleware) but is wrapped by CORS preflight handling.
from ai_gateway_core.tracing import OTelInboundMiddleware  # noqa: E402

app.add_middleware(OTelInboundMiddleware)

from ai_gateway_core.proxy import DrainMiddleware  # noqa: E402

app.add_middleware(DrainMiddleware)

# X-Request-Id middleware — bind incoming gateway request_id to request.state
# + REQUEST_ID_CTX contextvar so log lines can include it. When invoked
# directly (no gateway in front) mints `svc-<uuid>` so log aggregators can
# tell direct calls from gateway-fronted ones.
from ai_gateway_core.proxy import RequestIDMiddleware  # noqa: E402

app.add_middleware(RequestIDMiddleware)

from ai_gateway_core.comm import (  # noqa: E402
    IdempotencyMiddleware,
    InMemoryIdempotencyStore,
    RedisIdempotencyStore,
)


def _idempotency_store_from_env():
    backend = os.environ.get("INTERNAL_IDEMPOTENCY_BACKEND", "memory").strip().lower()
    if backend == "redis":
        redis_url = os.environ.get("INTERNAL_COMM_REDIS_URL") or os.environ.get("REDIS_URL", "")
        if redis_url:
            import redis.asyncio as aioredis

            return RedisIdempotencyStore(aioredis.from_url(redis_url, decode_responses=False))
    return InMemoryIdempotencyStore()


app.add_middleware(
    IdempotencyMiddleware,
    store=_idempotency_store_from_env(),
    ttl_seconds=int(os.environ.get("INTERNAL_IDEMPOTENCY_TTL_SECONDS", "86400")),
)

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


@app.get("/health/live")
async def health_live():
    return {"status": "alive", "service": "assistant"}


@app.get("/health/ready")
async def health_ready():
    from ai_gateway_core.proxy.drain import DRAIN

    ready = bool(getattr(app.state, "_ready", False)) and not DRAIN.draining
    checks = {
        "startup": "ready" if getattr(app.state, "_ready", False) else "starting",
        "drain": "draining" if DRAIN.draining else "accepting",
        "database": (
            "healthy" if getattr(app.state, "database", None) is not None else "not_configured"
        ),
        "kb_proxy": (
            "configured" if getattr(app.state, "kb_proxy", None) is not None else "not_configured"
        ),
    }
    return JSONResponse(
        status_code=200 if ready else 503,
        content={
            "status": "ready" if ready else "not_ready",
            "service": "assistant",
            "checks": checks,
        },
    )


@app.middleware("http")
async def security_headers(request, call_next):
    response = await call_next(request)
    response.headers["X-Content-Type-Options"] = "nosniff"
    response.headers["X-Frame-Options"] = "DENY"
    response.headers["Strict-Transport-Security"] = "max-age=31536000; includeSubDomains"
    return response


# ── Register API routes ──
from .api.router import router as api_router  # noqa: E402

app.include_router(api_router, prefix="/api/v1/assistant")
