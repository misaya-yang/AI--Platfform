from __future__ import annotations

import pytest


def _chapter_audio_payload(chapter_id: int) -> dict:
    return {
        "chapter_id": chapter_id,
        "recitation_id": 7,
        "audio_url": "https://cdn.example/chapter1.mp3",
        "source_api": "quran.foundation",
        "timings": [
            {
                "verse_key": "1:1",
                "timestamp_from_ms": 0,
                "timestamp_to_ms": 6090,
                "duration_ms": 6090,
                "segments": [{"word_index": 1, "start_ms": 0, "end_ms": 580}],
            }
        ],
    }


def _ayah_payload(verse_key: str) -> dict:
    return {
        "verse_key": verse_key,
        "surah_number": 1,
        "ayah_number": 1,
        "arabic_text": "بِسْمِ",
        "transliteration_text": "bismi",
        "translation_text": "In the name",
        "words": [
            {
                "position": 1,
                "arabic": "بِسْمِ",
                "segment": {"word_index": 1, "start_ms": 0, "end_ms": 580},
            }
        ],
        "timing": {
            "verse_key": verse_key,
            "timestamp_from_ms": 0,
            "timestamp_to_ms": 6090,
            "duration_ms": 6090,
            "segments": [{"word_index": 1, "start_ms": 0, "end_ms": 580}],
        },
        "audio": {"recitation_id": 7, "translation_id": 20, "url": "https://cdn.example/1.mp3"},
    }


class StubQuranQueryService:
    async def get_chapters(self):
        return {
            "generated_at": "2026-03-09T00:00:00Z",
            "source_api": "quran.foundation",
            "chapters": [{"chapter_id": 1, "name_simple": "Al-Fatihah"}],
        }

    async def get_translations(self):
        return {
            "generated_at": "2026-03-09T00:00:00Z",
            "source_api": "quran.foundation",
            "translations": [{"id": 20, "name": "Saheeh International"}],
            "synced_translation_ids": [20, 84],
        }

    async def get_recitations(self):
        return {
            "generated_at": "2026-03-09T00:00:00Z",
            "source_api": "quran.foundation",
            "recitations": [{"id": 7, "reciter_name": "Mishary"}],
            "synced_recitation_ids": [1, 7],
        }

    async def get_chapter_ayahs(self, chapter_id: int, *, translation_id=None, recitation_id=None):
        return {
            "generated_at": "2026-03-09T00:00:00Z",
            "source_api": "quran.foundation",
            "chapter_id": chapter_id,
            "translation_id": translation_id or 20,
            "recitation_id": recitation_id or 7,
            "chapter_audio": _chapter_audio_payload(chapter_id),
            "ayahs": [_ayah_payload("1:1")],
        }

    async def get_chapter_audio_text(self, chapter_id: int, *, translation_id=None, recitation_id=None):
        payload = await self.get_chapter_ayahs(
            chapter_id,
            translation_id=translation_id,
            recitation_id=recitation_id,
        )
        return {"screen": "quran_audio_text", **payload}

    async def get_chapter_triplets(self, chapter_id: int, *, translation_id=None, recitation_id=None):
        return {
            "generated_at": "2026-03-09T00:00:00Z",
            "screen": "quran_triplets",
            "chapter_id": chapter_id,
            "translation_id": translation_id or 20,
            "recitation_id": recitation_id or 7,
            "chapter_audio": _chapter_audio_payload(chapter_id),
            "blocks": [],
        }

    async def get_ayah_detail(self, verse_key: str, *, translation_id=None, recitation_id=None):
        return {
            "generated_at": "2026-03-09T00:00:00Z",
            "screen": "quran_ayah_detail",
            "translation_id": translation_id or 20,
            "recitation_id": recitation_id or 7,
            "chapter_audio": _chapter_audio_payload(1),
            "ayah": _ayah_payload(verse_key),
        }

    async def get_ayah_minimal(self, verse_key: str, *, translation_id=None, recitation_id=None):
        ayah = _ayah_payload(verse_key)
        return {
            "generated_at": "2026-03-09T00:00:00Z",
            "source_api": "quran.foundation",
            "source_type": "quran",
            "verse_key": verse_key,
            "surah_number": ayah["surah_number"],
            "ayah_number": ayah["ayah_number"],
            "translation_id": translation_id or 20,
            "recitation_id": recitation_id or 7,
            "arabic_text": ayah["arabic_text"],
            "transliteration_text": ayah["transliteration_text"],
            "translation_text": ayah["translation_text"],
        }

    async def get_ayah_translation(self, verse_key: str, *, translation_id=None):
        return {
            "generated_at": "2026-03-09T00:00:00Z",
            "source_api": "quran.foundation",
            "translation_id": translation_id or 20,
            "item": {
                "verse_key": verse_key,
                "surah_number": 1,
                "ayah_number": 1,
                "translation_id": translation_id or 20,
                "translation_text": "In the name",
            },
        }

    async def get_chapter_translations(self, chapter_id: int, *, translation_id=None):
        return {
            "generated_at": "2026-03-09T00:00:00Z",
            "source_api": "quran.foundation",
            "chapter_id": chapter_id,
            "translation_id": translation_id or 20,
            "items": [],
        }

    async def get_chapter_audio(self, chapter_id: int, *, recitation_id=None):
        return {
            "generated_at": "2026-03-09T00:00:00Z",
            **{
                **_chapter_audio_payload(chapter_id),
                "recitation_id": recitation_id or 7,
            },
        }


