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

    async def get_juzs(self):
        return {
            "generated_at": "2026-04-23T00:00:00Z",
            "screen": "quran_juzs",
            "source_api": "quran.foundation+internal",
            "juzs": [
                {
                    "juz_number": 1,
                    "name_arabic": "الم",
                    "name_simple": "Alif Lam Mim",
                    "name_transliteration": "Alif Lām Mīm",
                    "first_verse_key": "1:1",
                    "last_verse_key": "2:141",
                    "start_chapter_id": 1,
                    "start_chapter_name_simple": "Al-Fatihah",
                    "start_chapter_name_arabic": "ٱلْفَاتِحَة",
                    "start_ayah_number": 1,
                    "verses_count": 148,
                    "verse_mapping": {"1": "1-7", "2": "1-141"},
                }
            ],
        }

    async def get_juz_ayahs(self, juz_number: int, *, translation_id=None, recitation_id=None):
        return {
            "generated_at": "2026-04-23T00:00:00Z",
            "screen": "quran_juz_ayahs",
            "source_api": "quran.foundation+internal",
            "juz": {
                "juz_number": juz_number,
                "name_arabic": "الم",
                "name_simple": "Alif Lam Mim",
                "name_transliteration": "Alif Lām Mīm",
                "first_verse_key": "1:1",
                "last_verse_key": "2:141",
                "start_chapter_id": 1,
                "start_chapter_name_simple": "Al-Fatihah",
                "start_chapter_name_arabic": "ٱلْفَاتِحَة",
                "start_ayah_number": 1,
                "verses_count": 148,
                "verse_mapping": {"1": "1-7", "2": "1-141"},
            },
            "translation_id": translation_id or 20,
            "recitation_id": recitation_id or 7,
            "ayahs": [_ayah_payload("1:1")],
        }

    async def get_chapter_detail(self, chapter_id: int):
        return {
            "generated_at": "2026-04-23T00:00:00Z",
            "screen": "quran_chapter_detail",
            "source_api": "quran.foundation",
            "chapter": {"chapter_id": chapter_id, "name_simple": "Al-Fatihah"},
        }

    async def get_random_ayah(self, *, translation_id=None, recitation_id=None):
        return {
            "generated_at": "2026-04-23T00:00:00Z",
            "screen": "quran_random_ayah",
            "source_api": "quran.foundation",
            "translation_id": translation_id or 20,
            "recitation_id": recitation_id or 7,
            "ayah": _ayah_payload("1:1"),
        }

    async def get_page_ayahs(self, page_number: int, *, translation_id=None, recitation_id=None):
        return {
            "generated_at": "2026-04-23T00:00:00Z",
            "screen": "quran_page_ayahs",
            "source_api": "quran.foundation",
            "page_number": page_number,
            "translation_id": translation_id or 20,
            "recitation_id": recitation_id or 7,
            "ayahs": [_ayah_payload("1:1")],
        }

    async def get_ayahs_range(self, from_key, to_key, *, translation_id=None, recitation_id=None):
        return {
            "generated_at": "2026-04-23T00:00:00Z",
            "screen": "quran_ayahs_range",
            "source_api": "quran.foundation",
            "from_verse_key": from_key,
            "to_verse_key": to_key,
            "translation_id": translation_id or 20,
            "recitation_id": recitation_id or 7,
            "ayahs": [_ayah_payload("1:1")],
        }

    async def get_sajdahs(self, *, translation_id=None):
        return {
            "generated_at": "2026-04-23T00:00:00Z",
            "screen": "quran_sajdahs",
            "source_api": "quran.foundation+internal",
            "translation_id": translation_id or 20,
            "total": 15,
            "sajdahs": [{
                "sajdah_number": 1,
                "verse_key": "7:206",
                "surah_number": 7,
                "ayah_number": 206,
                "sajdah_type": "recommended",
                "chapter_name_simple": "Al-A'raf",
                "chapter_name_arabic": "ٱلأَعْرَاف",
                "arabic_text": "إِنَّ الَّذِينَ عِندَ رَبِّكَ",
                "translation_text": "Indeed, those near your Lord...",
            }],
        }

    async def get_hizbs(self):
        return {
            "generated_at": "2026-04-23T00:00:00Z",
            "screen": "quran_hizbs",
            "source_api": "quran.foundation+internal",
            "hizbs": [{
                "hizb_number": 1,
                "juz_number": 1,
                "first_verse_key": "1:1",
                "last_verse_key": "2:74",
                "start_chapter_id": 1,
                "start_chapter_name_simple": "Al-Fatihah",
                "start_chapter_name_arabic": "ٱلْفَاتِحَة",
                "start_ayah_number": 1,
                "verses_count": 81,
            }],
        }

    async def search_ayahs(self, query: str, *, translation_id=None, limit=20, offset=0):
        return {
            "generated_at": "2026-04-23T00:00:00Z",
            "screen": "quran_search",
            "source_api": "quran.foundation",
            "query": query,
            "translation_id": translation_id or 20,
            "total": 1,
            "limit": limit,
            "offset": offset,
            "items": [{
                "verse_key": "1:1",
                "surah_number": 1,
                "ayah_number": 1,
                "chapter_name_simple": "Al-Fatihah",
                "chapter_name_arabic": "ٱلْفَاتِحَة",
                "arabic_text": "بِسْمِ اللَّهِ",
                "translation_text": "In the name of Allah",
                "match_field": "translation",
            }],
        }

    async def get_juz_detail(self, juz_number: int):
        return {
            "generated_at": "2026-04-23T00:00:00Z",
            "screen": "quran_juz_detail",
            "source_api": "quran.foundation+internal",
            "juz": {
                "juz_number": juz_number,
                "name_arabic": "الم",
                "name_simple": "Alif Lam Mim",
                "name_transliteration": "Alif Lām Mīm",
                "first_verse_key": "1:1",
                "last_verse_key": "2:141",
                "start_chapter_id": 1,
                "start_chapter_name_simple": "Al-Fatihah",
                "start_chapter_name_arabic": "ٱلْفَاتِحَة",
                "start_ayah_number": 1,
                "verses_count": 148,
                "verse_mapping": {"1": "1-7", "2": "1-141"},
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
    juzs = await async_client.get("/api/v1/quran/juzs")
    juz_detail = await async_client.get("/api/v1/quran/juzs/1")
    juz_ayahs = await async_client.get("/api/v1/quran/juzs/1/ayahs")
    search = await async_client.get("/api/v1/quran/search", params={"q": "Allah"})
    chapter_detail = await async_client.get("/api/v1/quran/chapters/1")
    random_ayah = await async_client.get("/api/v1/quran/ayahs/random")
    ayahs_range = await async_client.get("/api/v1/quran/ayahs/range", params={"from": "1:1", "to": "1:5"})
    page = await async_client.get("/api/v1/quran/pages/1")
    sajdahs = await async_client.get("/api/v1/quran/sajdahs")
    hizbs = await async_client.get("/api/v1/quran/hizbs")

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
    assert juzs.status_code == 200
    juz_body = juzs.json()
    assert juz_body["screen"] == "quran_juzs"
    assert juz_body["juzs"][0]["juz_number"] == 1
    assert juz_body["juzs"][0]["name_arabic"] == "الم"
    assert juz_body["juzs"][0]["first_verse_key"] == "1:1"
    assert juz_body["juzs"][0]["verse_mapping"]["2"] == "1-141"
    assert juz_detail.status_code == 200
    assert juz_detail.json()["juz"]["juz_number"] == 1
    assert juz_detail.json()["juz"]["name_simple"] == "Alif Lam Mim"
    assert juz_ayahs.status_code == 200
    assert juz_ayahs.json()["juz"]["juz_number"] == 1
    assert juz_ayahs.json()["ayahs"][0]["verse_key"] == "1:1"
    assert search.status_code == 200
    assert search.json()["query"] == "Allah"
    assert search.json()["items"][0]["match_field"] == "translation"
    assert chapter_detail.status_code == 200
    assert chapter_detail.json()["chapter"]["chapter_id"] == 1
    assert random_ayah.status_code == 200
    assert random_ayah.json()["ayah"]["verse_key"] == "1:1"
    assert ayahs_range.status_code == 200
    assert ayahs_range.json()["from_verse_key"] == "1:1"
    assert ayahs_range.json()["to_verse_key"] == "1:5"
    assert page.status_code == 200
    assert page.json()["page_number"] == 1
    assert sajdahs.status_code == 200
    assert sajdahs.json()["total"] == 15
    assert sajdahs.json()["sajdahs"][0]["verse_key"] == "7:206"
    assert hizbs.status_code == 200
    assert hizbs.json()["hizbs"][0]["hizb_number"] == 1


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
