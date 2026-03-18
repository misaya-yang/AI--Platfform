from __future__ import annotations

from datetime import datetime, timezone
from typing import Any

import logging

from ..cache import RedisCache
from ..config import Settings
from ..db import Database
from ..domain.constants import SCHEMA_VERSION
from ..repositories.sync_repository import SyncRepository

logger = logging.getLogger(__name__)
from .dua_query_service import DuaQueryService
from .dua_sync_service import DuaSyncService
from .hadith_query_service import HadithQueryService
from .hadith_sync_service import HadithSyncService
from .quran_query_service import QuranQueryService
from .quran_sync_service import QuranSyncService


class BootstrapService:
    def __init__(
        self,
        settings: Settings,
        db: Database,
        sync_repository: SyncRepository,
        quran_sync_service: QuranSyncService,
        hadith_sync_service: HadithSyncService,
        dua_sync_service: DuaSyncService,
        quran_query_service: QuranQueryService,
        hadith_query_service: HadithQueryService,
        dua_query_service: DuaQueryService,
        cache: RedisCache,
    ) -> None:
        self.settings = settings
        self.db = db
        self.sync_repository = sync_repository
        self.quran_sync_service = quran_sync_service
        self.hadith_sync_service = hadith_sync_service
        self.dua_sync_service = dua_sync_service
        self.quran_query_service = quran_query_service
        self.hadith_query_service = hadith_query_service
        self.dua_query_service = dua_query_service
        self.cache = cache

    async def _module_status(
        self,
        *,
        source_name: str,
        configured: bool,
        primary_count: int,
        aux_count: int | None = None,
    ) -> dict[str, Any]:
        latest_completed_at = await self.sync_repository.get_latest_completed_at(source_name)
        status = "missing_credentials"
        if primary_count > 0 and aux_count is not None and aux_count == 0:
            status = "partial_data"
        elif primary_count > 0:
            status = "ready"
        elif configured:
            status = "empty_db"
        counts = {"primary": primary_count}
        if aux_count is not None:
            counts["auxiliary"] = aux_count
        return {
            "status": status,
            "configured": configured,
            "latest_completed_at": latest_completed_at,
            "counts": counts,
        }

    async def bootstrap(
        self, sources: list[str], *, skip_unconfigured: bool = False,
    ) -> dict[str, Any]:
        steps = []
        if "quran" in sources and self.settings.modules.enable_quran:
            if skip_unconfigured and not self.quran_sync_service.is_configured():
                logger.info("Skipping quran sync (not configured)")
                steps.append({"name": "quran", "status": "skipped"})
            else:
                await self._sync_quran(steps)
        if "hadith" in sources and self.settings.modules.enable_hadith:
            if skip_unconfigured and not self.hadith_sync_service.is_configured():
                logger.info("Skipping hadith sync (not configured)")
                steps.append({"name": "hadith", "status": "skipped"})
            else:
                await self._sync_hadith(steps)
        if "dua" in sources and self.settings.modules.enable_dua:
            if skip_unconfigured and not self.dua_sync_service.is_configured():
                logger.info("Skipping dua sync (data file not found)")
                steps.append({"name": "dua", "status": "skipped"})
            else:
                await self._sync_dua(steps)
        return {
            "generated_at": datetime.now(timezone.utc).isoformat(),
            "schema_version": SCHEMA_VERSION,
            "steps": steps,
        }

    async def _sync_quran(self, steps: list[dict[str, Any]]) -> None:
        run_id = await self.sync_repository.start_sync_run(
            "quran",
            {
                "default_translation_id": self.settings.quran.default_translation_id,
                "default_recitation_id": self.settings.quran.default_recitation_id,
                "sync_all_translations": self.settings.quran.sync_all_translations,
                "sync_all_recitations": self.settings.quran.sync_all_recitations,
                "translation_ids": self.settings.quran.translation_ids,
                "recitation_ids": self.settings.quran.recitation_ids,
            },
        )
        try:
            metrics = await self.quran_sync_service.sync()
            await self.sync_repository.finish_sync_run(run_id, status="completed", metrics=metrics)
            await self.quran_query_service.invalidate()
            steps.append({"name": "quran", "status": "completed", **metrics})
        except Exception as exc:
            await self.sync_repository.finish_sync_run(
                run_id, status="failed", metrics={}, error_summary=str(exc),
            )
            raise

    async def _sync_hadith(self, steps: list[dict[str, Any]]) -> None:
        run_id = await self.sync_repository.start_sync_run(
            "hadith", {"collections": self.settings.hadith.sync_collections},
        )
        try:
            metrics = await self.hadith_sync_service.sync()
            await self.sync_repository.finish_sync_run(run_id, status="completed", metrics=metrics)
            await self.hadith_query_service.invalidate()
            steps.append({"name": "hadith", "status": "completed", **metrics})
        except Exception as exc:
            await self.sync_repository.finish_sync_run(
                run_id, status="failed", metrics={}, error_summary=str(exc),
            )
            raise

    async def _sync_dua(self, steps: list[dict[str, Any]]) -> None:
        run_id = await self.sync_repository.start_sync_run(
            "dua", {"data_path": self.dua_sync_service.data_path},
        )
        try:
            metrics = await self.dua_sync_service.sync()
            await self.sync_repository.finish_sync_run(run_id, status="completed", metrics=metrics)
            await self.dua_query_service.invalidate()
            steps.append({"name": "dua", "status": "completed", **metrics})
        except Exception as exc:
            await self.sync_repository.finish_sync_run(
                run_id, status="failed", metrics={}, error_summary=str(exc),
            )
            raise

    async def get_manifest(self) -> dict[str, Any]:
        manifest = await self.sync_repository.build_manifest()
        manifest["schema_version"] = SCHEMA_VERSION
        return manifest

    async def get_canonical_summary(self) -> dict[str, Any]:
        return {
            "database_enabled": True,
            "generated_at": datetime.now(timezone.utc).isoformat(),
            "counts": await self.sync_repository.get_counts(),
        }

    async def get_readiness(self) -> dict[str, Any]:
        counts = await self.sync_repository.get_counts()
        modules = {}
        quran_count = counts.get("quran_ayahs", 0)
        quran_audio_timing_count = counts.get("quran_audio_timings", 0)
        quran_translation_count = counts.get("quran_ayah_translations", quran_count)
        quran_audio_track_count = counts.get("quran_ayah_audio", quran_count)
        hadith_count = counts.get("hadith_items", 0)
        dua_count = counts.get("dua_items", 0)

        if self.settings.modules.enable_quran:
            modules["quran"] = await self._module_status(
                source_name="quran",
                configured=self.quran_sync_service.is_configured(),
                primary_count=quran_count,
                aux_count=min(quran_audio_timing_count, quran_translation_count, quran_audio_track_count),
            )
        else:
            modules["quran"] = {
                "status": "disabled",
                "configured": False,
                "latest_completed_at": None,
                "counts": {
                    "primary": quran_count,
                    "auxiliary": min(quran_audio_timing_count, quran_translation_count, quran_audio_track_count),
                },
            }

        if self.settings.modules.enable_hadith:
            modules["hadith"] = await self._module_status(
                source_name="hadith",
                configured=self.hadith_sync_service.is_configured(),
                primary_count=hadith_count,
            )
        else:
            modules["hadith"] = {
                "status": "disabled",
                "configured": False,
                "latest_completed_at": None,
                "counts": {"primary": hadith_count},
            }

        if self.settings.modules.enable_dua:
            modules["dua"] = await self._module_status(
                source_name="dua",
                configured=self.dua_sync_service.is_configured(),
                primary_count=dua_count,
            )
        else:
            modules["dua"] = {
                "status": "disabled",
                "configured": False,
                "latest_completed_at": None,
                "counts": {"primary": dua_count},
            }

        db_ready = await self.db.ping()
        cache_ready = not self.cache.enabled or await self.cache.ping()
        blocking_statuses = [
            item["status"]
            for item in modules.values()
            if item["status"] not in ("disabled", "missing_credentials")
        ]
        healthy = db_ready and cache_ready and all(
            status == "ready" for status in blocking_statuses
        )
        return {
            "status": "ready" if healthy else "not_ready",
            "schema_version": SCHEMA_VERSION,
            "modules": modules,
            "counts": counts,
            "backends": {
                "database": "ready" if db_ready else "not_ready",
                "cache": "ready" if cache_ready else "not_ready",
            },
            "generated_at": datetime.now(timezone.utc).isoformat(),
        }

    async def get_config_payload(self) -> dict[str, Any]:
        latest_quran = await self.sync_repository.get_latest_completed_at("quran")
        latest_hadith = await self.sync_repository.get_latest_completed_at("hadith")
        synced_translation_ids = await self.quran_query_service.repository.get_synced_translation_ids()
        synced_recitation_ids = await self.quran_query_service.repository.get_synced_recitation_ids()
        return {
            "service_name": "islamic-content-service",
            "schema_version": SCHEMA_VERSION,
            "generated_at": datetime.now(timezone.utc).isoformat(),
            "database_schema": self.settings.database.schema,
            "default_translation_id": self.settings.quran.default_translation_id,
            "default_recitation_id": self.settings.quran.default_recitation_id,
            "sync_all_translations": self.settings.quran.sync_all_translations,
            "sync_all_recitations": self.settings.quran.sync_all_recitations,
            "synced_translation_ids": synced_translation_ids,
            "synced_recitation_ids": synced_recitation_ids,
            "enabled_modules": [
                module_name
                for module_name, enabled in (
                    ("quran", self.settings.modules.enable_quran),
                    ("hadith", self.settings.modules.enable_hadith),
                )
                if enabled
            ],
            "sync_status": {
                "quran": latest_quran,
                "hadith": latest_hadith,
            },
            "cache": {"enabled": self.cache.enabled},
            "quran_user": {
                "enabled": self.settings.quran_user.enabled,
                "configured": bool(
                    self.settings.quran_user.client_id.strip()
                    and self.settings.quran_user.auth_url.strip()
                ),
                "auth_url": self.settings.quran_user.auth_url,
                "api_base_url": self.settings.quran_user.user_api_base_url,
                "scopes": self.settings.quran_user.scopes,
            },
            "public_endpoints": {
                "quran_audio_text": "/api/v1/quran/chapters/{chapter_id}/audio-text",
                "quran_ayah_detail": "/api/v1/quran/ayahs/{verse_key}",
                "quran_user_auth_url": "/api/v1/quran/user/auth/authorize-url",
                "quran_user_proxy": "/api/v1/quran/user/request",
                "hadith_detail": "/api/v1/hadith/collections/{collection_name}/hadiths/{hadith_number}",
            },
            "module_features": {
                "quran": [
                    "text",
                    "all_translations",
                    "all_recitations",
                    "chapter_audio",
                    "verse_timing",
                    "word_segments",
                    "user_oauth_proxy",
                ],
                "hadith": ["collection", "book", "list", "detail"],
            },
        }
