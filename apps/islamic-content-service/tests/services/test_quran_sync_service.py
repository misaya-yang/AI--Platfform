from __future__ import annotations

from unittest.mock import AsyncMock

import pytest

from islamic_content_service.config import QuranSettings
from islamic_content_service.services.quran_sync_service import QuranSyncService


@pytest.mark.asyncio
async def test_quran_sync_service_generates_triplets():
    service = QuranSyncService(
        QuranSettings(default_translation_id=20, default_recitation_id=7),
        client=AsyncMock(),
        repository=AsyncMock(),
        sync_repository=AsyncMock(),
    )

    blocks = service._build_triplet_ranges(
        1,
        [
            {"verse_key": "1:1", "ayah_number": 1},
            {"verse_key": "1:2", "ayah_number": 2},
            {"verse_key": "1:3", "ayah_number": 3},
            {"verse_key": "1:4", "ayah_number": 4},
        ],
    )

    assert len(blocks) == 2
    assert blocks[0]["block_id"] == "quran:1:1-3"
    assert blocks[0]["verse_keys"] == ["1:1", "1:2", "1:3"]
    assert blocks[1]["block_id"] == "quran:1:4-4"


@pytest.mark.asyncio
async def test_quran_sync_service_normalizes_audio_timings():
    service = QuranSyncService(
        QuranSettings(default_translation_id=20, default_recitation_id=7),
        client=AsyncMock(),
        repository=AsyncMock(),
        sync_repository=AsyncMock(),
    )

    timings = service._normalize_audio_timings(
        1,
        {
            "audio_file": {
                "timestamps": [
                    {
                        "verse_key": "1:1",
                        "timestamp_from": 0,
                        "timestamp_to": 6090,
                        "duration": 6090,
                        "segments": [[1, 0, 580], [2, 580, 1409]],
                    }
                ]
            }
        },
    )

    assert timings == [
        {
            "chapter_id": 1,
            "verse_key": "1:1",
            "timestamp_from_ms": 0,
            "timestamp_to_ms": 6090,
            "duration_ms": 6090,
            "segments": [
                {"word_index": 1, "start_ms": 0, "end_ms": 580},
                {"word_index": 2, "start_ms": 580, "end_ms": 1409},
            ],
        }
    ]


@pytest.mark.asyncio
async def test_quran_sync_service_cleans_ayah_text_and_audio_urls():
    service = QuranSyncService(
        QuranSettings(default_translation_id=20, default_recitation_id=7),
        client=AsyncMock(),
        repository=AsyncMock(),
        sync_repository=AsyncMock(),
    )

    ayah = service._normalize_ayah_base(
        {
            "verse_key": "1:1",
            "text_uthmani": "بِسْمِ ٱللَّهِ",
            "words": [
                {
                    "position": 1,
                    "char_type_name": "word",
                    "text_uthmani": "بِسْمِ",
                    "text_uthmani_simple": "بسم",
                    "transliteration": {"text": "bis'mi"},
                    "translation": {"text": "In the name"},
                    "audio_url": "wbw/001_001_001.mp3",
                },
                {
                    "position": 2,
                    "char_type_name": "end",
                    "text_uthmani": "١",
                    "transliteration": {"text": None, "language_name": "english"},
                    "translation": {"text": "(1)"},
                },
            ],
        }
    )

    assert ayah["transliteration_text"] == "bis'mi"
    assert len(ayah["words"]) == 1
    assert ayah["words"][0]["audio_url"] == "https://verses.quran.foundation/wbw/001_001_001.mp3"


@pytest.mark.asyncio
async def test_quran_sync_service_normalizes_translation_rows():
    service = QuranSyncService(
        QuranSettings(default_translation_id=20, default_recitation_id=7),
        client=AsyncMock(),
        repository=AsyncMock(),
        sync_repository=AsyncMock(),
    )

    rows = service._normalize_translation_rows(
        {
            "verse_key": "1:1",
            "translations": [
                {"resource_id": 20, "text": "In the name of Allāh,<sup foot_note=1>1</sup>"},
                {"resource_id": 84, "text": "With the name of Allah"},
            ],
        }
    )

    assert rows[20]["translation_text"] == "In the name of Allāh,"
    assert rows[84]["translation_text"] == "With the name of Allah"
