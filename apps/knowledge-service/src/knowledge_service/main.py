"""Knowledge Base Microservice entry-point.

Boots a FastAPI application with async lifespan management for the database
pool, Qdrant client, and background worker.
"""

from __future__ import annotations

from contextlib import asynccontextmanager
from typing import Any

import structlog
import uvicorn
from fastapi import FastAPI, Request
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import ORJSONResponse

from .api.router import api_router
from .config import Settings
from .db.connection import DatabasePool

# ---------------------------------------------------------------------------
# Structured logging
# ---------------------------------------------------------------------------

def configure_logging(level: str = "INFO") -> None:
    """Configure structlog with JSON rendering for production."""
    structlog.configure(
        processors=[
            structlog.contextvars.merge_contextvars,
            structlog.processors.add_log_level,
            structlog.processors.StackInfoRenderer(),
            structlog.dev.set_exc_info,
            structlog.processors.TimeStamper(fmt="iso"),
            structlog.dev.ConsoleRenderer()
            if level.upper() == "DEBUG"
            else structlog.processors.JSONRenderer(),
        ],
        wrapper_class=structlog.make_filtering_bound_logger(
            structlog.get_level_from_name(level)
        ),
        context_class=dict,
        logger_factory=structlog.PrintLoggerFactory(),
        cache_logger_on_first_use=True,
    )


logger = structlog.get_logger()

# ---------------------------------------------------------------------------
# Qdrant client helper
# ---------------------------------------------------------------------------

async def _init_qdrant(settings: Settings) -> Any:
    """Create and verify Qdrant async client connection."""
    from qdrant_client import AsyncQdrantClient

    qdrant = AsyncQdrantClient(
        url=settings.qdrant.url,
        api_key=settings.qdrant.api_key,
        prefer_grpc=settings.qdrant.prefer_grpc,
        timeout=settings.qdrant.timeout_seconds,
    )
    try:
        collections = await qdrant.get_collections()
        logger.info(
            "qdrant_connected",
            url=settings.qdrant.url,
            collections=len(collections.collections),
        )
    except Exception:
        logger.warning("qdrant_probe_failed", url=settings.qdrant.url)
    return qdrant


# ---------------------------------------------------------------------------
# FastAPI app factory
# ---------------------------------------------------------------------------

OPENAPI_TAGS = [
    {"name": "Health", "description": "Liveness and readiness probes."},
    {"name": "Datasets", "description": "Dataset CRUD and listing."},
    {"name": "Retrieval", "description": "Vector similarity search / RAG retrieval."},
    {"name": "Worker", "description": "Background ingestion worker status."},
]


def create_app(settings: Settings | None = None) -> FastAPI:
    """Build the FastAPI application with lifespan hooks."""
    resolved = settings or Settings()

    @asynccontextmanager
    async def lifespan(app: FastAPI):
        configure_logging(resolved.app.log_level)
        logger.info(
            "knowledge_service_starting",
            port=resolved.app.port,
            qdrant_url=resolved.qdrant.url,
        )

        # --- startup ---
        db = DatabasePool()
        await db.init(
            resolved.database.dsn,
            min_size=resolved.database.pool_min_size,
            max_size=resolved.database.pool_max_size,
        )
        app.state.db = db

        qdrant = await _init_qdrant(resolved)
        app.state.qdrant = qdrant

        app.state.settings = resolved

        logger.info("knowledge_service_ready")
        yield

        # --- shutdown ---
        logger.info("knowledge_service_shutting_down")
        if hasattr(qdrant, "close"):
            await qdrant.close()
        await db.close()
        logger.info("knowledge_service_stopped")

    app = FastAPI(
        title="Knowledge Base Service",
        version="0.1.0",
        description="Independent Knowledge Base microservice extracted from AI Gateway",
        openapi_tags=OPENAPI_TAGS,
        default_response_class=ORJSONResponse,
        lifespan=lifespan,
    )

    # --- CORS ---
    app.add_middleware(
        CORSMiddleware,
        allow_origins=["*"],
        allow_credentials=True,
        allow_methods=["*"],
        allow_headers=["*"],
    )

    # --- Health ---
    @app.get("/health", tags=["Health"])
    async def health(request: Request) -> dict[str, str]:
        return {"status": "ok", "service": "knowledge-service"}

    # --- API routes ---
    app.include_router(api_router, prefix="/api/v1/knowledge")

    app.state.settings = resolved
    return app


app = create_app()


def main() -> None:
    """CLI entry-point (``knowledge-service`` console script)."""
    settings = Settings()
    configure_logging(settings.app.log_level)
    uvicorn.run(
        "knowledge_service.main:app",
        host=settings.app.host,
        port=settings.app.port,
        reload=False,
    )


if __name__ == "__main__":
    main()
