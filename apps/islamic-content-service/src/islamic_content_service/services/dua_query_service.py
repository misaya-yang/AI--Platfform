from __future__ import annotations

from datetime import datetime, timezone
from typing import Any, Awaitable, Callable

from ..cache import RedisCache
from ..config import CacheSettings
from ..domain.errors import NotReadyError
from ..repositories.dua_repository import DuaRepository

DUA_SOURCE = "kaggle/islamic-dua-adhkar"


class DuaQueryService:
    def __init__(
        self,
        cache_settings: CacheSettings,
        repository: DuaRepository,
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
        await self.cache.clear_prefix("dua:")

    async def get_categories(self) -> dict[str, Any]:
        async def _load() -> dict[str, Any]:
            categories = await self.repository.get_categories()
            if not categories:
                raise NotReadyError("No Dua category data found")
            return {
                "generated_at": datetime.now(timezone.utc).isoformat(),
                "screen": "dua_categories",
                "source": DUA_SOURCE,
                "total_categories": len(categories),
                "total_duas": sum(c["dua_count"] for c in categories),
                "categories": categories,
            }

        return await self._cached("dua:categories", self.cache_settings.ttl_seconds, _load)

    async def get_items_by_category(self, category: str) -> dict[str, Any]:
        async def _load() -> dict[str, Any]:
            items = await self.repository.get_items_by_category(category)
            if not items:
                raise NotReadyError(f"No Duas found for category: {category}")
            return {
                "generated_at": datetime.now(timezone.utc).isoformat(),
                "screen": "dua_category_items",
                "source": DUA_SOURCE,
                "category": category,
                "total": len(items),
                "items": items,
            }

        return await self._cached(
            f"dua:category:{category}",
            self.cache_settings.ttl_seconds,
            _load,
        )

    async def get_all_items(self) -> dict[str, Any]:
        async def _load() -> dict[str, Any]:
            items = await self.repository.get_all_items()
            if not items:
                raise NotReadyError("No Dua data found")
            return {
                "generated_at": datetime.now(timezone.utc).isoformat(),
                "screen": "dua_all_items",
                "source": DUA_SOURCE,
                "total": len(items),
                "items": items,
            }

        return await self._cached("dua:all_items", self.cache_settings.ttl_seconds, _load)

    async def get_detail(self, dua_id: str) -> dict[str, Any]:
        async def _load() -> dict[str, Any]:
            detail = await self.repository.get_detail(dua_id)
            if detail is None:
                raise NotReadyError(f"Dua not found: {dua_id}")
            return {
                "generated_at": datetime.now(timezone.utc).isoformat(),
                "screen": "dua_detail",
                "source": DUA_SOURCE,
                "dua": detail,
            }

        return await self._cached(
            f"dua:detail:{dua_id}",
            self.cache_settings.ttl_seconds,
            _load,
        )
