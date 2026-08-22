"""
Assistant Service — independent FastAPI microservice.

Runs on port 8093. Provides AI chat, streaming, tools, RAG, memory, agents.
Trusts gateway-forwarded X-User-* headers for authentication.

Start: uvicorn assistant_service.main:app --host 0.0.0.0 --port 8093
"""

from __future__ import annotations

import asyncio
import json
import logging
import os
from collections.abc import Iterable
from contextlib import asynccontextmanager
from typing import Any

from ai_gateway_core.logging import configure_structured_logging, log_internal_exception
from fastapi import FastAPI
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import JSONResponse

from .config.startup_fingerprint import (
    StartupConfigSnapshot,
    resolve_startup_config,
)

# Resolve once before logging/Pydantic/auth/idempotency validation can fail.
# Runtime callsites consume this same snapshot; safe_summary is the only form
# permitted in logs, readiness, and trace metadata.
_STARTUP_CONFIG = resolve_startup_config()
_STARTUP_CONFIG_SUMMARY = _STARTUP_CONFIG.safe_summary()

# PR-3: structured logging via the shared bridge so every record carries
# request_id (from REQUEST_ID_CTX), trace_id/span_id (when an OTel span is
# active), and the ``service`` tag. Prod (ENVIRONMENT=production) →
# single-line JSON; dev → human-readable "simple". LOG_FORMAT env wins.
_log_format = str(_STARTUP_CONFIG.runtime_value("LOG_FORMAT"))
configure_structured_logging(
    level="INFO",
    format_type=_log_format,
    service="assistant-service",
    log_to_file=False,
)
logger = logging.getLogger("assistant-service")

logger.info(
    "Assistant startup config resolved fingerprint=%s config=%s",
    _STARTUP_CONFIG.sha256,
    json.dumps(_STARTUP_CONFIG_SUMMARY, sort_keys=True, separators=(",", ":")),
)

def _env_truthy(name: str, *, default: bool = False) -> bool:
    raw = os.getenv(name)
    if raw is None:
        return default
    return raw.strip().lower() in {"1", "true", "yes", "on"}


def _resolved_runtime_feature_contract() -> dict[str, Any]:
    """Compatibility projection of the single immutable startup snapshot."""

    features = {
        "gateway": _STARTUP_CONFIG.bool_value("ASSISTANT_GATEWAY_ENABLED"),
        "runtime_context_v2": _STARTUP_CONFIG.bool_value("ASSISTANT_RUNTIME_CONTEXT_V2"),
        "runtime_memory_v2": _STARTUP_CONFIG.bool_value("ASSISTANT_RUNTIME_MEMORY_V2"),
        "runtime_skills": _STARTUP_CONFIG.bool_value("ASSISTANT_RUNTIME_SKILLS"),
        "staged_compaction": _STARTUP_CONFIG.bool_value(
            "ASSISTANT_STAGED_COMPACTION_ENABLED"
        ),
        "subagents": _STARTUP_CONFIG.bool_value("ASSISTANT_SUBAGENTS_ENABLED"),
        "tool_output_spill": _STARTUP_CONFIG.bool_value(
            "ASSISTANT_TOOL_OUTPUT_SPILL_ENABLED"
        ),
    }
    return {
        "schema_version": "assistant-runtime-features/v1",
        "features": features,
        "sha256": _STARTUP_CONFIG.sha256.removeprefix("sha256:"),
    }


def _storage_config_from_snapshot(startup_config: StartupConfigSnapshot):
    """Build storage config from the same frozen values used by the fingerprint."""

    from ai_gateway_core.storage.image_storage import StorageBackend, StorageConfig

    return StorageConfig(
        backend=StorageBackend(str(startup_config.runtime_value("GATEWAY_STORAGE__BACKEND"))),
        s3_bucket=str(startup_config.runtime_value("GATEWAY_STORAGE__S3__BUCKET")),
        s3_region=str(startup_config.runtime_value("GATEWAY_STORAGE__S3__REGION")),
        s3_access_key=startup_config.secret_value("GATEWAY_STORAGE__S3__ACCESS_KEY"),
        s3_secret_key=startup_config.secret_value("GATEWAY_STORAGE__S3__SECRET_KEY"),
        s3_endpoint_url=(
            str(startup_config.runtime_value("GATEWAY_STORAGE__S3__ENDPOINT_URL")) or None
        ),
        oss_bucket=str(startup_config.runtime_value("GATEWAY_STORAGE__OSS__BUCKET")),
        oss_endpoint=str(startup_config.runtime_value("GATEWAY_STORAGE__OSS__ENDPOINT")),
        oss_access_key=startup_config.secret_value("GATEWAY_STORAGE__OSS__ACCESS_KEY"),
        oss_secret_key=startup_config.secret_value("GATEWAY_STORAGE__OSS__SECRET_KEY"),
        local_base_path=str(
            startup_config.runtime_value("GATEWAY_STORAGE__LOCAL_BASE_PATH")
        ),
        url_expiry_seconds=int(
            startup_config.runtime_value("GATEWAY_STORAGE__URL_EXPIRY_SECONDS")
        ),
        key_prefix=str(startup_config.runtime_value("GATEWAY_STORAGE__KEY_PREFIX")),
    )


