"""Async image task store — Redis-backed with in-process dict fallback.

Locks the contract for ``_store_task`` / ``_load_task`` in
``assistant_service/api/routes/images.py``:

- Redis path: writes go through ``set(..., ex=TTL)``; reads go through ``get``.
- Fallback: when Redis is None or raises, the in-process dict is used so
  dev/test work without infra.
- Backend swap is transparent — submit on Redis, restart, query on Redis again
  and find the task. Submit without Redis, query without Redis, same dict
  serves both.
"""

from __future__ import annotations

import json
from unittest.mock import AsyncMock, MagicMock

import pytest

from assistant_service.api.routes.images import (
    _image_tasks,
    _load_task,
    _store_task,
    _TASK_KEY_PREFIX,
    _TASK_TTL_SECONDS,
)


@pytest.fixture(autouse=True)
def _clear_inprocess_dict():
    """Stop one test's in-process state from leaking into the next."""
    _image_tasks.clear()
    yield
    _image_tasks.clear()


# ---------------------------------------------------------------------------
# Redis path
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_store_writes_to_redis_with_ttl():
    redis = MagicMock()
    redis.set = AsyncMock()
    redis.get = AsyncMock()

    task = {"task_id": "t1", "status": "pending", "prompt": "cat"}
    await _store_task(redis, "t1", task)

    redis.set.assert_awaited_once()
    args, kwargs = redis.set.await_args
    assert args[0] == _TASK_KEY_PREFIX + "t1"
    assert json.loads(args[1]) == task
    assert kwargs["ex"] == _TASK_TTL_SECONDS


@pytest.mark.asyncio
async def test_load_reads_from_redis():
    task = {"task_id": "t1", "status": "completed", "prompt": "cat"}
    redis = MagicMock()
    redis.get = AsyncMock(return_value=json.dumps(task))

    result = await _load_task(redis, "t1")

    redis.get.assert_awaited_once_with(_TASK_KEY_PREFIX + "t1")
    assert result == task


@pytest.mark.asyncio
async def test_load_returns_none_when_redis_has_no_key():
    redis = MagicMock()
    redis.get = AsyncMock(return_value=None)

    result = await _load_task(redis, "nope")
    assert result is None


# ---------------------------------------------------------------------------
# Fallback path — Redis is None
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_store_falls_back_to_dict_when_redis_is_none():
    task = {"task_id": "t1", "status": "pending"}
    await _store_task(None, "t1", task)
    assert _image_tasks["t1"] == task


@pytest.mark.asyncio
async def test_load_falls_back_to_dict_when_redis_is_none():
    _image_tasks["t1"] = {"task_id": "t1", "status": "running"}
    result = await _load_task(None, "t1")
    assert result["status"] == "running"


@pytest.mark.asyncio
async def test_load_returns_none_when_dict_is_empty():
    result = await _load_task(None, "missing")
    assert result is None


# ---------------------------------------------------------------------------
# Resilience — Redis errors degrade to in-process dict, not 500s
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_store_falls_back_when_redis_raises():
    """If Redis breaks mid-flight, the request must still succeed by writing
    to the in-process dict. Logging the warning is fine; raising is not."""
    redis = MagicMock()
    redis.set = AsyncMock(side_effect=ConnectionError("boom"))

    task = {"task_id": "t1", "status": "pending"}
    await _store_task(redis, "t1", task)

    assert _image_tasks["t1"] == task


@pytest.mark.asyncio
async def test_load_falls_back_when_redis_raises():
    redis = MagicMock()
    redis.get = AsyncMock(side_effect=ConnectionError("boom"))
    _image_tasks["t1"] = {"task_id": "t1", "status": "completed"}

    result = await _load_task(redis, "t1")
    assert result["status"] == "completed"


# ---------------------------------------------------------------------------
# Round-trip — store then load via Redis returns the same task shape
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_redis_round_trip_preserves_full_task_shape():
    """The async task dict carries datetimes, floats, nested image lists.
    JSON serialization must round-trip them all without dropping fields."""
    captured: dict[str, str] = {}

    async def fake_set(key, value, ex=None):
        captured[key] = value

    async def fake_get(key):
        return captured.get(key)

    redis = MagicMock()
    redis.set = AsyncMock(side_effect=fake_set)
    redis.get = AsyncMock(side_effect=fake_get)

    task = {
        "task_id": "t1",
        "status": "completed",
        "progress": 100,
        "prompt": "a cat at sunset",
        "model_id": "gemini-3.1-flash-image-preview",
        "provider": "google",
        "images": [
            {"url": "data:image/png;base64,Zm9v", "width": 1024, "height": 1024},
        ],
        "duration_ms": 1234.56,
        "error": None,
        "created_at": "2026-04-27T10:30:00+00:00",
        "completed_at": "2026-04-27T10:30:02+00:00",
    }

    await _store_task(redis, "t1", task)
    result = await _load_task(redis, "t1")

    assert result == task


@pytest.mark.asyncio
async def test_load_prefers_dict_after_redis_write_failure_then_recovery():
    """Reproduces stale-Redis scenario:
    1. Successful Redis write -> 'pending'
    2. Redis write fails -> dict gets 'running'
    3. Redis read works again -> returns OLD 'pending'
    4. _load_task must return 'running' (from dict), not 'pending' (from Redis)
    """
    redis = MagicMock()
    redis.set = AsyncMock(side_effect=[None, ConnectionError("boom")])
    redis.get = AsyncMock(return_value=json.dumps({"status": "pending", "task_id": "t1"}))

    # Step 1: first write succeeds -> Redis has pending
    await _store_task(redis, "t1", {"status": "pending", "task_id": "t1"})
    # Step 2: second write fails -> dict has running
    await _store_task(redis, "t1", {"status": "running", "task_id": "t1"})
    # Step 3: load — Redis returns stale pending, dict has fresh running
    result = await _load_task(redis, "t1")
    assert result["status"] == "running", "must prefer fallback dict over stale Redis"
