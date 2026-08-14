from __future__ import annotations

import asyncio

import pytest

from src.adapters.langgraph_proxy import LangGraphProxy
from src.core.middleware._streaming import logging as streaming_logging_module
from src.core.middleware.streaming import StreamingLogConfig, StreamingLoggingMiddleware


@pytest.mark.asyncio
async def test_streaming_metrics_task_is_retained_until_recording_finishes(monkeypatch):
    started = asyncio.Event()
    release = asyncio.Event()

    class Recorder:
        async def record_request(self, **_kwargs):
            started.set()
            await release.wait()

    monkeypatch.setattr(streaming_logging_module, "get_metrics_recorder", Recorder)
    middleware = StreamingLoggingMiddleware(
        app=lambda *_args, **_kwargs: None,
        config=StreamingLogConfig(),
    )

    middleware._schedule_metrics_record(
        method="GET",
        path="/v1/models",
        status_code=200,
        duration_ms=1.0,
        user_id="user-a",
        service_id="gateway",
        error_label="metrics failed",
    )
    await asyncio.wait_for(started.wait(), timeout=1)
    assert len(middleware._background_tasks) == 1

    pending = tuple(middleware._background_tasks)
    release.set()
    await asyncio.gather(*pending)
    await asyncio.sleep(0)

    assert not middleware._background_tasks


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
