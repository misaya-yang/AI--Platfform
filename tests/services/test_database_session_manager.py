from __future__ import annotations

from unittest.mock import AsyncMock

import pytest

from src.models.session import Session
from src.services.session.database_session_manager import DatabaseSessionManager


def _build_manager() -> tuple[DatabaseSessionManager, AsyncMock]:
    db = AsyncMock()
    db.update_session_config = AsyncMock(return_value=True)
    db.update_session_metadata = AsyncMock(return_value=True)
    db.save_session = AsyncMock()
    manager = DatabaseSessionManager(database=db, redis=None)
    return manager, db


def _build_session(session_id: str = "session-1") -> Session:
    return Session(
        session_id=session_id,
        user_id="user-1",
        tenant_id="tenant-1",
        metadata={"title": "old"},
        history=[],
        state={},
        config={"model": "gemini"},
    )


@pytest.mark.asyncio
async def test_update_config_uses_atomic_patch_and_invalidates_cache():
    manager, db = _build_manager()
    session = _build_session()
    manager._memory_cache[session.session_id] = session

    result = await manager.update_config(session.session_id, {"selected_datasets": ["kb_1"]})

    assert result is True
    db.update_session_config.assert_awaited_once_with(
        session.session_id, {"selected_datasets": ["kb_1"]}
    )
    db.save_session.assert_not_awaited()
    assert session.session_id not in manager._memory_cache


@pytest.mark.asyncio
async def test_update_metadata_uses_atomic_patch_and_invalidates_cache():
    manager, db = _build_manager()
    session = _build_session("session-2")
    manager._memory_cache[session.session_id] = session

    result = await manager.update_metadata(session.session_id, {"agent_id": "imam"})

    assert result is True
    db.update_session_metadata.assert_awaited_once_with(session.session_id, {"agent_id": "imam"})
    db.save_session.assert_not_awaited()
    assert session.session_id not in manager._memory_cache
