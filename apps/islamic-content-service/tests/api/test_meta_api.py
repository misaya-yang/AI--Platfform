from __future__ import annotations

import pytest


class StubBootstrapService:
    async def get_readiness(self):
        return {
            "status": "ready",
            "schema_version": "001_init_schema",
            "generated_at": "2026-03-09T00:00:00Z",
            "modules": {
                "quran": {"status": "ready", "configured": True},
                "hadith": {"status": "ready", "configured": True},
            },
            "counts": {"quran_ayahs": 6236, "quran_audio_timings": 6236, "hadith_items": 7563},
            "backends": {"database": "ready", "cache": "ready"},
        }

    async def get_config_payload(self):
        return {
            "service_name": "islamic-content-service",
            "schema_version": "001_init_schema",
            "generated_at": "2026-03-09T00:00:00Z",
            "database_schema": "islamic_content",
            "default_translation_id": 20,
            "default_recitation_id": 7,
            "sync_all_translations": True,
            "sync_all_recitations": True,
            "synced_translation_ids": [20, 84],
            "synced_recitation_ids": [1, 7],
            "enabled_modules": ["quran", "hadith"],
            "sync_status": {"quran": "2026-03-09T00:00:00Z", "hadith": None},
            "cache": {"enabled": True},
            "quran_user": {"enabled": False, "configured": False, "scopes": []},
            "public_endpoints": {
                "quran_audio_text": "/api/v1/quran/chapters/{chapter_id}/audio-text",
                "quran_user_auth_url": "/api/v1/quran/user/auth/authorize-url",
            },
            "module_features": {
                "quran": ["text", "translation", "chapter_audio", "verse_timing", "word_segments"],
                "hadith": ["collection", "book", "list", "detail"],
            },
        }

    async def get_manifest(self):
        return {
            "generated_at": "2026-03-09T00:00:00Z",
            "schema_version": "001_init_schema",
            "counts": {"quran_ayahs": 6236},
            "latest_runs": [{"source_name": "quran", "status": "completed"}],
        }

    async def get_canonical_summary(self):
        return {
            "database_enabled": True,
            "generated_at": "2026-03-09T00:00:00Z",
            "counts": {"quran_ayahs": 6236},
        }


@pytest.mark.asyncio
async def test_meta_endpoints(async_client, test_app):
    test_app.state.bootstrap_service = StubBootstrapService()

    ready = await async_client.get("/health/ready")
    config = await async_client.get("/api/v1/meta/config")
    manifest = await async_client.get("/api/v1/meta/manifest")
    summary = await async_client.get("/api/v1/meta/canonical-summary")

    assert ready.status_code == 200
    assert config.json()["default_translation_id"] == 20
    assert config.json()["sync_all_translations"] is True
    assert config.json()["synced_translation_ids"] == [20, 84]
    assert config.json()["public_endpoints"]["quran_audio_text"].endswith("/audio-text")
    assert manifest.json()["counts"]["quran_ayahs"] == 6236
    assert summary.json()["database_enabled"] is True
