"""Generic SSE heartbeat wrapper for streaming pass-through proxies.

Long upstream tool calls (Gemini image gen 60s+, KB queries 30s+, model
generation under contention) leave the SSE stream silent for stretches
that exceed nginx's default proxy_read_timeout, ALB idle timeout, mobile
NAT keepalive windows. The browser-side EventSource then fires
``ERR_CONNECTION_RESET`` mid-flight and the model's final response never
reaches the user.

The fix is a constant ``: heartbeat\\n\\n`` SSE comment line every N
seconds of producer silence — invisible to spec-compliant EventSource
consumers (lines starting with ``:`` are dropped per the SSE spec) but
keeps every TCP/HTTP intermediary's keepalive timer alive.

The same pattern lives directly inside
``apps/assistant-service/.../routes/chat.py`` for the AS-owned event
generator. This module is the **proxy-layer** version: it wraps an
``async generator`` of bytes (or strings) coming back from a pass-
through ``client.stream(...)`` so the gateway's SSE proxies for imam-
agent and other LangGraph upstreams get the same protection without
having to edit each upstream's code.

Usage::

    async def upstream_gen():
        async with client.stream(...) as resp:
            async for chunk in resp.aiter_raw():
                yield chunk

    return StreamingResponse(
        with_sse_heartbeat(upstream_gen(), interval_seconds=15.0),
        media_type="text/event-stream",
    )
"""

from __future__ import annotations

import asyncio
import contextlib
from collections.abc import AsyncIterator
from typing import Any

# 15s is a safe default — beats most CDN / proxy / NAT idle timeouts
# (commonly 30s–60s) by a comfortable margin without flooding the wire.
DEFAULT_HEARTBEAT_INTERVAL_S: float = 15.0

# SSE comment line. ``:`` prefix marks the line as a comment per the SSE
# spec — well-behaved EventSource clients silently discard it. The
# trailing ``\n\n`` closes the SSE event frame so any client buffer that
# accumulates by event boundary still sees activity.
_HEARTBEAT_FRAME_BYTES = b": heartbeat\n\n"
_HEARTBEAT_FRAME_STR = ": heartbeat\n\n"


async def with_sse_heartbeat(
    upstream: AsyncIterator[Any],
    *,
    interval_seconds: float = DEFAULT_HEARTBEAT_INTERVAL_S,
    as_str: bool = False,
) -> AsyncIterator[Any]:
    """Yield from ``upstream`` while injecting periodic heartbeats.

    The wrapped iterator yields:
      * Each item the upstream produces, exactly as produced.
      * A heartbeat frame every ``interval_seconds`` of producer silence.
        The frame type is bytes by default (proxy ``aiter_raw`` is the
        common caller). Pass ``as_str=True`` if your producer yields
        ``str`` chunks so heartbeats match — mixing types would corrupt
        the stream when StreamingResponse encodes.

    Cancellation: when the consumer disconnects or stops iterating, the
    producer task is cancelled cleanly. Exceptions raised inside the
    upstream propagate to the consumer.
    """
    queue: asyncio.Queue[Any] = asyncio.Queue()
    sentinel_done = object()
    heartbeat_payload: Any = _HEARTBEAT_FRAME_STR if as_str else _HEARTBEAT_FRAME_BYTES
    at_sse_frame_boundary = True
    trailing_bytes = b""
    trailing_str = ""

    def _update_frame_boundary(chunk: Any) -> None:
        nonlocal at_sse_frame_boundary, trailing_bytes, trailing_str

        if isinstance(chunk, bytes):
            trailing_bytes = (trailing_bytes + chunk)[-4:]
            at_sse_frame_boundary = trailing_bytes.endswith(b"\n\n") or trailing_bytes.endswith(
                b"\r\n\r\n"
            )
            return

        if isinstance(chunk, str):
            trailing_str = (trailing_str + chunk)[-4:]
            at_sse_frame_boundary = trailing_str.endswith("\n\n") or trailing_str.endswith(
                "\r\n\r\n"
            )

    async def _producer() -> None:
        try:
            async for chunk in upstream:
                await queue.put(chunk)
        except BaseException as exc:  # noqa: BLE001 — propagate to consumer
            await queue.put(exc)
        else:
            await queue.put(sentinel_done)

    task = asyncio.create_task(_producer())

    try:
        while True:
            try:
                item = await asyncio.wait_for(queue.get(), timeout=interval_seconds)
            except asyncio.TimeoutError:
                if at_sse_frame_boundary:
                    yield heartbeat_payload
                continue

            if item is sentinel_done:
                return
            if isinstance(item, BaseException):
                raise item

            _update_frame_boundary(item)
            yield item
    finally:
        if not task.done():
            task.cancel()
            with contextlib.suppress(asyncio.CancelledError, BaseException):
                await task


__all__ = [
    "DEFAULT_HEARTBEAT_INTERVAL_S",
    "with_sse_heartbeat",
]
