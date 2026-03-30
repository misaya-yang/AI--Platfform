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
            getattr(structlog, "get_level_from_name", lambda l: getattr(__import__("logging"), l.upper(), 20))(level)
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

        # --- Initialize KnowledgeService + Worker ---
        knowledge_service = None
        knowledge_worker = None
        try:
            from .persistence.database import DatabaseStorage as FullDatabaseStorage
            from .services.knowledge.knowledge_service import KnowledgeService
            from .services.knowledge.worker import KnowledgeWorker

            db_storage = FullDatabaseStorage(
                dsn=resolved.database.dsn,
                enabled=True,
                auto_init=False,  # Schema already exists
                pool_min_size=resolved.database.pool_min_size,
                pool_max_size=resolved.database.pool_max_size,
            )
            await db_storage.connect()

            # KnowledgeService expects gateway-style settings with settings.knowledge.*
            # Create a compatibility wrapper that maps KB Service flat config
            class _SettingsCompat:
                """Adapts KB Service Settings to gateway Settings shape."""
                def __init__(self, s):
                    self._s = s
                    embed = s.embeddings
                    self.knowledge = type("K", (), {
                        "enabled": True,
                        "qdrant": type("Q", (), {
                            "enabled": True,
                            "url": s.qdrant.url,
                            "api_key": s.qdrant.api_key,
                            "timeout_seconds": s.qdrant.timeout_seconds,
                            "prefer_grpc": s.qdrant.prefer_grpc,
                            "max_retries": getattr(s.qdrant, "max_retries", 3),
                            "retry_base_delay": getattr(s.qdrant, "retry_base_delay", 1.0),
                        })(),
                        "dashscope": type("D", (), {
                            "api_key": embed.api_key if embed.provider == "dashscope" else "",
                            "model_name": embed.model if embed.provider == "dashscope" else "",
                        })(),
                        "gemini": type("G", (), {
                            "api_key": embed.api_key if embed.provider == "gemini" else "",
                            "model_name": embed.model if embed.provider == "gemini" else "",
                        })(),
                        "siliconflow": type("SF", (), {
                            "api_key": embed.api_key if embed.provider == "siliconflow" else "",
                            "base_url": embed.base_url or "",
                            "model_name": embed.model if embed.provider == "siliconflow" else "",
                        })(),
                        "ocr_enabled": s.ocr.enabled,
                        "ocr_strategy": s.ocr.strategy,
                        "worker_concurrency": s.processing.worker_concurrency,
                        "document_worker_concurrency": s.processing.document_worker_concurrency,
                        # Embedding config
                        "text_embedding_dimension": embed.dimension,
                        "text_embedding_batch_size": embed.batch_size,
                        "text_embedding_max_concurrent": embed.max_concurrent,
                        "text_embedding_config": {
                            "provider": embed.provider,
                            "model": embed.model,
                            "api_key": embed.api_key,
                            "dimension": embed.dimension,
                            "base_url": embed.base_url or "",
                        },
                        "default_embedding_model": embed.model,
                        "default_embedding_provider": embed.provider,
                        # Multimodal (optional)
                        "multimodal_embedding_model": getattr(s, "multimodal", type("M", (), {"model": ""})()).model,
                        "multimodal_embedding_max_concurrent": 5,
                        # VLM
                        "vlm_max_concurrent": getattr(s.ocr, "vlm_concurrency", 4),
                        # Islamic profile (optional)
                        "islamic_profile": None,
                    })()
                def __getattr__(self, name):
                    return getattr(self._s, name)

            compat_settings = _SettingsCompat(resolved)

            # Initialize S3 ImageStorageService for file persistence
            image_storage = None
            try:
                from .storage.image_storage import (
                    ImageStorageService, StorageBackend, StorageConfig,
                )
                storage_cfg = resolved.storage
                if storage_cfg.backend == "s3" and storage_cfg.s3.bucket:
                    sc = StorageConfig(
                        backend=StorageBackend.S3,
                        s3_bucket=storage_cfg.s3.bucket,
                        s3_region=storage_cfg.s3.region,
                        s3_access_key=storage_cfg.s3.access_key,
                        s3_secret_key=storage_cfg.s3.secret_key,
                        s3_endpoint_url=storage_cfg.s3.endpoint_url or None,
                        key_prefix=storage_cfg.key_prefix,
                        url_expiry_seconds=storage_cfg.url_expiry_seconds,
                    )
                    image_storage = ImageStorageService(sc)
                    logger.info("s3_storage_initialized", bucket=storage_cfg.s3.bucket)
                else:
                    sc = StorageConfig(
                        backend=StorageBackend.LOCAL,
                        local_base_path=storage_cfg.local_base_path,
                        key_prefix=storage_cfg.key_prefix,
                    )
                    image_storage = ImageStorageService(sc)
                    logger.info("local_storage_initialized", path=storage_cfg.local_base_path)
            except Exception as e:
                logger.warning("storage_init_failed", error=str(e))

            knowledge_service = KnowledgeService(
                settings=compat_settings,
                database=db_storage,
                image_storage_service=image_storage,
            )
            app.state.knowledge_service = knowledge_service

            # --- Initialize VLM OCR Service (if configured) ---
            vlm_ocr_service = None
            ocr = resolved.ocr
            if ocr.enabled and ocr.strategy in ("vlm", "hybrid"):
                try:
                    from .services.knowledge.vlm_ocr_service import VLMOCRService

                    if ocr.vlm_provider == "siliconflow" or "deepseek" in ocr.vlm_model.lower():
                        keys = [k.strip() for k in ocr.vlm_api_keys.split(",") if k.strip()]
                        if keys:
                            vlm_ocr_service = VLMOCRService(
                                api_keys=keys,
                                model=ocr.vlm_model or "deepseek-ai/DeepSeek-OCR",
                                provider="siliconflow",
                                base_url=ocr.vlm_base_url,
                                max_retries=3,
                            )
                    else:
                        ocr_api_key = resolved.embeddings.api_key if ocr.vlm_provider == "gemini" else ocr.vlm_api_keys or resolved.embeddings.api_key
                        if ocr_api_key:
                            vlm_ocr_service = VLMOCRService(
                                api_key=ocr_api_key,
                                model=ocr.vlm_model,
                                provider=ocr.vlm_provider,
                                concurrency=ocr.vlm_concurrency,
                                timeout_seconds=ocr.vlm_timeout_seconds,
                            )
                    if vlm_ocr_service:
                        logger.info("vlm_ocr_initialized", provider=vlm_ocr_service.provider, model=vlm_ocr_service.model)
                except Exception as e:
                    logger.warning("vlm_ocr_init_failed", error=str(e))

            # Initialize document type detector for auto-mode processing
            doc_detector = None
            try:
                from .services.knowledge.document_detector import DocumentTypeDetector
                doc_detector = DocumentTypeDetector()
                logger.info("document_detector_initialized")
            except Exception as e:
                logger.warning("document_detector_init_failed", error=str(e))

            knowledge_worker = KnowledgeWorker(
                knowledge_service,
                detector=doc_detector,
                vlm_ocr_service=vlm_ocr_service,
            )
            await knowledge_worker.start()
            app.state.knowledge_worker = knowledge_worker

            logger.info("knowledge_service_initialized",
                        worker_running=knowledge_worker.is_running if hasattr(knowledge_worker, 'is_running') else True)
        except Exception as e:
            logger.warning("knowledge_service_init_partial", error=str(e))
            # Service still boots — retrieve endpoint works, upload may not until init completes

        logger.info("knowledge_service_ready")
        yield

        # --- shutdown ---
        logger.info("knowledge_service_shutting_down")
        if knowledge_worker:
            try:
                await knowledge_worker.stop()
            except Exception:
                pass
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
    # Try full 51-endpoint router first; fall back to placeholder if imports fail
    try:
        from .api.routes.knowledge import router as full_knowledge_router
        app.include_router(full_knowledge_router, prefix="/api/v1")
        logger.info("knowledge_routes_loaded", mode="full", endpoints=51)
    except Exception as e:
        logger.warning("knowledge_routes_fallback", error=str(e), mode="placeholder")
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
