"""Behavior of the proxy-layer SSE heartbeat wrapper."""

from __future__ import annotations

import asyncio

import pytest

from ai_gateway_core.proxy.sse_heartbeat import (
    DEFAULT_HEARTBEAT_INTERVAL_S,
    with_sse_heartbeat,
)


# ----- Helpers --------------------------------------------------------------


async def _drain(it):
    out = []
    async for v in it:
        out.append(v)
    return out


# ----- Tests ----------------------------------------------------------------


async def test_default_interval_is_15s():
    assert DEFAULT_HEARTBEAT_INTERVAL_S == 15.0


async def test_passthrough_when_producer_is_fast_yields_no_heartbeats():
    async def fast_gen():
        for i in range(3):
            yield f"data: {i}\n\n".encode()

    out = await _drain(with_sse_heartbeat(fast_gen(), interval_seconds=1.0))
    assert out == [b"data: 0\n\n", b"data: 1\n\n", b"data: 2\n\n"]


async def test_idle_producer_emits_heartbeat_lines():
    async def slow_gen():
        # No production for ~3 heartbeat intervals, then one chunk.
        await asyncio.sleep(0.18)
        yield b"data: ok\n\n"

    out = await _drain(with_sse_heartbeat(slow_gen(), interval_seconds=0.05))
    heartbeats = [c for c in out if c == b": heartbeat\n\n"]
    payload = [c for c in out if c == b"data: ok\n\n"]
    assert len(heartbeats) >= 2
    assert len(payload) == 1


async def test_heartbeat_payload_matches_upstream_string_type_when_opted_in():
    async def str_gen():
        await asyncio.sleep(0.1)
        yield "data: hello\n\n"

    out = await _drain(
        with_sse_heartbeat(str_gen(), interval_seconds=0.04, as_str=True)
    )
    # Every yielded item is a str — no bytes mixed in.
    assert all(isinstance(c, str) for c in out), out
    assert ": heartbeat\n\n" in out


async def test_heartbeat_default_is_bytes():
    async def empty_gen():
        await asyncio.sleep(0.1)
        # produce nothing else
        return
        yield  # pragma: no cover  (unreachable; makes this an async generator)

    out = await _drain(with_sse_heartbeat(empty_gen(), interval_seconds=0.04))
    assert out, "expected at least one heartbeat before generator finished"
    assert all(isinstance(c, bytes) for c in out)
    assert b": heartbeat\n\n" in out


async def test_producer_exception_propagates():
    async def broken_gen():
        yield b"data: a\n\n"
        raise RuntimeError("upstream fell over")

    pieces = []
    with pytest.raises(RuntimeError, match="upstream fell over"):
        async for chunk in with_sse_heartbeat(broken_gen(), interval_seconds=1.0):
            pieces.append(chunk)
    assert pieces == [b"data: a\n\n"]


async def test_consumer_disconnect_cancels_producer_cleanly():
    cancelled = asyncio.Event()

    async def long_gen():
        try:
            for _ in range(100):
                await asyncio.sleep(0.02)
                yield b"data: x\n\n"
        except asyncio.CancelledError:
            cancelled.set()
            raise

    it = with_sse_heartbeat(long_gen(), interval_seconds=10.0)
    # Pull the first event then close the iterator (simulates client disconnect).
    first = await it.__anext__()
    assert first == b"data: x\n\n"
    await it.aclose()
    # Allow the producer task to settle.
    await asyncio.sleep(0.05)
    assert cancelled.is_set()
