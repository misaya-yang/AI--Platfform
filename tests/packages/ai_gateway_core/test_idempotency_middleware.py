from __future__ import annotations

import asyncio

import httpx
import pytest
from ai_gateway_core.comm.idempotency import IdempotencyMiddleware, InMemoryIdempotencyStore
from starlette.applications import Starlette
from starlette.responses import JSONResponse, StreamingResponse
from starlette.routing import Route


@pytest.mark.asyncio
async def test_idempotency_middleware_replays_cached_response() -> None:
    calls = 0

    async def endpoint(_request):
        nonlocal calls
        calls += 1
        return JSONResponse({"calls": calls}, headers={"x-result": "fresh"})

    app = Starlette(routes=[Route("/work", endpoint, methods=["POST"])])
    app.add_middleware(
        IdempotencyMiddleware,
        store=InMemoryIdempotencyStore(),
        ttl_seconds=60,
    )
    transport = httpx.ASGITransport(app=app)

    async with httpx.AsyncClient(transport=transport, base_url="http://test") as client:
        first = await client.post("/work", headers={"Idempotency-Key": "idem-1"})
        second = await client.post("/work", headers={"Idempotency-Key": "idem-1"})

    assert first.json() == {"calls": 1}
    assert second.json() == {"calls": 1}
    assert second.headers["x-idempotency-replayed"] == "true"
    assert calls == 1


@pytest.mark.asyncio
async def test_idempotency_middleware_waits_for_concurrent_owner() -> None:
    calls = 0
    gate = asyncio.Event()

    async def endpoint(_request):
        nonlocal calls
        calls += 1
        await gate.wait()
        return JSONResponse({"calls": calls})

    app = Starlette(routes=[Route("/work", endpoint, methods=["POST"])])
    app.add_middleware(
        IdempotencyMiddleware,
        store=InMemoryIdempotencyStore(),
        ttl_seconds=60,
        wait_timeout_seconds=1.0,
        wait_poll_seconds=0.01,
    )
    transport = httpx.ASGITransport(app=app)

    async with httpx.AsyncClient(transport=transport, base_url="http://test") as client:
        first_task = asyncio.create_task(
            client.post("/work", headers={"Idempotency-Key": "idem-concurrent"})
        )
        await asyncio.sleep(0.05)
        second_task = asyncio.create_task(
            client.post("/work", headers={"Idempotency-Key": "idem-concurrent"})
        )
        gate.set()
        first, second = await asyncio.gather(first_task, second_task)

    assert first.json() == {"calls": 1}
    assert second.json() == {"calls": 1}
    assert calls == 1


@pytest.mark.asyncio
async def test_idempotency_middleware_does_not_cache_streaming_response() -> None:
    calls = 0

    async def endpoint(_request):
        nonlocal calls
        calls += 1

        async def chunks():
            yield f"data: {calls}\n\n".encode()

        return StreamingResponse(chunks(), media_type="text/event-stream")

    app = Starlette(routes=[Route("/stream", endpoint, methods=["POST"])])
    app.add_middleware(
        IdempotencyMiddleware,
        store=InMemoryIdempotencyStore(),
        ttl_seconds=60,
    )
    transport = httpx.ASGITransport(app=app)

    async with httpx.AsyncClient(transport=transport, base_url="http://test") as client:
        first = await client.post("/stream", headers={"Idempotency-Key": "idem-stream"})
        second = await client.post("/stream", headers={"Idempotency-Key": "idem-stream"})

    assert first.text == "data: 1\n\n"
    assert second.text == "data: 2\n\n"
    assert calls == 2


@pytest.mark.asyncio
async def test_idempotency_middleware_scopes_key_by_route() -> None:
    calls = {"a": 0, "b": 0}

    async def endpoint_a(_request):
        calls["a"] += 1
        return JSONResponse({"route": "a", "calls": calls["a"]})

    async def endpoint_b(_request):
        calls["b"] += 1
        return JSONResponse({"route": "b", "calls": calls["b"]})

    app = Starlette(
        routes=[
            Route("/a", endpoint_a, methods=["POST"]),
            Route("/b", endpoint_b, methods=["POST"]),
        ]
    )
    app.add_middleware(
        IdempotencyMiddleware,
        store=InMemoryIdempotencyStore(),
        ttl_seconds=60,
    )
    transport = httpx.ASGITransport(app=app)

    async with httpx.AsyncClient(transport=transport, base_url="http://test") as client:
        first = await client.post("/a", headers={"Idempotency-Key": "idem-shared"})
        second = await client.post("/b", headers={"Idempotency-Key": "idem-shared"})

    assert first.json()["route"] == "a"
    assert second.json()["route"] == "b"
    assert "x-idempotency-replayed" not in second.headers
    assert calls == {"a": 1, "b": 1}


@pytest.mark.asyncio
async def test_idempotency_middleware_does_not_cache_5xx_response() -> None:
    calls = 0

    async def endpoint(_request):
        nonlocal calls
        calls += 1
        if calls == 1:
            return JSONResponse({"temporary": True}, status_code=503)
        return JSONResponse({"calls": calls})

    app = Starlette(routes=[Route("/work", endpoint, methods=["POST"])])
    app.add_middleware(
        IdempotencyMiddleware,
        store=InMemoryIdempotencyStore(),
        ttl_seconds=60,
    )
    transport = httpx.ASGITransport(app=app)

    async with httpx.AsyncClient(transport=transport, base_url="http://test") as client:
        first = await client.post("/work", headers={"Idempotency-Key": "idem-5xx"})
        second = await client.post("/work", headers={"Idempotency-Key": "idem-5xx"})

    assert first.status_code == 503
    assert second.status_code == 200
    assert second.json() == {"calls": 2}
    assert "x-idempotency-replayed" not in second.headers
