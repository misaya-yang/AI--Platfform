from __future__ import annotations

import pytest


class StubIslamicContentService:
    async def get_canonical_summary(self):
        return {
            "database_enabled": True,
            "generated_at": "2026-03-09T00:00:00Z",
            "counts": {"quran_chapters": 114, "quran_ayahs": 6236},
        }

    async def get_manifest(self):
        return {
            "generated_at": "2026-03-09T00:00:00Z",
            "cache_dir": "/tmp/islamic",
            "steps": [{"name": "quran", "status": "ok"}],
        }

    async def get_quran_home(self, *, continue_verse_key=None, use_cache=True):
        return {
            "screen": "quran_home",
            "version": "v1",
            "generated_at": "2026-03-09T00:00:00Z",
            "header": {"title": "Quran", "tabs": ["surah"], "default_tab": "surah"},
            "continue_reading": None,
            "chapters": [
                {
                    "chapter_id": 1,
                    "name_simple": "Al-Fatihah",
                    "name_complex": "Al-Fatihah",
                    "name_arabic": "الفاتحة",
                    "translated_name": "The Opener",
                    "revelation_place": "makkah",
                    "verses_count": 7,
                }
            ],
        }

    async def get_quran_chapters(self, *, use_cache=True):
        return {
            "generated_at": "2026-03-09T00:00:00Z",
            "source_api": "quran.foundation",
            "chapters": [
                {
                    "chapter_id": 1,
                    "name_simple": "Al-Fatihah",
                    "name_complex": "Al-Fatihah",
                    "name_arabic": "الفاتحة",
                    "translated_name": "The Opener",
                    "revelation_place": "makkah",
                    "verses_count": 7,
                }
            ],
        }

    async def get_quran_chapter_translations(self, chapter_id, *, translation_id=None, use_cache=True):
        return {
            "generated_at": "2026-03-09T00:00:00Z",
            "source_api": "quran.foundation",
            "chapter_id": chapter_id,
            "translation_id": translation_id or 20,
            "items": [
                {
                    "verse_key": "2:1",
                    "surah_number": 2,
                    "ayah_number": 1,
                    "translation_id": translation_id or 20,
                    "translation_text": "Alif, Lam, Meem.",
                }
            ],
        }

    async def get_quran_ayah_translation(self, verse_key, *, translation_id=None, use_cache=True):
        return {
            "generated_at": "2026-03-09T00:00:00Z",
            "source_api": "quran.foundation",
            "translation_id": translation_id or 20,
            "item": {
                "verse_key": verse_key,
                "surah_number": 2,
                "ayah_number": 255,
                "translation_id": translation_id or 20,
                "translation_text": "Allah - there is no deity except Him...",
            },
        }

    async def get_hadith_collections(self, *, use_cache=True):
        return {
            "generated_at": "2026-03-09T00:00:00Z",
            "screen": "hadith_collections",
            "source_api": "sunnah",
            "collections": [
                {
                    "name": "bukhari",
                    "title": "Sahih al-Bukhari",
                    "short_intro": "Intro",
                    "has_books": True,
                    "has_chapters": True,
                    "total_books": 97,
                    "total_hadith": 7563,
                }
            ],
        }

    async def get_qibla(self, *, latitude, longitude):
        return {
            "screen": "qiblah_home",
            "generated_at": "2026-03-09T00:00:00Z",
            "source_api": "aladhan",
            "location": {"latitude": latitude, "longitude": longitude},
            "qiblah_bearing": 284.0,
            "meta": {"direction": 284.0},
        }


@pytest.mark.asyncio
async def test_quran_home_endpoint(async_client, test_app):
    test_app.state.islamic_content_service = StubIslamicContentService()

    response = await async_client.get("/api/v1/islamic/quran/home")

    assert response.status_code == 200
    payload = response.json()
    assert payload["screen"] == "quran_home"
    assert payload["chapters"][0]["chapter_id"] == 1


@pytest.mark.asyncio
async def test_quran_chapters_endpoint(async_client, test_app):
    test_app.state.islamic_content_service = StubIslamicContentService()

    response = await async_client.get("/api/v1/islamic/quran/chapters")

    assert response.status_code == 200
    payload = response.json()
    assert payload["chapters"][0]["name_simple"] == "Al-Fatihah"


@pytest.mark.asyncio
async def test_quran_chapter_translations_endpoint(async_client, test_app):
    test_app.state.islamic_content_service = StubIslamicContentService()

    response = await async_client.get("/api/v1/islamic/quran/chapters/2/translations")

    assert response.status_code == 200
    payload = response.json()
    assert payload["items"][0]["translation_text"] == "Alif, Lam, Meem."


@pytest.mark.asyncio
async def test_quran_ayah_translation_endpoint(async_client, test_app):
    test_app.state.islamic_content_service = StubIslamicContentService()

    response = await async_client.get("/api/v1/islamic/quran/ayahs/2:255/translation")

    assert response.status_code == 200
    payload = response.json()
    assert payload["item"]["verse_key"] == "2:255"


@pytest.mark.asyncio
async def test_hadith_collections_endpoint(async_client, test_app):
    test_app.state.islamic_content_service = StubIslamicContentService()

    response = await async_client.get("/api/v1/islamic/hadith/collections")

    assert response.status_code == 200
    payload = response.json()
    assert payload["collections"][0]["name"] == "bukhari"


@pytest.mark.asyncio
async def test_manifest_endpoint(async_client, test_app):
    test_app.state.islamic_content_service = StubIslamicContentService()

    response = await async_client.get("/api/v1/islamic/manifest")

    assert response.status_code == 200
    assert response.json()["steps"][0]["name"] == "quran"


@pytest.mark.asyncio
async def test_canonical_summary_endpoint(async_client, test_app):
    test_app.state.islamic_content_service = StubIslamicContentService()

    response = await async_client.get("/api/v1/islamic/canonical/summary")

    assert response.status_code == 200
    payload = response.json()
    assert payload["database_enabled"] is True
    assert payload["counts"]["quran_chapters"] == 114


@pytest.mark.asyncio
async def test_qibla_endpoint(async_client, test_app):
    test_app.state.islamic_content_service = StubIslamicContentService()

    response = await async_client.get("/api/v1/islamic/qibla?latitude=22.3&longitude=114.2")

    assert response.status_code == 200
    payload = response.json()
    assert payload["qiblah_bearing"] == 284.0
