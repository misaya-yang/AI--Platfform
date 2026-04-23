from __future__ import annotations

import pytest


class StubHadithQueryService:
    async def get_collections(self):
        return {
            "generated_at": "2026-03-09T00:00:00Z",
            "screen": "hadith_collections",
            "source_api": "hadith-cdn",
            "collections": [{"name": "bukhari", "title": "Sahih al-Bukhari"}],
        }

    async def get_books(self, collection_name: str):
        return {
            "generated_at": "2026-03-09T00:00:00Z",
            "screen": "hadith_books",
            "source_api": "hadith-cdn",
            "collection": {"name": collection_name, "title": "Sahih al-Bukhari", "has_chapters": False},
            "books": [{"book_number": "1", "title": "Revelation"}],
        }

    async def get_book_items(self, collection_name: str, book_number: str, *, page: int, limit: int):
        return {
            "generated_at": "2026-03-09T00:00:00Z",
            "screen": "hadith_book_items",
            "source_api": "hadith-cdn",
            "collection_name": collection_name,
            "book_number": book_number,
            "items": [{
                "collection": collection_name,
                "book_number": book_number,
                "section_number": book_number,
                "section_title": "Revelation",
                "chapter_id": book_number,
                "hadith_number": "1",
                "title": "Revelation",
            }],
            "pagination": {"page": page, "limit": limit, "total_items": 1, "total_pages": 1},
        }

    async def get_collection_detail(self, collection_name: str):
        return {
            "generated_at": "2026-04-23T00:00:00Z",
            "screen": "hadith_collection_detail",
            "source_api": "hadith-cdn",
            "collection": {"name": collection_name, "title": "Sahih al-Bukhari", "total_hadith": 7563},
        }

    async def get_random_hadith(self, *, collection_name=None):
        return {
            "generated_at": "2026-04-23T00:00:00Z",
            "screen": "hadith_random",
            "source_api": "hadith-cdn",
            "hadith": {
                "collection": collection_name or "bukhari",
                "book_number": "1",
                "section_title": "Revelation",
                "chapter_id": "1",
                "chapter_title": "Revelation",
                "hadith_number": "1",
                "translation_text": "Actions are but by intention.",
                "arabic_text": "إنما الأعمال بالنيات",
                "grades": {},
            },
        }

    async def get_context(self, collection_name: str, hadith_number: str):
        return {
            "generated_at": "2026-04-23T00:00:00Z",
            "screen": "hadith_context",
            "source_api": "hadith-cdn",
            "collection": collection_name,
            "hadith_number": hadith_number,
            "neighbors": {"previous": None, "next": "2"},
        }

    async def search_hadiths(self, query: str, *, language="en", collection_name=None, limit=20, offset=0):
        return {
            "generated_at": "2026-04-23T00:00:00Z",
            "screen": "hadith_search",
            "source_api": "hadith-cdn",
            "query": query,
            "language": language,
            "collection": collection_name,
            "total": 1,
            "limit": limit,
            "offset": offset,
            "items": [{
                "collection": collection_name or "bukhari",
                "book_number": "1",
                "book_title": "Revelation",
                "chapter_title": "The first revelation",
                "hadith_number": "1",
                "language": language,
                "preview_text": "Actions are but by intention...",
            }],
        }

    async def get_detail(self, collection_name: str, hadith_number: str):
        return {
            "generated_at": "2026-03-09T00:00:00Z",
            "screen": "hadith_detail",
            "source_api": "hadith-cdn",
            "hadith": {
                "collection": collection_name,
                "book_number": "1",
                "section_number": "1",
                "section_title": "Revelation",
                "chapter_id": "1",
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
    search = await async_client.get(
        "/api/v1/hadith/search",
        params={"q": "intention", "collection": "bukhari"},
    )
    coll_detail = await async_client.get("/api/v1/hadith/collections/bukhari")
    random_hadith = await async_client.get("/api/v1/hadith/random")
    context = await async_client.get("/api/v1/hadith/collections/bukhari/hadiths/1/context")

    assert collections.status_code == 200
    assert books.json()["collection"]["name"] == "bukhari"
    assert books.json()["collection"]["has_chapters"] is False
    assert items.json()["items"][0]["hadith_number"] == "1"
    assert items.json()["items"][0]["section_title"] == "Revelation"
    assert detail.json()["hadith"]["section_title"] == "Revelation"
    assert detail.json()["hadith"]["chapter_title"] == "Revelation"
    assert search.status_code == 200
    assert search.json()["query"] == "intention"
    assert search.json()["collection"] == "bukhari"
    assert search.json()["items"][0]["preview_text"].startswith("Actions")
    assert coll_detail.status_code == 200
    assert coll_detail.json()["collection"]["total_hadith"] == 7563
    assert random_hadith.status_code == 200
    assert random_hadith.json()["hadith"]["hadith_number"] == "1"
    assert context.status_code == 200
    assert context.json()["neighbors"]["next"] == "2"
