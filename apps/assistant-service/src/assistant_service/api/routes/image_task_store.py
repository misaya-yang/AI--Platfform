"""Redis/DB image task state with an in-process development fallback."""

from __future__ import annotations

import asyncio
import json
import logging
from datetime import datetime, timezone

from ai_gateway_core.image import update_image_task
from ai_gateway_core.logging import record_internal_exception

logger = logging.getLogger("assistant_service.api.routes.images")

_image_tasks: dict[str, dict] = {}
_MAX_TASKS = 500
_TASK_TTL_SECONDS = 3600
_TASK_KEY_PREFIX = "image_task:"

# Strong refs to in-flight async-generation workers. ``asyncio.create_task``
# only weak-references the task; without this set the task can be GC'd
# mid-execution under load. Each worker self-removes via ``done_callback``.
_in_flight_workers: set[asyncio.Task[None]] = set()


def _cleanup_old_tasks() -> None:
    """Evict completed/failed in-process tasks older than the TTL.

    No-op for the Redis backend (Redis ``EX`` handles expiry)."""
    if len(_image_tasks) < _MAX_TASKS:
        return
    now = datetime.now(timezone.utc)
    to_remove = []
    for tid, task in _image_tasks.items():
        if task["status"] in ("completed", "failed"):
            created = datetime.fromisoformat(task["created_at"])
            if (now - created).total_seconds() > _TASK_TTL_SECONDS:
                to_remove.append(tid)
    for tid in to_remove:
        _image_tasks.pop(tid, None)


async def _store_task(redis, task_id: str, task: dict, *, pool=None) -> None:
    """Persist ``task`` under ``task_id``. Uses Redis when present, else the
    in-process dict. Always refreshes the TTL on Redis writes — completed
    tasks then expire 1h after completion regardless of when submitted.

    Recovery: when a Redis write succeeds after a previous fallback, we
    proactively pop the dict entry. Without this, ``_load_task`` keeps
    preferring the now-stale dict value forever (it's only ever cleared
    by the size-cap GC). After recovery, Redis is again the authoritative
    source.
    """
    if redis is not None:
        try:
            await redis.set(
                _TASK_KEY_PREFIX + task_id,
                json.dumps(task, default=str),
                ex=_TASK_TTL_SECONDS,
            )
            # Redis is now authoritative — drop any stale fallback entry.
            _image_tasks.pop(task_id, None)
            await _persist_task_state(pool, task)
            return
        except Exception as exc:
            record_internal_exception(
                __name__, "assistant.api.routes.image_task_store.internal_failure", exc
            )
    _image_tasks[task_id] = task
    await _persist_task_state(pool, task)


async def _load_task(redis, task_id: str) -> dict | None:
    """Look up a task by id. Tries Redis first, falls back to dict.

    The fallback dict is preferred over a successful Redis read when both
    have an entry: the dict is only populated when a prior write failed to
    Redis, so its presence means it holds the freshest known state and
    Redis may be stale after recovery."""
    redis_task: dict | None = None
    if redis is not None:
        try:
            raw = await redis.get(_TASK_KEY_PREFIX + task_id)
            if raw:
                redis_task = json.loads(raw)
        except Exception as exc:
            record_internal_exception(
                __name__, "assistant.api.routes.image_task_store.internal_failure", exc
            )
    # Fallback dict wins when present — it's only populated on Redis write
    # failure so it's authoritative over a possibly-stale Redis value.
    if task_id in _image_tasks:
        return _image_tasks[task_id]
    return redis_task


async def _persist_task_state(pool, task: dict, *, lock_seconds: int | None = None) -> None:
    try:
        await update_image_task(
            pool,
            task_id=task["task_id"],
            status=task.get("status"),
            progress=task.get("progress"),
            provider=task.get("provider"),
            result=task,
            error=task.get("error"),
            error_code=task.get("error_code"),
            parent_artifact_id=task.get("parent_artifact_id"),
            output_artifact_id=task.get("output_artifact_id"),
            locked_seconds=lock_seconds,
        )
    except Exception as exc:
        record_internal_exception(
            __name__, "assistant.api.routes.image_task_store.internal_failure", exc
        )
