from __future__ import annotations

import asyncio

import pytest
from starlette.requests import Request
from starlette.responses import Response

from src.adapters.langgraph_proxy import LangGraphProxy
from src.core.middleware.request_logging import RequestLogConfig, RequestLoggingMiddleware


@pytest.mark.asyncio
async def test_request_log_task_is_retained_until_writer_finishes():
    started = asyncio.Event()
    release = asyncio.Event()

    async def writer(_log_data):
        started.set()
        await release.wait()

    middleware = RequestLoggingMiddleware(
        app=lambda *_args, **_kwargs: None,
        config=RequestLogConfig(exclude_paths=[]),
        log_writer=writer,
    )
    request = Request(
        {
            "type": "http",
            "asgi": {"version": "3.0"},
            "http_version": "1.1",
            "method": "GET",
            "scheme": "http",
            "path": "/audit",
            "raw_path": b"/audit",
            "query_string": b"",
            "root_path": "",
            "headers": [],
            "client": ("127.0.0.1", 1234),
            "server": ("testserver", 80),
        }
    )

    async def call_next(_request):
        return Response("ok")

    await middleware.dispatch(request, call_next)
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
