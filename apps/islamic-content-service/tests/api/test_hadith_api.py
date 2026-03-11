from __future__ import annotations

import pytest


class StubHadithQueryService:
    async def get_collections(self):
        return {
            "generated_at": "2026-03-09T00:00:00Z",
            "screen": "hadith_collections",
            "source_api": "sunnah",
            "collections": [{"name": "bukhari", "title": "Sahih al-Bukhari"}],
        }

    async def get_books(self, collection_name: str):
        return {
            "generated_at": "2026-03-09T00:00:00Z",
            "screen": "hadith_books",
            "source_api": "sunnah",
            "collection": {"name": collection_name, "title": "Sahih al-Bukhari"},
            "books": [{"book_number": "1", "title": "Book 1"}],
        }

    async def get_book_items(self, collection_name: str, book_number: str, *, page: int, limit: int):
        return {
            "generated_at": "2026-03-09T00:00:00Z",
            "screen": "hadith_book_items",
            "source_api": "sunnah",
            "collection_name": collection_name,
            "book_number": book_number,
            "items": [{"collection": collection_name, "book_number": book_number, "hadith_number": "1"}],
            "pagination": {"page": page, "limit": limit, "total_items": 1, "total_pages": 1},
        }

    async def get_detail(self, collection_name: str, hadith_number: str):
        return {
            "generated_at": "2026-03-09T00:00:00Z",
            "screen": "hadith_detail",
            "source_api": "sunnah",
            "hadith": {
                "collection": collection_name,
                "book_number": "1",
                "chapter_id": "2",
                "hadith_number": hadith_number,
                "chapter_title": "Revelation",
                "translation_text": "Narrated Umar...",
                "arabic_text": "حدثنا...",
                "grades": {"en": []},
            },
        }


@pytest.mark.asyncio
async def test_hadith_endpoints(async_client, test_app):
    test_app.state.hadith_query_service = StubHadithQueryService()

    collections = await async_client.get("/api/v1/hadith/collections")
    books = await async_client.get("/api/v1/hadith/collections/bukhari/books")
    items = await async_client.get("/api/v1/hadith/collections/bukhari/books/1/hadiths")
    detail = await async_client.get("/api/v1/hadith/collections/bukhari/hadiths/1")

    assert collections.status_code == 200
    assert books.json()["collection"]["name"] == "bukhari"
    assert items.json()["items"][0]["hadith_number"] == "1"
    assert detail.json()["hadith"]["chapter_title"] == "Revelation"
