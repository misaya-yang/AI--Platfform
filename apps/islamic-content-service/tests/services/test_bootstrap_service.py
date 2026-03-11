from __future__ import annotations

from unittest.mock import AsyncMock

import pytest

from islamic_content_service.config import Settings
from islamic_content_service.services.bootstrap_service import BootstrapService


@pytest.mark.asyncio
async def test_readiness_marks_modules_ready_when_synced():
    sync_repository = AsyncMock()
    sync_repository.get_counts = AsyncMock(
        return_value={"quran_ayahs": 10, "quran_audio_timings": 10, "hadith_items": 5}
    )
    sync_repository.get_latest_completed_at = AsyncMock(return_value="2026-03-09T00:00:00Z")
    db = AsyncMock()
    db.ping = AsyncMock(return_value=True)
    cache = AsyncMock(enabled=True)
    cache.ping = AsyncMock(return_value=True)
    service = BootstrapService(
        Settings(),
        db=db,
        sync_repository=sync_repository,
        quran_sync_service=AsyncMock(is_configured=lambda: True),
        hadith_sync_service=AsyncMock(is_configured=lambda: True),
        quran_query_service=AsyncMock(),
        hadith_query_service=AsyncMock(),
        cache=cache,
    )

    payload = await service.get_readiness()

    assert payload["status"] == "ready"
    assert payload["modules"]["quran"]["status"] == "ready"
    assert payload["modules"]["hadith"]["status"] == "ready"
    assert payload["backends"]["database"] == "ready"


@pytest.mark.asyncio
async def test_readiness_marks_quran_partial_when_audio_timings_missing():
    sync_repository = AsyncMock()
    sync_repository.get_counts = AsyncMock(
        return_value={"quran_ayahs": 10, "quran_audio_timings": 0, "hadith_items": 5}
    )
    sync_repository.get_latest_completed_at = AsyncMock(return_value="2026-03-09T00:00:00Z")
    db = AsyncMock()
    db.ping = AsyncMock(return_value=True)
    cache = AsyncMock(enabled=False)
    service = BootstrapService(
        Settings(),
        db=db,
        sync_repository=sync_repository,
        quran_sync_service=AsyncMock(is_configured=lambda: True),
        hadith_sync_service=AsyncMock(is_configured=lambda: True),
        quran_query_service=AsyncMock(),
        hadith_query_service=AsyncMock(),
        cache=cache,
    )

    payload = await service.get_readiness()

    assert payload["status"] == "not_ready"
    assert payload["modules"]["quran"]["status"] == "partial_data"


@pytest.mark.asyncio
async def test_readiness_ignores_disabled_hadith_module():
    settings = Settings()
    settings.modules.enable_hadith = False
    sync_repository = AsyncMock()
    sync_repository.get_counts = AsyncMock(
        return_value={"quran_ayahs": 10, "quran_audio_timings": 10, "hadith_items": 0}
    )
    sync_repository.get_latest_completed_at = AsyncMock(return_value="2026-03-09T00:00:00Z")
    db = AsyncMock()
    db.ping = AsyncMock(return_value=True)
    cache = AsyncMock(enabled=False)
    service = BootstrapService(
        settings,
        db=db,
        sync_repository=sync_repository,
        quran_sync_service=AsyncMock(is_configured=lambda: True),
        hadith_sync_service=AsyncMock(is_configured=lambda: False),
        quran_query_service=AsyncMock(),
        hadith_query_service=AsyncMock(),
        cache=cache,
    )

    payload = await service.get_readiness()

    assert payload["status"] == "ready"
    assert payload["modules"]["hadith"]["status"] == "disabled"
