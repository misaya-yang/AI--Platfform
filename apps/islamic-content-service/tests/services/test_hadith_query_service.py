from __future__ import annotations

import pytest
from islamic_content_service.config import CacheSettings
from islamic_content_service.services.hadith_query_service import HadithQueryService


class NoopCache:
    async def get_json(self, _key):
        return None

    async def set_json(self, _key, _payload, _ttl):
        return None

    async def clear_prefix(self, _prefix):
        return None


class ChaptersRepo:
    async def get_books(self, collection_name: str):
        return {"name": collection_name}, []

    async def get_chapters(self, _collection_name: str, _book_number: str):
        return [
            {
                "chapter_id": "1",
                "chapter_number": 1,
                "chapter_title": "Heading only",
                "hadith_count": 0,
            },
            {
                "chapter_id": "2",
                "chapter_number": 2,
                "chapter_title": "With hadith",
                "hadith_count": 1,
            },
        ]


@pytest.mark.asyncio
async def test_get_chapters_filters_empty_source_chapters_by_default():
    service = HadithQueryService(CacheSettings(), ChaptersRepo(), NoopCache())

    payload = await service.get_chapters("bukhari", "2")

    assert [chapter["chapter_id"] for chapter in payload["chapters"]] == ["2"]


@pytest.mark.asyncio
async def test_get_chapters_can_include_empty_source_chapters_for_debugging():
    service = HadithQueryService(CacheSettings(), ChaptersRepo(), NoopCache())

    payload = await service.get_chapters("bukhari", "2", include_empty=True)

    assert [chapter["chapter_id"] for chapter in payload["chapters"]] == ["1", "2"]
