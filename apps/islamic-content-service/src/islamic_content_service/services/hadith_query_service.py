from __future__ import annotations

from collections.abc import Awaitable, Callable
from datetime import datetime, timezone
from typing import Any

from ..cache import RedisCache
from ..config import CacheSettings
from ..domain.constants import HADITH_SOURCE_API
from ..domain.errors import NotReadyError
from ..repositories.hadith_repository import HadithRepository


class HadithQueryService:
    def __init__(
        self,
        cache_settings: CacheSettings,
        repository: HadithRepository,
        cache: RedisCache,
    ) -> None:
        self.cache_settings = cache_settings
        self.repository = repository
        self.cache = cache

    async def _cached(
        self,
        key: str,
        ttl: int,
        loader: Callable[[], Awaitable[dict[str, Any]]],
    ) -> dict[str, Any]:
        cached = await self.cache.get_json(key)
        if cached is not None:
            return cached
        payload = await loader()
        await self.cache.set_json(key, payload, ttl)
        return payload

    async def invalidate(self) -> None:
        await self.cache.clear_prefix("hadith:")

    async def get_collections(self) -> dict[str, Any]:
        async def _load() -> dict[str, Any]:
            collections = await self.repository.get_collections()
            if not collections:
                raise NotReadyError("No Hadith collection data found")
            return {
                "generated_at": datetime.now(timezone.utc).isoformat(),
                "screen": "hadith_collections",
                "source_api": HADITH_SOURCE_API,
                "collections": collections,
            }

        return await self._cached("hadith:collections", self.cache_settings.ttl_seconds, _load)

    async def get_books(self, collection_name: str) -> dict[str, Any]:
        async def _load() -> dict[str, Any]:
            collection, books = await self.repository.get_books(collection_name)
            if collection is None:
                raise NotReadyError(f"No Hadith collection found for {collection_name}")
            return {
                "generated_at": datetime.now(timezone.utc).isoformat(),
                "screen": "hadith_books",
                "source_api": HADITH_SOURCE_API,
                "collection": collection,
                "books": books,
            }

        return await self._cached(
            f"hadith:books:{collection_name}",
            self.cache_settings.ttl_seconds,
            _load,
        )

    async def get_book_items(
        self,
        collection_name: str,
        book_number: str,
        *,
        page: int,
        limit: int,
    ) -> dict[str, Any]:
        async def _load() -> dict[str, Any]:
            payload = await self.repository.get_book_items(
                collection_name,
                book_number,
                page=page,
                limit=limit,
            )
            # Empty result: return 200 with empty items + total_items=0
            # rather than 503 NotReadyError. Lets Java distinguish a
            # legitimately empty page from a real service issue, and
            # surfaces typo book_numbers via total_items=0 instead of 5xx.
            return {
                "generated_at": datetime.now(timezone.utc).isoformat(),
                "screen": "hadith_book_items",
                "source_api": HADITH_SOURCE_API,
                "collection_name": collection_name,
                "book_number": book_number,
                **payload,
            }

        return await self._cached(
            f"hadith:book_items:{collection_name}:{book_number}:{page}:{limit}",
            self.cache_settings.ttl_seconds,
            _load,
        )

    async def get_chapters(
        self,
        collection_name: str,
        book_number: str,
        *,
        include_empty: bool = False,
    ) -> dict[str, Any]:
        async def _load() -> dict[str, Any]:
            collection, _ = await self.repository.get_books(collection_name)
            if collection is None:
                raise NotReadyError(f"No Hadith collection found for {collection_name}")
            chapters = await self.repository.get_chapters(collection_name, book_number)
            if not include_empty:
                chapters = [
                    chapter for chapter in chapters
                    if (chapter.get("hadith_count") or 0) > 0
                ]
            # Empty result: return 200 with empty chapters list rather
            # than 503 NotReadyError. Same rationale as get_book_items.
            return {
                "generated_at": datetime.now(timezone.utc).isoformat(),
                "screen": "hadith_chapters",
                "source_api": HADITH_SOURCE_API,
                "collection_name": collection_name,
                "book_number": book_number,
                "chapters": chapters,
            }

        return await self._cached(
            f"hadith:chapters:{collection_name}:{book_number}:{include_empty}",
            self.cache_settings.ttl_seconds,
            _load,
        )

    async def get_collection_detail(self, collection_name: str) -> dict[str, Any]:
        async def _load() -> dict[str, Any]:
            collection = await self.repository.get_collection(collection_name)
            if collection is None:
                raise NotReadyError(f"No Hadith collection found for {collection_name}")
            return {
                "generated_at": datetime.now(timezone.utc).isoformat(),
                "screen": "hadith_collection_detail",
                "source_api": HADITH_SOURCE_API,
                "collection": collection,
            }

        return await self._cached(
            f"hadith:collection:{collection_name}",
            self.cache_settings.meta_ttl_seconds,
            _load,
        )

    async def get_random_hadith(
        self, *, collection_name: str | None = None
    ) -> dict[str, Any]:
        picked = await self.repository.get_random_hadith(collection_name=collection_name)
        if picked is None:
            raise NotReadyError(
                "No Hadith data available"
                if collection_name is None
                else f"No Hadiths found in collection {collection_name}"
            )
        coll, number = picked
        detail = await self.repository.get_detail(coll, number)
        if detail is None:
            raise NotReadyError(f"Detail missing for {coll}:{number}")
        return {
            "generated_at": datetime.now(timezone.utc).isoformat(),
            "screen": "hadith_random",
            "source_api": HADITH_SOURCE_API,
            "hadith": detail,
        }

    async def get_context(
        self, collection_name: str, hadith_number: str
    ) -> dict[str, Any]:
        async def _load() -> dict[str, Any]:
            # Confirm hadith exists so we 503 early on typos
            detail = await self.repository.get_detail(collection_name, hadith_number)
            if detail is None:
                raise NotReadyError(
                    f"No Hadith found for {collection_name}:{hadith_number}"
                )
            neighbors = await self.repository.get_neighbors(collection_name, hadith_number)
            return {
                "generated_at": datetime.now(timezone.utc).isoformat(),
                "screen": "hadith_context",
                "source_api": HADITH_SOURCE_API,
                "collection": collection_name,
                "hadith_number": hadith_number,
                "neighbors": neighbors,
            }

        return await self._cached(
            f"hadith:context:{collection_name}:{hadith_number}",
            self.cache_settings.ttl_seconds,
            _load,
        )

    async def search_hadiths(
        self,
        query: str,
        *,
        language: str = "en",
        collection_name: str | None = None,
        limit: int = 20,
        offset: int = 0,
    ) -> dict[str, Any]:
        query = (query or "").strip()
        if not query:
            raise NotReadyError("Query parameter 'q' must not be empty")
        if language not in ("en", "ar"):
            raise NotReadyError(f"Unsupported search language: {language}")

        async def _load() -> dict[str, Any]:
            items, total = await self.repository.search_hadiths(
                query,
                language=language,
                collection_name=collection_name,
                limit=limit,
                offset=offset,
            )
            return {
                "generated_at": datetime.now(timezone.utc).isoformat(),
                "screen": "hadith_search",
                "source_api": HADITH_SOURCE_API,
                "query": query,
                "language": language,
                "collection": collection_name,
                "total": total,
                "limit": limit,
                "offset": offset,
                "items": items,
            }

        return await self._cached(
            f"hadith:search:{query}:{language}:{collection_name or '-'}:{limit}:{offset}",
            self.cache_settings.ttl_seconds,
            _load,
        )

    async def get_detail(self, collection_name: str, hadith_number: str) -> dict[str, Any]:
        async def _load() -> dict[str, Any]:
            detail = await self.repository.get_detail(collection_name, hadith_number)
            if detail is None:
                raise NotReadyError(
                    f"No Hadith detail found for {collection_name}:{hadith_number}"
                )
            return {
                "generated_at": datetime.now(timezone.utc).isoformat(),
                "screen": "hadith_detail",
                "source_api": HADITH_SOURCE_API,
                "hadith": detail,
            }

        return await self._cached(
            f"hadith:detail:{collection_name}:{hadith_number}",
            self.cache_settings.ttl_seconds,
            _load,
        )
