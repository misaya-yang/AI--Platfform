from __future__ import annotations

from datetime import datetime, timezone
from typing import Any, Awaitable, Callable

from ..cache import RedisCache
from ..config import CacheSettings
from ..domain.constants import SUNNAH_SOURCE_API
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
                "source_api": SUNNAH_SOURCE_API,
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
                "source_api": SUNNAH_SOURCE_API,
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
            if not payload["items"]:
                raise NotReadyError(
                    f"No Hadith items found for {collection_name} book {book_number}"
                )
            return {
                "generated_at": datetime.now(timezone.utc).isoformat(),
                "screen": "hadith_book_items",
                "source_api": SUNNAH_SOURCE_API,
                "collection_name": collection_name,
                "book_number": book_number,
                **payload,
            }

        return await self._cached(
            f"hadith:book_items:{collection_name}:{book_number}:{page}:{limit}",
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
                "source_api": SUNNAH_SOURCE_API,
                "hadith": detail,
            }

        return await self._cached(
            f"hadith:detail:{collection_name}:{hadith_number}",
            self.cache_settings.ttl_seconds,
            _load,
        )
