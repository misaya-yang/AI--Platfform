from __future__ import annotations

from contextlib import asynccontextmanager
from dataclasses import dataclass
from pathlib import Path

import uvicorn
from fastapi import FastAPI

from .api.router import api_router
from .cache import RedisCache
from .clients.hadith_cdn_client import HadithCdnClient
from .clients.quran_foundation_client import QuranFoundationClient
from .clients.quran_user_client import QuranUserClient
from .config import Settings
from .db import Database
from .observability import configure_logging
from .repositories.dua_repository import DuaRepository
from .repositories.hadith_repository import HadithRepository
from .repositories.quran_repository import QuranRepository
from .repositories.sync_repository import SyncRepository
from .services.bootstrap_service import BootstrapService
from .services.dua_query_service import DuaQueryService
from .services.dua_sync_service import DuaSyncService
from .services.hadith_query_service import HadithQueryService
from .services.hadith_sync_service import HadithSyncService
from .services.quran_query_service import QuranQueryService
from .services.quran_sync_service import QuranSyncService
from .services.quran_user_service import QuranUserService

OPENAPI_TAGS = [
    {"name": "Health", "description": "Service liveness, readiness, and module-level health probes."},
    {"name": "Meta", "description": "Service metadata, sync manifest, and canonical row-count summary."},
    {"name": "Quran", "description": "Low-latency Quran read APIs — chapters, ayahs, translations, audio, word-level data. Backed by PostgreSQL (synced from quran.foundation)."},
    {"name": "Quran User", "description": "Quran user features via OAuth PKCE proxy — bookmarks, notes, collections. Requires `QURAN_USER__ENABLED=true`."},
    {"name": "Hadith", "description": "Hadith collections, books, and individual narrations with grades. 7 collections, 34,497 hadiths, 74,224 grades (synced from fawazahmed0 CDN + HuggingFace enrichment)."},
    {"name": "Dua", "description": "Verified Islamic Dua and Adhkar collection — 31 categories, 72 duas with Arabic text, transliteration, and English meaning."},
    {"name": "Sheikh Wahda", "description": "Wahda AI recommendation system — daily/religious recommendations, typeahead suggestions, trending questions, feedback, conversation sharing, and AI-generated personalized follow-up questions."},
]


@dataclass
class Runtime:
    settings: Settings
    db: Database
    cache: RedisCache
    quran_client: QuranFoundationClient
    quran_user_client: QuranUserClient
    hadith_client: HadithCdnClient
    bootstrap_service: BootstrapService
    quran_query_service: QuranQueryService
    quran_user_service: QuranUserService
    hadith_query_service: HadithQueryService
    dua_query_service: DuaQueryService
    wahda_service: object = None  # WahdaService (optional)
    gemini_client: object = None  # GeminiClient (optional, M-4)

    async def close(self) -> None:
        if self.gemini_client and hasattr(self.gemini_client, "close"):
            await self.gemini_client.close()
        await self.quran_client.close()
        await self.quran_user_client.close()
        await self.hadith_client.close()
        await self.cache.close()
        await self.db.close()


async def build_runtime(settings: Settings) -> Runtime:
    configure_logging(settings.app.log_level)
    db = Database(settings.database)
    await db.connect()
    # In editable install: parents[2] = project root. In Docker: use /app.
    src_relative = Path(__file__).resolve().parents[2] / "migrations"
    migrations_dir = src_relative if src_relative.is_dir() else Path("/app/migrations")
    if settings.database.auto_migrate:
        await db.migrate(migrations_dir / "001_init_schema.sql")
        await db.migrate(migrations_dir / "002_dua_tables.sql")
        await db.migrate(migrations_dir / "003_wahda_features.sql")
        await db.migrate(migrations_dir / "004_share_conversations.sql")
        await db.migrate(migrations_dir / "005_recommended_questions.sql")
        await db.migrate(migrations_dir / "007_hadith_chapters.sql")
        await db.migrate(migrations_dir / "008_strip_bidi_marks_arabic.sql")
        await db.migrate(migrations_dir / "009_fix_cross_book_hadith_linkage.sql")
        await db.migrate(migrations_dir / "010_drop_phantom_hadiths_and_recount.sql")
        await db.migrate(migrations_dir / "011_drop_empty_books.sql")
        await db.migrate(migrations_dir / "012_strip_replacement_and_advanced_bidi.sql")
        await db.migrate(migrations_dir / "013_strip_bidi_quran_translations.sql")
    cache = RedisCache(settings.cache)
    await cache.connect()

    sync_repository = SyncRepository(db)
    quran_repository = QuranRepository(db)
    hadith_repository = HadithRepository(db)
    dua_repository = DuaRepository(db)

    quran_client = QuranFoundationClient(settings.quran)
    quran_user_client = QuranUserClient(settings.quran_user)
    hadith_client = HadithCdnClient(settings.hadith)

    quran_sync_service = QuranSyncService(settings.quran, quran_client, quran_repository, sync_repository)
    hadith_sync_service = HadithSyncService(settings.hadith, hadith_client, hadith_repository, sync_repository)
    quran_query_service = QuranQueryService(settings.quran, settings.cache, quran_repository, cache)
    quran_user_service = QuranUserService(settings.quran_user, quran_user_client)
    hadith_query_service = HadithQueryService(settings.cache, hadith_repository, cache)
    src_data = Path(__file__).resolve().parents[2] / "data" / "islamic_dua_dataset_final.csv"
    dua_data_path = settings.dua.data_path or str(
        src_data if src_data.is_file() else Path("/app/data/islamic_dua_dataset_final.csv")
    )
    dua_sync_service = DuaSyncService(dua_data_path, dua_repository)
    dua_query_service = DuaQueryService(settings.cache, dua_repository, cache)

    # Wahda recommendation service (+ optional Gemini client for AI question generation)
    from .repositories.wahda_repository import WahdaRepository
    from .services.wahda_service import WahdaService
    wahda_repo = WahdaRepository(db)
    gemini_client = None
    if settings.gemini.api_key:
        from .clients.gemini_client import GeminiClient
        gemini_client = GeminiClient(settings.gemini)
    wahda_service = WahdaService(wahda_repo, gemini_client=gemini_client)

    bootstrap_service = BootstrapService(
        settings,
        db,
        sync_repository,
        quran_sync_service,
        hadith_sync_service,
        dua_sync_service,
        quran_query_service,
        hadith_query_service,
        dua_query_service,
        cache,
    )
    return Runtime(
        settings=settings,
        db=db,
        cache=cache,
        quran_client=quran_client,
        quran_user_client=quran_user_client,
        hadith_client=hadith_client,
        bootstrap_service=bootstrap_service,
        quran_query_service=quran_query_service,
        quran_user_service=quran_user_service,
        hadith_query_service=hadith_query_service,
        dua_query_service=dua_query_service,
        wahda_service=wahda_service,
        gemini_client=gemini_client,
    )


