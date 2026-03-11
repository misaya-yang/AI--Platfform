from __future__ import annotations

import contextlib
from pathlib import Path
from typing import Any, Callable

import asyncpg

from .config import DatabaseSettings


class Database:
    def __init__(self, settings: DatabaseSettings):
        self.settings = settings
        self._pool: asyncpg.Pool | None = None

    @property
    def connected(self) -> bool:
        return self._pool is not None

    async def connect(self) -> None:
        if self._pool is not None:
            return

        async def _configure_conn(connection: asyncpg.Connection) -> None:
            await connection.execute(
                f'SET search_path TO "{self.settings.schema}", public'
            )

        self._pool = await asyncpg.create_pool(
            dsn=self.settings.dsn,
            min_size=self.settings.pool_min_size,
            max_size=self.settings.pool_max_size,
            init=_configure_conn,
            setup=_configure_conn,
        )

    async def close(self) -> None:
        if self._pool is not None:
            await self._pool.close()
            self._pool = None

    async def execute(self, query: str, *args: Any) -> str:
        if self._pool is None:
            raise RuntimeError("Database is not connected")
        async with self._pool.acquire() as connection:
            return await connection.execute(query, *args)

    async def executemany(self, query: str, args_list: list[tuple[Any, ...]]) -> None:
        if self._pool is None:
            raise RuntimeError("Database is not connected")
        if not args_list:
            return
        async with self._pool.acquire() as connection:
            await connection.executemany(query, args_list)

    async def fetch(self, query: str, *args: Any) -> list[Any]:
        if self._pool is None:
            raise RuntimeError("Database is not connected")
        async with self._pool.acquire() as connection:
            return await connection.fetch(query, *args)

    async def fetchrow(self, query: str, *args: Any) -> Any | None:
        if self._pool is None:
            raise RuntimeError("Database is not connected")
        async with self._pool.acquire() as connection:
            return await connection.fetchrow(query, *args)

    async def fetchval(self, query: str, *args: Any) -> Any | None:
        if self._pool is None:
            raise RuntimeError("Database is not connected")
        async with self._pool.acquire() as connection:
            return await connection.fetchval(query, *args)

    async def transaction(self, func: Callable[[asyncpg.Connection], Any]) -> Any:
        if self._pool is None:
            raise RuntimeError("Database is not connected")
        async with self._pool.acquire() as connection:
            async with connection.transaction():
                return await func(connection)

    async def execute_script(self, script_path: str | Path) -> None:
        if self._pool is None:
            raise RuntimeError("Database is not connected")
        sql = Path(script_path).read_text(encoding="utf-8").replace(
            "__ISLAMIC_CONTENT_SCHEMA__",
            self.settings.schema,
        )
        async with self._pool.acquire() as connection:
            await connection.execute(sql)

    async def migrate(self, script_path: str | Path) -> None:
        await self.execute_script(script_path)

    async def ping(self) -> bool:
        with contextlib.suppress(Exception):
            return bool(await self.fetchval("SELECT 1"))
        return False
