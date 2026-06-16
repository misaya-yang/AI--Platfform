import pytest

from src.core.tasks.queue import TaskQueue


@pytest.mark.asyncio
async def test_start_worker_skips_when_no_handlers():
    queue = TaskQueue(redis=object())

    await queue.start_worker()

    assert queue._worker_task is None
    assert queue._running is False