@pytest.mark.asyncio
async def test_quran_endpoints(async_client, test_app):
    test_app.state.quran_query_service = StubQuranQueryService()

    chapters = await async_client.get("/api/v1/quran/chapters")
    translations = await async_client.get("/api/v1/quran/resources/translations")
    recitations = await async_client.get("/api/v1/quran/resources/recitations")
    audio_text = await async_client.get("/api/v1/quran/chapters/1/audio-text")
    audio_text_variant = await async_client.get(
        "/api/v1/quran/chapters/1/audio-text",
        params={"translation_id": 84, "recitation_id": 1},
    )
    ayah = await async_client.get("/api/v1/quran/ayahs/1:1")
    ayah_minimal = await async_client.get("/api/v1/quran/ayahs/1:1/minimal")
    translation = await async_client.get("/api/v1/quran/ayahs/1:1/translation")
    audio = await async_client.get("/api/v1/quran/chapters/1/audio")

    assert chapters.status_code == 200
    assert chapters.json()["chapters"][0]["chapter_id"] == 1
    assert translations.json()["synced_translation_ids"] == [20, 84]
    assert recitations.json()["synced_recitation_ids"] == [1, 7]
    assert audio_text.json()["chapter_audio"]["audio_url"] == "https://cdn.example/chapter1.mp3"
    assert audio_text_variant.json()["translation_id"] == 84
    assert audio_text_variant.json()["recitation_id"] == 1
    assert audio_text.json()["ayahs"][0]["words"][0]["segment"]["word_index"] == 1
    assert ayah.json()["ayah"]["verse_key"] == "1:1"
    assert ayah.json()["ayah"]["timing"]["timestamp_to_ms"] == 6090
    assert ayah_minimal.json()["verse_key"] == "1:1"
    assert "chapter_audio" not in ayah_minimal.json()
    assert ayah_minimal.json()["transliteration_text"] == "bismi"
    assert translation.json()["item"]["translation_id"] == 20
    assert audio.json()["recitation_id"] == 7
    assert audio.json()["timings"][0]["segments"][0]["word_index"] == 1


@pytest.mark.asyncio
async def test_quran_openapi_examples_present(async_client, test_app):
    test_app.state.quran_query_service = StubQuranQueryService()

    schema = (await async_client.get("/openapi.json")).json()
    audio_text_example = (
        schema["paths"]["/api/v1/quran/chapters/{chapter_id}/audio-text"]["get"]["responses"]["200"]["content"]["application/json"]["example"]
    )
    ayah_detail_example = (
        schema["paths"]["/api/v1/quran/ayahs/{verse_key}"]["get"]["responses"]["200"]["content"]["application/json"]["example"]
    )
    ayah_minimal_example = (
        schema["paths"]["/api/v1/quran/ayahs/{verse_key}/minimal"]["get"]["responses"]["200"]["content"]["application/json"]["example"]
    )

    assert audio_text_example["screen"] == "quran_audio_text"
    assert audio_text_example["chapter_audio"]["timings"][0]["segments"][0]["word_index"] == 1
    assert ayah_detail_example["screen"] == "quran_ayah_detail"
    assert ayah_detail_example["ayah"]["timing"]["timestamp_to_ms"] == 6090
    assert ayah_minimal_example["verse_key"] == "1:1"
    assert ayah_minimal_example["translation_text"] == "In the name of Allāh, the Entirely Merciful, the Especially Merciful."
