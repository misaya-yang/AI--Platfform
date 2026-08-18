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
async def test_idempotency_middleware_replays_only_an_identical_request_body() -> None:
    calls = 0

    async def endpoint(request):
        nonlocal calls
        calls += 1
        return JSONResponse({"calls": calls, "body": (await request.body()).decode()})

    app = Starlette(routes=[Route("/work", endpoint, methods=["POST"])])
    app.add_middleware(
        IdempotencyMiddleware,
        store=InMemoryIdempotencyStore(),
        ttl_seconds=60,
    )
    transport = httpx.ASGITransport(app=app)

    async with httpx.AsyncClient(transport=transport, base_url="http://test") as client:
        first = await client.post(
            "/work",
            content=b'{"operation":"first"}',
            headers={"Idempotency-Key": "idem-body"},
        )
        replay = await client.post(
            "/work",
            content=b'{"operation":"first"}',
            headers={"Idempotency-Key": "idem-body"},
        )
        conflict = await client.post(
            "/work",
            content=b'{"operation":"different"}',
            headers={"Idempotency-Key": "idem-body"},
        )

    assert first.status_code == 200
    assert replay.status_code == 200
    assert replay.json() == first.json()
    assert replay.headers["x-idempotency-replayed"] == "true"
    assert conflict.status_code == 409
    assert "different request body" in conflict.text
    assert calls == 1


@pytest.mark.asyncio
async def test_idempotency_middleware_rejects_oversized_body_without_calling_app() -> None:
    calls = 0

    async def endpoint(_request):
        nonlocal calls
        calls += 1
        return JSONResponse({"calls": calls})

    app = Starlette(routes=[Route("/work", endpoint, methods=["POST"])])
    app.add_middleware(
        IdempotencyMiddleware,
        store=InMemoryIdempotencyStore(),
        ttl_seconds=60,
        max_body_bytes=4,
    )
    transport = httpx.ASGITransport(app=app)

    async def oversized_chunks():
        yield b"12"
        yield b"345"

    async with httpx.AsyncClient(transport=transport, base_url="http://test") as client:
        oversized = await client.post(
            "/work",
            content=oversized_chunks(),
            headers={"Idempotency-Key": "idem-too-large"},
        )
        retry = await client.post(
            "/work",
            content=b"1234",
            headers={"Idempotency-Key": "idem-too-large"},
        )

    assert oversized.status_code == 413
    assert "too large" in oversized.text
    assert retry.status_code == 200
    assert retry.json() == {"calls": 1}
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
            await asyncio.sleep(0.01)
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


@pytest.mark.asyncio
async def test_idempotency_middleware_aborts_owned_key_when_request_is_cancelled() -> None:
    store = InMemoryIdempotencyStore()

    async def cancelled_app(_scope, _receive, _send) -> None:
        raise asyncio.CancelledError

    middleware = IdempotencyMiddleware(cancelled_app, store=store, ttl_seconds=60)
    scope = {
        "type": "http",
        "method": "POST",
        "path": "/work",
        "headers": [(b"idempotency-key", b"cancelled-owner")],
    }
    received = False

    async def receive():
        nonlocal received
        if received:
            return {"type": "http.disconnect"}
        received = True
        return {"type": "http.request", "body": b"{}", "more_body": False}

    async def send(_message) -> None:
        return None

    with pytest.raises(asyncio.CancelledError):
        await middleware(scope, receive, send)

    assert store._records == {}


@pytest.mark.asyncio
async def test_idempotency_middleware_stops_reading_after_client_disconnect() -> None:
    app_called = False
    receive_calls = 0

    async def app(_scope, _receive, _send) -> None:
        nonlocal app_called
        app_called = True

    middleware = IdempotencyMiddleware(app, store=InMemoryIdempotencyStore())
    scope = {
        "type": "http",
        "method": "POST",
        "path": "/work",
        "headers": [(b"idempotency-key", b"disconnected-client")],
    }

    async def receive():
        nonlocal receive_calls
        receive_calls += 1
        return {"type": "http.disconnect"}

    async def send(_message) -> None:
        raise AssertionError("a disconnected client must not receive a response")

    await asyncio.wait_for(middleware(scope, receive, send), timeout=0.1)

    assert receive_calls == 1
    assert app_called is False