def _code_execution_config_from_snapshot(startup_config: StartupConfigSnapshot):
    """Build the sandbox boundary from the attested startup snapshot."""

    from .core.code_executor import CodeExecutionConfig

    sandbox_runtime = startup_config.runtime_value("SANDBOX_RUNTIME")
    return CodeExecutionConfig(
        sandbox_backend=str(
            startup_config.runtime_value("ASSISTANT_CODE_EXECUTOR_BACKEND")
        ),
        sandbox_runtime=str(sandbox_runtime) if sandbox_runtime is not None else None,
        allow_default_runtime_fallback=(
            sandbox_runtime is None
            or startup_config.bool_value("ASSISTANT_ALLOW_RUNC_CODE_EXECUTOR")
        ),
        image=str(startup_config.runtime_value("ASSISTANT_CODE_EXECUTOR_IMAGE")),
        python_executable=str(
            startup_config.runtime_value("ASSISTANT_CODE_EXECUTOR_PYTHON")
        ),
    )


def _gateway_secret_from_snapshot(
    startup_config: StartupConfigSnapshot,
    *,
    replay_protection: bool,
):
    """Construct the HMAC signer/verifier without any package-level env parse."""

    from ai_gateway_core.auth.gateway_secret import (
        GatewaySecret,
        InMemoryReplayStore,
        RedisReplayStore,
    )

    secret = startup_config.secret_value("GATEWAY_ASSISTANT_SHARED_SECRET")
    if not secret:
        return None
    keys: dict[str, str] = {}
    for entry in startup_config.secret_value("INTERNAL_AUTH_KEYS").split(","):
        if ":" not in entry:
            continue
        key_id, key_value = (part.strip() for part in entry.split(":", 1))
        if key_id and key_value:
            keys[key_id] = key_value
    active_key_id = str(
        startup_config.runtime_value("INTERNAL_AUTH_ACTIVE_KEY_ID")
    )
    auth_version = str(startup_config.runtime_value("INTERNAL_AUTH_VERSION"))
    if auth_version != "v2":
        raise RuntimeError("INTERNAL_AUTH_VERSION must be v2 for private service traffic")
    environment = str(startup_config.runtime_value("ENVIRONMENT")).strip().lower()
    replay_backend = str(startup_config.runtime_value("INTERNAL_COMM_STATE_BACKEND")).lower()
    test_mode = bool(startup_config.runtime_value("PYTEST_CURRENT_TEST"))
    if (
        environment not in {"local", "dev", "development", "test", "testing"}
        and not test_mode
        and replay_backend != "redis"
    ):
        raise RuntimeError("INTERNAL_COMM_STATE_BACKEND must be redis outside local development/test")
    replay_store = InMemoryReplayStore()
    if replay_protection and replay_backend == "redis":
        redis_url = str(startup_config.runtime_value("INTERNAL_COMM_REDIS_URL"))
        if not redis_url:
            if not test_mode and environment not in {"local", "dev", "development", "test", "testing"}:
                raise RuntimeError("Redis replay protection is configured without a URL")
        else:
            replay_store = RedisReplayStore.from_url(redis_url)
    return GatewaySecret(
        secret=secret,
        version=auth_version,
        key_id=active_key_id,
        keys=keys or {active_key_id: secret},
        replay_store=replay_store,
    )


def _register_subagent_tool_if_enabled(
    agent_definitions: Iterable[Any] = (),
    *,
    enabled: bool | None = None,
) -> bool:
    """Register delegation only when the rollout flag explicitly enables it."""

    resolved_enabled = (
        _env_truthy("ASSISTANT_SUBAGENTS_ENABLED") if enabled is None else bool(enabled)
    )
    if not resolved_enabled:
        return False
    from .core.tools.subagent_tool import register_subagent_tool

    definitions = tuple(agent_definitions)
    if definitions:
        register_subagent_tool(agent_definitions=definitions)
    else:
        register_subagent_tool()
    return True


def _initialize_agent_plugin_catalog(app: FastAPI):
    """Discover inert plugin agents without depending on DB/runtime memory."""

    from .core.agent.plugin_catalog import AgentPluginCatalog

    catalog = AgentPluginCatalog.load(
        str(_STARTUP_CONFIG.runtime_value("ASSISTANT_AGENT_PLUGIN_PATHS")),
        enabled=_STARTUP_CONFIG.bool_value("ASSISTANT_SUBAGENTS_ENABLED"),
    )
    app.state.agent_plugin_catalog = catalog
    app.state.agent_plugin_catalog_status = catalog.status
    logger.info(
        "Agent Plugin catalog initialized enabled=%s agents=%s",
        catalog.enabled,
        len(catalog.agents),
    )
    return catalog


def _register_catalog_subagent_tool(catalog) -> bool:
    """Publish validated profiles through the composition-root tool seam."""

    registered = _register_subagent_tool_if_enabled(
        catalog.agents,
        enabled=catalog.enabled,
    )
    if registered:
        logger.info(
            "Sub-agent delegation enabled (plugin_agents=%s)",
            len(catalog.agents),
        )
    return registered


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


