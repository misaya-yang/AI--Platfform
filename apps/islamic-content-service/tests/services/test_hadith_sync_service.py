from __future__ import annotations

from unittest.mock import AsyncMock

import pytest

from islamic_content_service.config import HadithSettings
from islamic_content_service.services.hadith_sync_service import HadithSyncService


def _catalog() -> dict:
    return {
        "bukhari": {
            "name": "Sahih al Bukhari",
            "collection": [
                {"name": "eng-bukhari", "language": "English", "book": "bukhari"},
                {"name": "ara-bukhari", "language": "Arabic", "book": "bukhari"},
            ],
        }
    }


def _english_payload() -> dict:
    return {
        "metadata": {
            "name": "Sahih al Bukhari",
            "sections": {
                "1": "Revelation",
                "2": "Belief",
            },
            "section_details": {
                "1": {"hadithnumber_first": 1, "hadithnumber_last": 2},
                "2": {"hadithnumber_first": 3, "hadithnumber_last": 3},
            },
        },
        "hadiths": [
            {"hadithnumber": 1, "text": "Narrated Umar", "grades": [{"grade": "Sahih"}], "reference": {"book": 1, "hadith": 1}},
            {"hadithnumber": 2, "text": "Narrated Aisha", "grades": [], "reference": {"book": 1, "hadith": 2}},
            {"hadithnumber": 3, "text": "Narrated Abu Huraira", "grades": [], "reference": {"book": 2, "hadith": 1}},
        ],
    }


def _arabic_payload() -> dict:
    return {
        "metadata": {"name": "صحيح البخاري"},
        "hadiths": [
            {"hadithnumber": 1, "text": "حدثنا عمر"},
            {"hadithnumber": 3, "text": "حدثنا أبو هريرة"},
        ],
    }


@pytest.mark.asyncio
async def test_hadith_sync_service_maps_sections_into_books_and_items():
    client = AsyncMock()
    client.is_configured.return_value = True
    client.get_editions = AsyncMock(return_value=_catalog())
    client.get_edition = AsyncMock(side_effect=[_english_payload(), _arabic_payload()])
    repository = AsyncMock()
    sync_repository = AsyncMock()
    service = HadithSyncService(HadithSettings(sync_collections=["bukhari"]), client, repository, sync_repository)

    metrics = await service.sync()

    assert metrics == {"collections": 1, "books": 2, "hadith_items": 3}
    repository.replace_collection.assert_awaited_once()
    collection, books, hadiths = repository.replace_collection.await_args.args
    assert collection["name"] == "bukhari"
    # bukhari now carries real chapters (populated out-of-band from sunnah.com)
    assert collection["has_chapters"] is True
    assert books == [
        {
            "book_number": "1",
            "title": "Revelation",
            "hadith_start_number": 1,
            "hadith_end_number": 2,
            "number_of_hadith": 2,
        },
        {
            "book_number": "2",
            "title": "Belief",
            "hadith_start_number": 3,
            "hadith_end_number": 3,
            "number_of_hadith": 1,
        },
    ]
    assert hadiths[0]["book_number"] == "1"
    assert hadiths[0]["chapter_title"] == "Revelation"
    assert hadiths[0]["arabic_text"] == "حدثنا عمر"
    assert hadiths[2]["book_number"] == "2"
    assert hadiths[2]["chapter_title"] == "Belief"


@pytest.mark.asyncio
async def test_hadith_sync_service_raises_when_section_mapping_is_missing():
    client = AsyncMock()
    client.is_configured.return_value = True
    client.get_editions = AsyncMock(return_value=_catalog())
    broken_payload = _english_payload()
    broken_payload["hadiths"][0]["reference"] = {}
    broken_payload["metadata"]["section_details"] = {}
    client.get_edition = AsyncMock(side_effect=[broken_payload, _arabic_payload()])
    repository = AsyncMock()
    sync_repository = AsyncMock()
    service = HadithSyncService(HadithSettings(sync_collections=["bukhari"]), client, repository, sync_repository)

    with pytest.raises(RuntimeError, match="missing section mapping"):
        await service.sync()


@pytest.mark.asyncio
async def test_hadith_sync_service_buckets_cdn_zero_book_gaps():
    client = AsyncMock()
    client.is_configured.return_value = True
    client.get_editions = AsyncMock(return_value=_catalog())
    gap_payload = _english_payload()
    gap_payload["hadiths"].append(
        {"hadithnumber": 4, "text": "Gap hadith", "grades": [], "reference": {"book": 0, "hadith": 0}}
    )
    client.get_edition = AsyncMock(side_effect=[gap_payload, _arabic_payload()])
    repository = AsyncMock()
    sync_repository = AsyncMock()
    service = HadithSyncService(HadithSettings(sync_collections=["bukhari"]), client, repository, sync_repository)

    metrics = await service.sync()

    assert metrics["books"] == 3
    _, books, hadiths = repository.replace_collection.await_args.args
    assert books[0]["book_number"] == "0"
    assert books[0]["title"] == "Unmapped (CDN section gap)"
    assert books[0]["number_of_hadith"] == 1
    assert hadiths[-1]["book_number"] == "0"
    assert hadiths[-1]["chapter_title"] == "Unmapped (CDN section gap)"


@pytest.mark.asyncio
async def test_hadith_sync_service_skips_empty_cdn_phantom_hadiths():
    client = AsyncMock()
    client.is_configured.return_value = True
    client.get_editions = AsyncMock(return_value=_catalog())
    gap_payload = _english_payload()
    gap_payload["hadiths"].append(
        {"hadithnumber": 4, "text": "", "grades": [], "reference": {"book": 0, "hadith": 0}}
    )
    client.get_edition = AsyncMock(side_effect=[gap_payload, _arabic_payload()])
    repository = AsyncMock()
    sync_repository = AsyncMock()
    service = HadithSyncService(HadithSettings(sync_collections=["bukhari"]), client, repository, sync_repository)

    metrics = await service.sync()

    assert metrics == {"collections": 1, "books": 2, "hadith_items": 3}
    _, books, hadiths = repository.replace_collection.await_args.args
    assert [book["book_number"] for book in books] == ["1", "2"]
    assert [item["hadith_number"] for item in hadiths] == ["1", "2", "3"]


@pytest.mark.asyncio
async def test_hadith_sync_service_deduplicates_duplicate_hadith_numbers():
    client = AsyncMock()
    client.is_configured.return_value = True
    client.get_editions = AsyncMock(return_value=_catalog())
    duplicate_payload = _english_payload()
    duplicate_payload["hadiths"].append(
        {"hadithnumber": 1, "text": "Duplicate Umar", "grades": [], "reference": {"book": 1, "hadith": 1}}
    )
    client.get_edition = AsyncMock(side_effect=[duplicate_payload, _arabic_payload()])
    repository = AsyncMock()
    sync_repository = AsyncMock()
    service = HadithSyncService(HadithSettings(sync_collections=["bukhari"]), client, repository, sync_repository)

    metrics = await service.sync()

    assert metrics["hadith_items"] == 3
    _, _, hadiths = repository.replace_collection.await_args.args
    assert [item["hadith_number"] for item in hadiths] == ["1", "2", "3"]
