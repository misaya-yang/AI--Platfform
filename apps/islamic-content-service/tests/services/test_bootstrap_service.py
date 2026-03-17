from __future__ import annotations

from unittest.mock import AsyncMock

import pytest

from islamic_content_service.config import Settings
from islamic_content_service.services.bootstrap_service import BootstrapService


def _make_service(
    *,
    counts: dict,
    quran_configured: bool = True,
    hadith_configured: bool = True,
    cache_enabled: bool = False,
    settings: Settings | None = None,
) -> BootstrapService:
    sync_repository = AsyncMock()
    sync_repository.get_counts = AsyncMock(return_value=counts)
    sync_repository.get_latest_completed_at = AsyncMock(return_value="2026-03-09T00:00:00Z")
    db = AsyncMock()
    db.ping = AsyncMock(return_value=True)
    cache = AsyncMock(enabled=cache_enabled)
    if cache_enabled:
        cache.ping = AsyncMock(return_value=True)
    return BootstrapService(
        settings or Settings(),
        db=db,
        sync_repository=sync_repository,
        quran_sync_service=AsyncMock(is_configured=lambda: quran_configured),
        hadith_sync_service=AsyncMock(is_configured=lambda: hadith_configured),
        dua_sync_service=AsyncMock(is_configured=lambda: False),
        quran_query_service=AsyncMock(),
        hadith_query_service=AsyncMock(),
        dua_query_service=AsyncMock(),
        cache=cache,
    )


@pytest.mark.asyncio
async def test_readiness_marks_modules_ready_when_synced():
    service = _make_service(
        counts={"quran_ayahs": 10, "quran_audio_timings": 10, "hadith_items": 5},
        cache_enabled=True,
    )

    payload = await service.get_readiness()

    assert payload["status"] == "ready"
    assert payload["modules"]["quran"]["status"] == "ready"
    assert payload["modules"]["hadith"]["status"] == "ready"
    assert payload["backends"]["database"] == "ready"


@pytest.mark.asyncio
async def test_readiness_marks_quran_partial_when_audio_timings_missing():
    service = _make_service(
        counts={"quran_ayahs": 10, "quran_audio_timings": 0, "hadith_items": 5},
    )

    payload = await service.get_readiness()

    assert payload["status"] == "not_ready"
    assert payload["modules"]["quran"]["status"] == "partial_data"


@pytest.mark.asyncio
async def test_readiness_ignores_disabled_hadith_module():
    settings = Settings()
    settings.modules.enable_hadith = False
    service = _make_service(
        counts={"quran_ayahs": 10, "quran_audio_timings": 10, "hadith_items": 0},
        hadith_configured=False,
        settings=settings,
    )

    payload = await service.get_readiness()

    assert payload["status"] == "ready"
    assert payload["modules"]["hadith"]["status"] == "disabled"


@pytest.mark.asyncio
async def test_readiness_ready_when_quran_has_data_but_no_credentials():
    service = _make_service(
        counts={"quran_ayahs": 6236, "quran_audio_timings": 6236, "hadith_items": 0},
        quran_configured=False,
        hadith_configured=False,
    )

    payload = await service.get_readiness()

    assert payload["status"] == "ready"
    assert payload["modules"]["quran"]["status"] == "ready"
    assert payload["modules"]["quran"]["configured"] is False
    assert payload["modules"]["hadith"]["status"] == "missing_credentials"
