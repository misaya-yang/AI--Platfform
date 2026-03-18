"""Shim: DatabaseStorage compatible with gateway's persistence.database.

Maps the gateway's DatabaseStorage interface to the KB service's DatabasePool.
"""
from knowledge_service.db.connection import DatabasePool


class DatabaseStorage:
    """Thin wrapper that delegates to DatabasePool.

    The gateway's KnowledgeService expects a DatabaseStorage with
    execute/fetch/fetchrow/fetchval methods — DatabasePool already
    provides these, so this is mostly a type alias.
    """

    def __init__(self, pool: DatabasePool):
        self._pool = pool

    async def execute(self, sql: str, *args):
        return await self._pool.execute(sql, *args)

    async def fetch(self, sql: str, *args):
        return await self._pool.fetch(sql, *args)

    async def fetchrow(self, sql: str, *args):
        return await self._pool.fetchrow(sql, *args)

    async def fetchval(self, sql: str, *args):
        return await self._pool.fetchval(sql, *args)

    @property
    def pool(self):
        return self._pool._pool
