"""Database-storage contract.

Both services need a handle to persistent storage but the concrete class
(asyncpg-backed ``DatabaseStorage``) stays per-service. This Protocol
captures only the methods callers actually use, so the implementations
can evolve independently.
"""

from __future__ import annotations

from typing import Any, Protocol, runtime_checkable


@runtime_checkable
class DatabaseStorageLike(Protocol):
    """Minimal async query surface used by assistant-owned callers."""

    async def fetch_one(self, query: str, *args: Any) -> dict[str, Any] | None: ...
    async def fetch_all(self, query: str, *args: Any) -> list[dict[str, Any]]: ...
    async def execute(self, query: str, *args: Any) -> Any: ...


__all__ = ["DatabaseStorageLike"]
