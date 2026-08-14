from __future__ import annotations

import asyncio

import pytest

from src.adapters.langgraph_proxy import LangGraphProxy


@pytest.mark.asyncio
async def test_langgraph_cache_invalidation_task_is_retained_until_delete_finishes():
    started = asyncio.Event()
    release = asyncio.Event()

    class FakeRedis:
        enabled = True

        async def delete(self, _key):
            started.set()
            await release.wait()

    proxy = LangGraphProxy(load_balancer=object(), redis_client=FakeRedis())

    proxy._invalidate_assistant_cache("assistant-1")
    await asyncio.wait_for(started.wait(), timeout=1)
    assert len(proxy._background_tasks) == 1

    pending = tuple(proxy._background_tasks)
    release.set()
    await asyncio.gather(*pending)
    await asyncio.sleep(0)

    assert not proxy._background_tasks