def create_app(settings: Settings | None = None, *, enable_runtime: bool = True) -> FastAPI:
    resolved_settings = settings or Settings()

    @asynccontextmanager
    async def lifespan(app: FastAPI):
        if not enable_runtime:
            yield
            return
        runtime = await build_runtime(resolved_settings)
        app.state.runtime = runtime
        app.state.settings = resolved_settings
        app.state.db = runtime.db
        app.state.cache = runtime.cache
        app.state.bootstrap_service = runtime.bootstrap_service
        app.state.quran_query_service = runtime.quran_query_service
        app.state.quran_user_service = runtime.quran_user_service
        app.state.hadith_query_service = runtime.hadith_query_service
        app.state.dua_query_service = runtime.dua_query_service
        app.state.wahda_service = runtime.wahda_service

        # Seed Wahda recommendation data (idempotent)
        if runtime.wahda_service:
            try:
                await runtime.wahda_service.seed_if_empty()
            except Exception as e:
                import logging
                logging.getLogger(__name__).warning("wahda_seed_failed: %s", e)

        if resolved_settings.bootstrap.on_start:
            await runtime.bootstrap_service.bootstrap(
                [
                    module_name
                    for module_name, enabled in (
                        ("quran", resolved_settings.modules.enable_quran),
                        ("hadith", resolved_settings.modules.enable_hadith),
                        ("dua", resolved_settings.modules.enable_dua),
                    )
                    if enabled
                ],
                skip_unconfigured=True,
            )
        yield
        await runtime.close()

    app = FastAPI(
        title="Islamic Content Service",
        version="1.0.0",
        description=(
            "Standalone Islamic content microservice providing Quran, Hadith, and Dua APIs.\n\n"
            "**Data sources:**\n"
            "- Quran: 114 chapters, 6,236 ayahs, 145 translations, 12 recitations (quran.foundation)\n"
            "- Hadith: 7 collections, 34,497 hadiths, 74,224 grades (fawazahmed0 CDN + HuggingFace)\n"
            "- Dua: 31 categories, 72 verified duas (local JSON)\n\n"
            "**Sheikh Wahda:** AI recommendation system with typeahead, trending, feedback, sharing, and personalized questions."
        ),
        openapi_tags=OPENAPI_TAGS,
        lifespan=lifespan,
        contact={"name": "Hejaz AI Team", "email": "tech@hejazfs.com.au"},
        license_info={"name": "Proprietary"},
    )

    from starlette.middleware.cors import CORSMiddleware

    app.add_middleware(
        CORSMiddleware,
        allow_origins=["http://localhost:8081", "http://localhost:3000"],
        allow_credentials=True,
        allow_methods=["*"],
        allow_headers=["*"],
    )

    @app.middleware("http")
    async def security_headers(request, call_next):
        response = await call_next(request)
        response.headers["X-Content-Type-Options"] = "nosniff"
        response.headers["X-Frame-Options"] = "DENY"
        response.headers["Strict-Transport-Security"] = "max-age=31536000; includeSubDomains"
        return response

    app.include_router(api_router)
    app.state.settings = resolved_settings
    return app


app = create_app()


def main() -> None:
    settings = Settings()
    uvicorn.run(
        "islamic_content_service.main:app",
        host=settings.app.host,
        port=settings.app.port,
        reload=False,
    )


if __name__ == "__main__":
    main()