async def _initialize_model_registry(
    database,
    startup_config: StartupConfigSnapshot = _STARTUP_CONFIG,
):
    """Configure providers and load the default tenant model catalog."""
    # ── Model Registry ──
    from .core.models.model_registry import (
        ModelProvider,
        ModelRegistry,
        configure_stream_smoother,
    )

    vertex_models = {
        item.strip()
        for item in str(startup_config.runtime_value("GOOGLE_VERTEX_MODELS")).split(",")
        if item.strip()
    }
    model_registry = ModelRegistry(
        vertex_models=vertex_models,
        vertex_api_key_override=startup_config.providers["google-vertex"].api_key,
        startup_config_frozen=True,
    )
    configure_stream_smoother(
        disabled=bool(startup_config.runtime_value("GEMINI_SMOOTHER_DISABLED"))
    )

    # Provider config must mirror gateway's (src/main.py). When the gateway
    # chat-stream route proxies to assistant-service, the request specifies a
    # ``model_id`` that ModelRegistry must resolve against a CONFIGURED provider
    # — a model routed to ``google-vertex`` or ``dashscope`` with the chat-key
    # override fails here if the provider isn't configured. Previously the
    # gateway ran assistant code in-process so its registry was used; now that
    # assistant-service is the real execution target, its registry has to know
    # every provider the gateway knows.
    #
    for pid, provider_config in startup_config.providers.items():
        if not provider_config.api_key:
            continue

        try:
            kwargs = {
                "api_key": provider_config.api_key,
                "base_url": provider_config.base_url,
            }
            if provider_config.backend is not None:
                kwargs["backend"] = provider_config.backend
            if provider_config.wire_protocol is not None:
                kwargs["wire_protocol"] = provider_config.wire_protocol
            model_registry.configure_provider(ModelProvider(pid), **kwargs)
            logger.info("Provider %s configured", pid)
        except ValueError as exc:
            log_internal_exception(
                logger,
                "assistant.provider.configuration_rejected",
                exc,
                level=logging.WARNING,
            )
            logger.warning("Provider configuration skipped provider=%s", pid)
        except Exception as exc:
            log_internal_exception(
                logger,
                "assistant.provider.configuration_failed",
                exc,
                level=logging.WARNING,
            )

    # Load models from database
    if database and getattr(database, "_pool", None):
        try:
            async with database._pool.acquire() as conn:
                rows = await conn.fetch(
                    "SELECT model_id, display_name, provider_id, context_window, max_output_tokens, "
                    "supports_vision, supports_tools, catalog_capabilities, capability_overrides, "
                    "capability_revision, input_price_per_1k, output_price_per_1k, access_level "
                    "FROM llm_models WHERE tenant_id = $1 AND is_enabled = true "
                    "ORDER BY sort_order ASC, model_id ASC",
                    "default",
                )
                if rows:
                    loaded_count = model_registry.replace_models_from_database_rows(
                        rows,
                        default_context_window=32000,
                    )
                    logger.info("Loaded %s models from DB", loaded_count)
        except Exception as exc:
            log_internal_exception(
                logger,
                "assistant.model_catalog.database_load_failed",
                exc,
                level=logging.WARNING,
            )

    return model_registry


async def _shutdown_assistant_service(
    *,
    agent_plugin_mcp_manager,
    mcp_runtime,
    database,
    redis_client,
    model_config_watch_task,
    tenant_model_registry_resolver,
) -> None:
    """Drain requests, then close process-scoped resources in startup order."""
    from ai_gateway_core.proxy.drain import DRAIN

    if not await DRAIN.wait_drained(timeout=30.0):
        logger.warning(
            "drain timeout — %d request(s) still in flight at shutdown",
            DRAIN.inflight,
        )

    if model_config_watch_task is not None:
        model_config_watch_task.cancel()
        # The watcher is best-effort; a watcher that already died on a Redis
        # error must not break the shutdown ordering either. CancelledError is
        # a BaseException since Python 3.8, so it must be named explicitly.
        try:
            await model_config_watch_task
        except (asyncio.CancelledError, Exception) as exc:
            log_internal_exception(
                logger,
                "assistant.model_config.watch_shutdown_failed",
                exc,
                level=logging.WARNING,
            )
    if agent_plugin_mcp_manager is not None:
        await agent_plugin_mcp_manager.shutdown()
    if mcp_runtime is not None:
        await mcp_runtime.close()
    if tenant_model_registry_resolver is not None:
        await tenant_model_registry_resolver.close()
    if database:
        await database.close()
    if redis_client:
        await redis_client.close()
    logger.info("Assistant Service shut down")


async def _watch_model_config_changes(redis_client, resolver) -> None:
    """Invalidate exact tenant model snapshots after Gateway CRUD commits.

    The subscription is a liveness optimization (bounded TTL is the degraded
    fallback), so a dropped Redis connection must never kill the watcher
    silently: reconnect with capped backoff instead.
    """

    channel = "gateway:model-config:changed:v1"
    backoff = 1.0
    while True:
        pubsub = redis_client.pubsub()
        try:
            await pubsub.subscribe(channel)
            backoff = 1.0
            async for message in pubsub.listen():
                if message.get("type") != "message":
                    continue
                try:
                    payload = json.loads(message.get("data") or "{}")
                    await resolver.invalidate(
                        tenant_id=payload.get("tenant_id"),
                        model_id=payload.get("model_id"),
                        provider_id=payload.get("provider_id"),
                    )
                except Exception as exc:
                    log_internal_exception(
                        logger,
                        "assistant.model_config.invalidation_failed",
                        exc,
                        level=logging.WARNING,
                    )
        except asyncio.CancelledError:
            raise
        except Exception as exc:
            log_internal_exception(
                logger,
                "assistant.model_config.watch_reconnecting",
                exc,
                level=logging.WARNING,
            )
            await asyncio.sleep(backoff)
            backoff = min(backoff * 2.0, 30.0)
        finally:
            try:
                await pubsub.unsubscribe(channel)
            except Exception as exc:
                log_internal_exception(
                    logger,
                    "assistant.model_config.watch_unsubscribe_failed",
                    exc,
                    level=logging.WARNING,
                )
            try:
                await pubsub.aclose()
            except Exception as exc:
                log_internal_exception(
                    logger,
                    "assistant.model_config.watch_close_failed",
                    exc,
                    level=logging.WARNING,
                )


