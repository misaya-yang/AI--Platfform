from __future__ import annotations

import json
from typing import Any

from redis import asyncio as redis_asyncio

from .config import CacheSettings

try:
    import orjson  # type: ignore
except ModuleNotFoundError:  # pragma: no cover - exercised in minimal envs
    orjson = None


class RedisCache:
    def __init__(self, settings: CacheSettings):
        self.settings = settings
        self._client: redis_asyncio.Redis | None = None

    @property
    def enabled(self) -> bool:
        return bool(self.settings.enabled)

    async def connect(self) -> None:
        if not self.enabled or self._client is not None:
            return
        self._client = redis_asyncio.from_url(self.settings.redis_url, decode_responses=False)

    async def close(self) -> None:
        if self._client is not None:
            await self._client.close()
            self._client = None

    async def get_json(self, key: str) -> Any | None:
        if self._client is None:
            return None
        raw = await self._client.get(key)
        if raw is None:
            return None
        if orjson is not None:
            return orjson.loads(raw)
        return json.loads(raw.decode("utf-8"))

    async def set_json(self, key: str, payload: Any, ttl_seconds: int) -> None:
        if self._client is None:
            return
        encoded = orjson.dumps(payload) if orjson is not None else json.dumps(payload).encode("utf-8")
        await self._client.set(key, encoded, ex=ttl_seconds)

    async def delete(self, *keys: str) -> None:
        if self._client is None or not keys:
            return
        await self._client.delete(*keys)

    async def clear_prefix(self, prefix: str) -> None:
        if self._client is None:
            return
        cursor = 0
        pattern = f"{prefix}*"
        while True:
            cursor, keys = await self._client.scan(cursor=cursor, match=pattern, count=100)
            if keys:
                await self._client.delete(*keys)
            if cursor == 0:
                break

    async def ping(self) -> bool:
        if self._client is None:
            return False
        return bool(await self._client.ping())
