from __future__ import annotations

import asyncio
import contextlib
import json
import logging
import uuid
from collections.abc import Awaitable, Callable
from typing import Any

from redis.asyncio import Redis

logger = logging.getLogger(__name__)


class TaskQueue:
    """Simple Redis-based async task queue."""

    def __init__(self, redis: Redis, queue_name: str = "gateway:tasks"):
        self.redis = redis
        self.queue_name = queue_name
        self._handlers: dict[str, Callable[[dict[str, Any]], Awaitable[None]]] = {}
        self._running = False
        self._worker_task: asyncio.Task | None = None

    def register_handler(
        self, task_type: str, handler: Callable[[dict[str, Any]], Awaitable[None]]
    ):
        """Register a handler for a task type."""
        self._handlers[task_type] = handler

    async def enqueue(self, task_type: str, payload: dict[str, Any]) -> str:
        """Enqueue a task."""
        task_id = str(uuid.uuid4())
        task = {
            "id": task_id,
            "type": task_type,
            "payload": payload,
            "attempts": 0,
        }
        await self.redis.lpush(self.queue_name, json.dumps(task))
        logger.info(f"Task enqueued: {task_type} ({task_id})")
        return task_id

    async def start_worker(self):
        """Start the background worker."""
        if self._running:
            return
        self._running = True
        self._worker_task = asyncio.create_task(self._worker_loop())
        logger.info(f"Task worker started for queue: {self.queue_name}")

    async def stop_worker(self):
        """Stop the background worker."""
        self._running = False
        if self._worker_task:
            self._worker_task.cancel()
            with contextlib.suppress(asyncio.CancelledError):
                await self._worker_task
        logger.info("Task worker stopped")

    async def _worker_loop(self):
        """Main worker loop."""
        while self._running:
            try:
                # BRPOP blocks until item is available
                # Use a timeout to allow checking self._running periodically
                result = await self.redis.brpop(self.queue_name, timeout=5)

                if not result:
                    continue

                _, task_json = result
                try:
                    task = json.loads(task_json)
                    await self._process_task(task)
                except json.JSONDecodeError:
                    logger.error(f"Failed to decode task: {task_json}")
                except Exception as e:
                    logger.error(f"Error processing task: {e}", exc_info=True)

            except asyncio.CancelledError:
                break
            except Exception as e:
                logger.error(f"Worker loop error: {e}")
                await asyncio.sleep(1)  # Backoff on redis errors

    async def _process_task(self, task: dict[str, Any]):
        """Process a single task."""
        task_type = task.get("type")
        handler = self._handlers.get(task_type)

        if not handler:
            logger.error(f"No handler for task type: {task_type}")
            return

        task_id = task.get("id")
        logger.info(f"Processing task: {task_type} ({task_id})")

        try:
            await handler(task.get("payload", {}))
            logger.info(f"Task completed: {task_type} ({task_id})")
        except Exception as e:
            logger.error(f"Task failed: {task_type} ({task_id}) - {e}", exc_info=True)
            # Simple retry logic could go here (e.g. re-enqueue with attempts + 1)