@asynccontextmanager
async def lifespan(app: FastAPI):
    """Startup / shutdown lifecycle."""
    logger.info("Assistant Service starting...")
    app.state.startup_config = _STARTUP_CONFIG
    app.state.startup_config_summary = _STARTUP_CONFIG_SUMMARY
    from .core.tools.tool_registry import configure_test_only_direct_registry_bypass

    configure_test_only_direct_registry_bypass(
        bool(_STARTUP_CONFIG.runtime_value("PYTEST_CURRENT_TEST"))
    )
    runtime_features = _resolved_runtime_feature_contract()
    app.state.runtime_feature_contract = runtime_features
    agent_plugin_catalog = _initialize_agent_plugin_catalog(app)

    # ── OpenTelemetry SDK bootstrap — must run BEFORE database init below
    # so AsyncPGInstrumentor patches asyncpg before any pool is created.
    # Idempotent: a duplicate call is a debug-log no-op. Endpoint comes
    # from OTEL_EXPORTER_OTLP_ENDPOINT env; unset → in-process spans only.
    from ai_gateway_core.tracing import init_tracing

    otlp_endpoint = str(
        _STARTUP_CONFIG.runtime_value("OTEL_EXPORTER_OTLP_ENDPOINT")
    )
    if otlp_endpoint:
        init_tracing("assistant-service", otlp_endpoint=otlp_endpoint)
    else:
        init_tracing("assistant-service")

    # ── Graceful drain — install SIGTERM/SIGINT handlers so the orchestrator's
    # "please stop" signal flips ``DRAIN`` and the shutdown path below can wait
    # for in-flight requests to finish. The middleware that consumes ``DRAIN``
    # is registered after the FastAPI() instance is built (see below — placed
    # BEFORE CORSMiddleware so the 503 short-circuit fires first).
    from ai_gateway_core.proxy.drain import install_signal_handlers

    install_signal_handlers(asyncio.get_running_loop())

    # Mandatory init failures (DB, Redis when REQUIRE_REDIS is set) raise
    # from lifespan so the container crashes instead of starting in a half-broken
    # state where /health returns 200 but every chat request 500s. Optional
    # integrations (Confluence, Tavily, etc.) further down still warn-and-continue.
    require_db = _STARTUP_CONFIG.bool_value("ASSISTANT_REQUIRE_DB")
    require_redis = _STARTUP_CONFIG.bool_value("ASSISTANT_REQUIRE_REDIS")

    # ── Database ──
    database = None
    db_dsn = str(_STARTUP_CONFIG.runtime_value("DATABASE_URL"))
    try:
        from ai_gateway_core.persistence import DatabaseStorage

        # SPO-05 / D1 / SOTA: configurable connection pool for assistant worker
        pool_min_size = _STARTUP_CONFIG.int_value("ASSISTANT_DB_POOL_MIN_SIZE")
        pool_max_size = _STARTUP_CONFIG.int_value("ASSISTANT_DB_POOL_MAX_SIZE")
        database = DatabaseStorage(
            db_dsn,
            enabled=True,
            auto_init=False,
            pool_min_size=pool_min_size,
            pool_max_size=pool_max_size,
        )
        await database.connect()
        app.state.database = database
        logger.info("Database connected")
    except Exception as exc:
        database = None
        log_internal_exception(
            logger,
            "assistant.database.startup_failed",
            exc,
            level=logging.ERROR if require_db else logging.WARNING,
        )
        if require_db:
            raise RuntimeError(
                "Database is mandatory but failed to initialize. "
                "Either fix DATABASE_URL/connectivity or set ASSISTANT_REQUIRE_DB=false "
                "(dev only — production must not run without DB)."
            ) from None

    tenant_tool_policy = _configure_agent_runtime_resource_policies(app, database)

    # ── Redis ──
    redis_client = None
    redis_storage = None  # RedisStorage wrapper for shared session cache
    redis_url = str(_STARTUP_CONFIG.runtime_value("REDIS_URL"))
    if redis_url and _STARTUP_CONFIG.bool_value("ASSISTANT_REDIS__ENABLED"):
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
        except Exception as exc:
            log_internal_exception(
                logger,
                "assistant.redis.startup_failed",
                exc,
                level=logging.ERROR if require_redis else logging.WARNING,
            )
            if require_redis:
                raise RuntimeError(
                    "Redis is mandatory but failed. "
                    "Async image tasks fall back to in-process dict without Redis, "
                    "which breaks across container restarts and multi-replica deploys."
                ) from None

    model_registry = await _initialize_model_registry(database)

    app.state.model_registry = model_registry

    # ── Tenant MCP registry/runtime ──
    mcp_runtime = None
    agent_plugin_mcp_manager = None
    agent_plugin_mcp_server_names: set[str] = set()
    default_agent_plugin_mcp_server_names: set[str] = set()
    mcp_repository = None
    mcp_secret_resolver = None
    mcp_enabled = _STARTUP_CONFIG.bool_value("AGENT_STUDIO_MCP_ENABLED")
    app.state.agent_studio_mcp_enabled = mcp_enabled
    if database and mcp_enabled:
        try:
            from ai_gateway_core.persistence.repositories.mcp_repository import (
                DatabaseMCPRepository,
            )

            from .core.mcp.runtime import (
                MappingSecretResolver,
                MCPDiscoveryService,
                MCPRuntimeService,
            )

            mcp_repository = DatabaseMCPRepository(database)
            mcp_secret_resolver = MappingSecretResolver(
                dict(_STARTUP_CONFIG.mcp_secret_values)
            )
            mcp_runtime = MCPRuntimeService(
                repository=mcp_repository,
                secret_resolver=mcp_secret_resolver,
                client_cache_ttl_seconds=_STARTUP_CONFIG.int_value(
                    "ASSISTANT_MCP_CLIENT_CACHE_TTL_SECONDS"
                ),
                client_cache_max_entries=_STARTUP_CONFIG.int_value(
                    "ASSISTANT_MCP_CLIENT_CACHE_MAX_ENTRIES"
                ),
            )
            app.state.mcp_repository = mcp_repository
            app.state.mcp_runtime = mcp_runtime
            app.state.mcp_discovery_service = MCPDiscoveryService(
                secret_resolver=mcp_secret_resolver,
            )
            logger.info("Tenant MCP registry/runtime initialized")
        except Exception as exc:
            # MCP is optional for the built-in Assistant. Agent-bound MCP will
            # remain absent and fail closed until this adapter is available.
            log_internal_exception(
                logger,
                "assistant.tenant_mcp.startup_failed",
                exc,
                level=logging.WARNING,
            )
    elif not mcp_enabled:
        logger.info("Tenant MCP registry/runtime disabled by feature flag")

    # ── KB Proxy ──
    kb_proxy = None
    kb_url = str(_STARTUP_CONFIG.runtime_value("KB_SERVICE_URL"))
    try:
        import httpx
        from ai_gateway_core.comm.retry import RetryPolicy
        from ai_gateway_core.knowledge import KBProxyClient

        kb_proxy = KBProxyClient(
            base_url=kb_url,
            timeout=httpx.Timeout(
                connect=float(
                    _STARTUP_CONFIG.runtime_value(
                        "KB_PROXY_CONNECT_TIMEOUT_SECONDS"
                    )
                ),
                read=float(
                    _STARTUP_CONFIG.runtime_value("KB_PROXY_READ_TIMEOUT_SECONDS")
                ),
                write=float(
                    _STARTUP_CONFIG.runtime_value("KB_PROXY_WRITE_TIMEOUT_SECONDS")
                ),
                pool=float(
                    _STARTUP_CONFIG.runtime_value("KB_PROXY_POOL_TIMEOUT_SECONDS")
                ),
            ),
            limits=httpx.Limits(
                max_connections=int(
                    _STARTUP_CONFIG.runtime_value("KB_PROXY_MAX_CONNECTIONS")
                ),
                max_keepalive_connections=int(
                    _STARTUP_CONFIG.runtime_value(
                        "KB_PROXY_MAX_KEEPALIVE_CONNECTIONS"
                    )
                ),
            ),
            retry_policy=RetryPolicy(
                max_attempts=int(
                    _STARTUP_CONFIG.runtime_value("KB_PROXY_RETRY_MAX_ATTEMPTS")
                ),
                base_delay_ms=int(
                    _STARTUP_CONFIG.runtime_value("SERVICE_RETRY_BASE_DELAY_MS")
                ),
                max_delay_ms=int(
                    _STARTUP_CONFIG.runtime_value("SERVICE_RETRY_MAX_DELAY_MS")
                ),
            ),
            gateway_secret=_gateway_secret_from_snapshot(
                _STARTUP_CONFIG,
                replay_protection=False,
            ),
        )
        logger.info(f"KB proxy → {kb_url}")
    except Exception as exc:
        log_internal_exception(
            logger,
            "assistant.kb_proxy.startup_failed",
            exc,
            level=logging.WARNING,
        )

    # ── Memory Service ──
    memory_service = None
    if database:
        try:
            from .core.memory_service import MemoryService

            memory_service = MemoryService(database)
        except Exception as exc:
            log_internal_exception(
                logger,
                "assistant.memory_service.startup_failed",
                exc,
                level=logging.WARNING,
            )

    # Runtime memory/index/skill dependencies are process-scoped. Build the
    # adapter once and share it with both the memory tool and every AgentLoop.
    assistant_runtime_adapter = None
    assistant_runtime_adapter_unavailable = False
    if database is not None:
        try:
            from .core.runtime.compat.runtime_adapter import (
                AssistantRuntimeAdapter,
                AssistantRuntimeFeatures,
            )

            assistant_runtime_adapter = AssistantRuntimeAdapter.from_env(
                database=database,
                agent_plugin_catalog=agent_plugin_catalog,
                base_memory_dir=str(
                    _STARTUP_CONFIG.runtime_value("ASSISTANT_RUNTIME_MEMORY_DIR")
                )
                or None,
                legacy_memory_dir=str(
                    _STARTUP_CONFIG.runtime_value("ASSISTANT_RUNTIME_LEGACY_MEMORY_DIR")
                )
                or None,
                memory_max_source_bytes=int(
                    _STARTUP_CONFIG.runtime_value(
                        "ASSISTANT_RUNTIME_MEMORY_MAX_SOURCE_BYTES"
                    )
                ),
                features=AssistantRuntimeFeatures(
                    memory_v2=_STARTUP_CONFIG.bool_value("ASSISTANT_RUNTIME_MEMORY_V2"),
                    context_v2=_STARTUP_CONFIG.bool_value("ASSISTANT_RUNTIME_CONTEXT_V2"),
                    tool_policy_v2=_STARTUP_CONFIG.bool_value(
                        "ASSISTANT_RUNTIME_TOOL_POLICY_V2"
                    ),
                    skills=_STARTUP_CONFIG.bool_value("ASSISTANT_RUNTIME_SKILLS"),
                    scheduler=_STARTUP_CONFIG.bool_value("ASSISTANT_RUNTIME_SCHEDULER"),
                    failover_v2=_STARTUP_CONFIG.bool_value("ASSISTANT_RUNTIME_FAILOVER_V2"),
                ),
            )
            logger.info("Assistant runtime adapter initialized")
        except Exception as exc:
            assistant_runtime_adapter_unavailable = True
            log_internal_exception(
                logger,
                "assistant.runtime_adapter.startup_failed",
                exc,
            )
    app.state.assistant_runtime_adapter = assistant_runtime_adapter

    # ── Session Manager ──
    session_manager = None
    if database:
        try:
            from ai_gateway_core.session import DatabaseSessionManager

            session_manager = DatabaseSessionManager(database, redis=redis_storage)
        except Exception as exc:
            log_internal_exception(
                logger,
                "assistant.session_manager.startup_failed",
                exc,
                level=logging.WARNING,
            )

    # ── Tool Registry ──
    # `search_web` (Tavily) was deleted in PR-2 — capable models use their own
    # native search (Qwen `enable_search`, Anthropic `web_search_20250305`),
    # `web_fetch` is the URL-fetch fallback for everything else.
    from .core.tools import (
        register_builtin_tools,
        register_document_generation_tool,
        register_pptx_generation_tool,
        register_quiz_tool,
    )
    from .core.tools.image_generator_tool import register_image_generation_tool

    register_builtin_tools(
        kb_service=kb_proxy,
        memory_service=memory_service,
        runtime_adapter=assistant_runtime_adapter,
    )
    # Operator-installed Agent Plugins are an independent component boundary:
    # valid Streamable HTTP servers connect even when a sibling Skill is
    # invalid, and a connection failure falls back to the built-in generators.
    agent_plugin_mcp_results: dict[str, int] = {}
    try:
        from .core.mcp.config import load_agent_plugin_mcp_config
        from .core.mcp.manager import MCPManager

        plugin_mcp_configs = load_agent_plugin_mcp_config(
            startup_config=_STARTUP_CONFIG
        )
        if plugin_mcp_configs:
            agent_plugin_mcp_manager = MCPManager(plugin_mcp_configs)
            agent_plugin_mcp_results = await agent_plugin_mcp_manager.initialize_all()
            agent_plugin_mcp_server_names = {
                name for name, count in agent_plugin_mcp_results.items() if count > 0
            }
            default_agent_plugin_mcp_server_names = {
                config.name
                for config in plugin_mcp_configs
                if config.default_tenant_enabled
                and agent_plugin_mcp_results.get(config.name, 0) > 0
            }
            app.state.agent_plugin_mcp_manager = agent_plugin_mcp_manager
            app.state.agent_plugin_mcp_status = dict(agent_plugin_mcp_results)
            logger.info(
                "Agent Plugin MCP initialized servers=%s tools=%s",
                len(agent_plugin_mcp_server_names),
                sum(max(0, count) for count in agent_plugin_mcp_results.values()),
            )
    except Exception as exc:
        log_internal_exception(
            logger,
            "assistant.agent_plugin_mcp.startup_failed",
            exc,
            level=logging.WARNING,
        )

    docgen_plugin_ready = agent_plugin_mcp_results.get("docgen", 0) > 0
    if docgen_plugin_ready:
        logger.info("Using Agent Plugin docgen tool; legacy document generators disabled")
    else:
        register_document_generation_tool()
        try:
            register_pptx_generation_tool()
        except Exception as exc:
            log_internal_exception(
                logger,
                "assistant.pptx_tool.registration_failed",
                exc,
                level=logging.WARNING,
            )
    register_image_generation_tool(startup_config=_STARTUP_CONFIG)
    # Quiz generation is a first-class Assistant capability.  The executor
    # validates and persists model-supplied questions without a second model
    # call, so it must be wired to the live Assistant database just like the
    # other built-in generation tools.
    register_quiz_tool(database=database)

    # ── Todo tools (Phase 5) — always on; exposes the per-session
    # WorkingMemory to the model for long-horizon task tracking.
    from .core.tools.todo_tools import register_todo_tools

    register_todo_tools()

    # ── Context management tool — always on; lets the model (or a user
    # /compact slash command) explicitly request history compression.
    from .core.tools.context_tools import register_context_tools

    register_context_tools()

    # Three small, stateless bridge schemas keep arbitrary tenant-authorized
    # plugin/MCP catalogs reachable without sending every full schema on every
    # model turn. Underlying calls still return through the canonical gateway.
    from .core.tools.tool_discovery import register_tool_discovery_tools

    register_tool_discovery_tools()

    _register_catalog_subagent_tool(agent_plugin_catalog)

    # ── Primitive tools (Phase 4) — env-gated opt-in ──
    # Exposes fs_read/fs_write/fs_glob/fs_grep to the model. Requires a writable
    # workspace root (default /tmp/ai-gateway-workspace, override with
    # ASSISTANT_WORKSPACE_ROOT). Off by default to keep legacy deployments
    # unchanged.
    if _STARTUP_CONFIG.bool_value("ASSISTANT_ENABLE_PRIMITIVES"):
        from .core.tools.primitives import register_primitive_tools
        from .core.tools.workspace import configure_workspace_root

        configure_workspace_root(
            str(_STARTUP_CONFIG.runtime_value("ASSISTANT_WORKSPACE_ROOT"))
        )
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
    from .core.agent.artifact_persister import resolve_artifact_storage_for_persistence

    realtime_metrics = get_realtime_metrics()
    usage_recorder = get_usage_recorder()

    # Artifact storage init — the ai_gateway_core singleton is per-process.
    # Gateway initializes it during its own startup; AS runs in a separate
    # container and must initialize its own instance from the GATEWAY_STORAGE__*
    # env vars (prod hands AS the same bucket so artifacts are visible to both
    # services).
    if get_artifact_storage() is None:
        try:
            storage_config = _storage_config_from_snapshot(_STARTUP_CONFIG)
            init_artifact_storage(storage_config, database)
            init_file_storage(storage_config)
            logger.info(f"Artifact storage initialized (backend={storage_config.backend.value})")
        except Exception as exc:
            log_internal_exception(
                logger,
                "assistant.artifact_storage.startup_failed",
                exc,
                level=logging.WARNING,
            )

    artifact_storage = resolve_artifact_storage_for_persistence(
        get_artifact_storage(),
        database,
    )
    artifact_metadata_ready = artifact_storage is not None
    logger.info(
        "Artifact metadata persistence ready=%s",
        artifact_metadata_ready,
    )
    from .core.tools.tool_artifact_reader import register_tool_artifact_reader

    if register_tool_artifact_reader(
        artifact_storage,
        max_limit_tokens=_STARTUP_CONFIG.int_value(
            "ASSISTANT_TOOL_ARTIFACT_READ_MAX_TOKENS"
        ),
    ):
        logger.info("Scoped tool-output artifact reader registered")
    try:
        file_storage = get_file_storage()
    except RuntimeError as exc:
        log_internal_exception(
            logger,
            "assistant.file_storage.lookup_failed",
            exc,
            level=logging.DEBUG,
        )
        try:
            file_storage = init_file_storage(
                _storage_config_from_snapshot(_STARTUP_CONFIG)
            )
            logger.info(f"File storage initialized (backend={file_storage.config.backend.value})")
        except Exception as init_err:
            log_internal_exception(
                logger,
                "assistant.file_storage.startup_failed",
                init_err,
                level=logging.WARNING,
            )
            from ai_gateway_core.storage import NoOpFileStorage

            file_storage = NoOpFileStorage()

    # One invoker owns the process-local result cache and policy/MCP adapters;
    # the gateway and all per-request AgentLoop instances reuse this identity.
    from .core.audit.composition import create_audited_tool_invoker
    from .core.mcp.tenant_mcp_config import TenantMCPConfigService

    tenant_mcp_config = TenantMCPConfigService(
        database=database,
        all_server_names=sorted(agent_plugin_mcp_server_names),
        default_allowed_servers=set(default_agent_plugin_mcp_server_names),
    )

    tool_invoker = create_audited_tool_invoker(
        database=database,
        tenant_tool_policy=tenant_tool_policy,
        tenant_mcp_config=tenant_mcp_config,
        mcp_runtime=mcp_runtime,
    )
    app.state.tool_invoker = tool_invoker

    code_executor = None
    if _STARTUP_CONFIG.bool_value("ASSISTANT_CODE_EXECUTOR_ENABLED"):
        from .core.code_executor import get_code_executor

        code_executor = get_code_executor(
            config=_code_execution_config_from_snapshot(_STARTUP_CONFIG),
            allow_runc_code_executor=_STARTUP_CONFIG.bool_value(
                "ASSISTANT_ALLOW_RUNC_CODE_EXECUTOR"
            ),
            startup_config=_STARTUP_CONFIG,
        )
        if not code_executor.is_docker_available():
            raise RuntimeError(
                "ASSISTANT_CODE_EXECUTOR_ENABLED requires a reachable Docker daemon "
                "and an approved sandbox runtime"
            )
        logger.warning(
            "Assistant code executor enabled; Docker sandbox policy must be reviewed "
            "for this environment"
        )

    from .core.models.tenant_registry import TenantModelRegistryResolver
    from .core.trace_writer import AssistantTraceWriter

    tenant_model_registry_resolver = (
        TenantModelRegistryResolver(
            database,
            encryption_key=_STARTUP_CONFIG.secret_value("GATEWAY_ENCRYPTION_KEY"),
        )
        if database is not None
        else None
    )
    model_config_watch_task = (
        asyncio.create_task(
            _watch_model_config_changes(redis_client, tenant_model_registry_resolver),
            name="assistant-model-config-watch",
        )
        if redis_client is not None and tenant_model_registry_resolver is not None
        else None
    )

    trace_writer = AssistantTraceWriter(
        database=database,
        startup_config=_STARTUP_CONFIG,
    )
    assistant_service = AssistantService(
        model_registry=model_registry,
        kb_service=None,
        kb_proxy=kb_proxy,
        session_manager=session_manager,
        redis_client=redis_client,
        memory_service=memory_service,
        db=database,
        trace_writer=trace_writer,
        usage_recorder=usage_recorder,
        realtime_metrics=realtime_metrics,
        artifact_storage=artifact_storage,
        file_storage=file_storage,
        mcp_runtime=mcp_runtime,
        tenant_tool_policy=tenant_tool_policy,
        runtime_adapter=assistant_runtime_adapter,
        tool_invoker=tool_invoker,
        runtime_adapter_unavailable=assistant_runtime_adapter_unavailable,
        code_executor=code_executor,
        startup_config=_STARTUP_CONFIG,
    )
    assistant_service.tenant_model_registry_resolver = tenant_model_registry_resolver
    app.state.assistant_service = assistant_service
    app.state.session_manager = session_manager
    app.state.kb_proxy = kb_proxy
    app.state.memory_service = memory_service
    try:
        from .core.runtime.memory.governance_cleanup import (
            AgentRuntimeMemoryCleanupService,
        )

        # Governance cleanup intentionally keeps a separate adapter because it
        # injects a deletion/readback-only Qdrant client with stricter receipts.
        app.state.runtime_memory_cleanup_service = (
            AgentRuntimeMemoryCleanupService.from_startup_config(
                database=database,
                startup_config=_STARTUP_CONFIG,
            )
        )
    except Exception as exc:
        app.state.runtime_memory_cleanup_service = None
        log_internal_exception(
            logger,
            "assistant.runtime_memory_cleanup.startup_failed",
            exc,
            level=logging.WARNING,
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
                except Exception as exc:
                    log_internal_exception(
                        logger,
                        "assistant.confluence.startup_query_failed",
                        exc,
                        level=logging.WARNING,
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
        except Exception as exc:
            log_internal_exception(
                logger,
                "assistant.confluence.registration_failed",
                exc,
            )

    app.state._ready = True
    logger.info("Assistant Service ready ✓")

    yield  # ── Running ──

    await _shutdown_assistant_service(
        agent_plugin_mcp_manager=agent_plugin_mcp_manager,
        mcp_runtime=mcp_runtime,
        database=database,
        redis_client=redis_client,
        model_config_watch_task=model_config_watch_task,
        tenant_model_registry_resolver=tenant_model_registry_resolver,
    )


# ── Create App ──

app = FastAPI(
    title="AI Assistant Service",
    version="0.1.0",
    lifespan=lifespan,
)
app.state.startup_config = _STARTUP_CONFIG
app.state.startup_config_summary = _STARTUP_CONFIG_SUMMARY
app.state.runtime_feature_contract = _resolved_runtime_feature_contract()

_origins = [
    origin.strip()
    for origin in str(
        _STARTUP_CONFIG.runtime_value("ASSISTANT_CORS__ALLOW_ORIGINS")
    ).split(",")
    if origin.strip()
]
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
    backend = str(_STARTUP_CONFIG.runtime_value("INTERNAL_IDEMPOTENCY_BACKEND"))
    if backend == "redis":
        redis_url = str(_STARTUP_CONFIG.runtime_value("INTERNAL_COMM_REDIS_URL"))
        if redis_url:
            import redis.asyncio as aioredis

            return RedisIdempotencyStore(aioredis.from_url(redis_url, decode_responses=False))
    return InMemoryIdempotencyStore()


app.add_middleware(
    IdempotencyMiddleware,
    store=_idempotency_store_from_env(),
    ttl_seconds=int(_STARTUP_CONFIG.runtime_value("INTERNAL_IDEMPOTENCY_TTL_SECONDS")),
)

# Phase 5a: reject traffic without a valid ``X-Gateway-Secret``. Closes
# Audit Finding H-4 (sibling-container SSRF → user impersonation). Skipped
# entirely when the secret env var is unset (local dev); in prod compose
# the env MUST be set and ``ASSISTANT_APP__ALLOW_ANONYMOUS`` MUST be
# ``false`` for the middleware to actively reject.
_gateway_secret_env = _STARTUP_CONFIG.secret_value("GATEWAY_ASSISTANT_SHARED_SECRET").strip()
from ai_gateway_core.auth.gateway_secret_middleware import (  # noqa: E402
    validate_gateway_auth_configuration,
)

validate_gateway_auth_configuration(
    secret=_gateway_secret_env,
    allow_anonymous=_STARTUP_CONFIG.bool_value("ASSISTANT_APP__ALLOW_ANONYMOUS"),
    allow_anonymous_setting="ASSISTANT_APP__ALLOW_ANONYMOUS",
    environment=str(_STARTUP_CONFIG.runtime_value("ENVIRONMENT")),
)
if _gateway_secret_env:
    from .auth import GatewaySecretAuthMiddleware

    app.add_middleware(
        GatewaySecretAuthMiddleware,
        gateway_secret=_gateway_secret_from_snapshot(
            _STARTUP_CONFIG,
            replay_protection=True,
        ),
        allow_anonymous=_STARTUP_CONFIG.bool_value("ASSISTANT_APP__ALLOW_ANONYMOUS"),
        # These two private routes authenticate the Rust Runtime with the
        # distinct AI_PLATFORM_INTERNAL_TOKEN inside the route handler.
        separately_authenticated_paths=frozenset(
            {
                "/internal/v1/capabilities/catalog",
                "/internal/v1/capabilities/invoke",
            }
        ),
    )
    logger.info(
        "Gateway-secret middleware active (allow_anonymous=%s)",
        _STARTUP_CONFIG.bool_value("ASSISTANT_APP__ALLOW_ANONYMOUS"),
    )
elif not _STARTUP_CONFIG.bool_value("ASSISTANT_APP__ALLOW_ANONYMOUS"):
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
            "startup_config_schema_version": _STARTUP_CONFIG_SUMMARY["schema_version"],
            "startup_config_fingerprint": _STARTUP_CONFIG.sha256,
            "runtime_feature_fingerprint": dict(
                getattr(app.state, "runtime_feature_contract", {})
            ).get("sha256"),
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
from .api.routes.capability_plane import router as capability_plane_router  # noqa: E402

app.include_router(capability_plane_router)
