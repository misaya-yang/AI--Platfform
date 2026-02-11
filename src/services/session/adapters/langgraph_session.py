from __future__ import annotations

from typing import Any


class LangGraphSessionAdapter:
    def __init__(self, checkpointer: Any | None = None):
        self.checkpointer = checkpointer

    async def load(self, session_id: str) -> Any:
        if not self.checkpointer:
            return None
        return await self.checkpointer.get(session_id)

    async def save(self, session_id: str, state: Any) -> None:
        if not self.checkpointer:
            return
        await self.checkpointer.save(session_id, state)
