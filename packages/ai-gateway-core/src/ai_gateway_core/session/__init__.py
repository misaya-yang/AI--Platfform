"""Session-manager contract.

Both services need to read/write chat-session state. The concrete
``DatabaseSessionManager`` (asyncpg + JSON columns) stays per-service.
"""

from __future__ import annotations

from typing import Any, Protocol, runtime_checkable


@runtime_checkable
class SessionManagerLike(Protocol):
    """Minimal async contract for session CRUD used by assistant callers."""

    async def get_session(self, session_id: str) -> dict[str, Any] | None: ...
    async def create_session(self, **kwargs: Any) -> str: ...
    async def update_session(self, session_id: str, **kwargs: Any) -> None: ...
    async def append_message(self, session_id: str, message: dict[str, Any]) -> None: ...


__all__ = ["SessionManagerLike"]
