"""Graceful shutdown drain for FastAPI services.

Handles SIGTERM / SIGINT by flipping a process-singleton flag that:

1. Causes the next inbound request to be rejected with ``503 Shutting down``
   plus a ``Retry-After`` header so a load balancer / client can retry against
   another replica or back off.
2. Lets in-flight requests finish before the event loop exits, via
   ``DRAIN.wait_drained(timeout=...)`` called in the lifespan shutdown phase.

The trio of pieces:

- ``DrainState``: counter + flag + zero-event, supports ``async with`` to
  reference-count an in-flight request.
- ``DrainMiddleware``: BaseHTTPMiddleware that enters/leaves the DrainState
  context for every non-probe request.
- ``install_signal_handlers``: wires SIGTERM / SIGINT on the running event
  loop so the orchestrator's "please stop" signal flips the flag without
  needing a full asyncio task to be running.

Health probe paths (``/health``, ``/health/live``, ``/health/ready``,
``/metrics``) are excluded from drain accounting AND from the 503 short-circuit
on the request side. We still want them to answer during drain so the
load balancer's readiness check can see "not ready" (or whichever signal the
service emits) and stop routing traffic — that's what *kicks off* graceful
draining of the rest of the requests.

Usage in a FastAPI app:

```python
from ai_gateway_core.proxy.drain import (
    DRAIN,
    DrainMiddleware,
    install_signal_handlers,
)

@asynccontextmanager
async def lifespan(app):
    install_signal_handlers(asyncio.get_running_loop())
    yield
    if not await DRAIN.wait_drained(timeout=30.0):
        logger.warning("drain timeout — forcing exit with %d in-flight",
                       DRAIN.inflight)

app = FastAPI(lifespan=lifespan)
app.add_middleware(DrainMiddleware)  # add BEFORE CORSMiddleware
```
"""
from __future__ import annotations

import asyncio
import logging
import signal
import sys
from typing import Iterable

from fastapi import HTTPException
from starlette.middleware.base import BaseHTTPMiddleware
from starlette.requests import Request
from starlette.responses import JSONResponse, Response

logger = logging.getLogger(__name__)


_DEFAULT_EXCLUDE_PATHS: frozenset[str] = frozenset(
    {"/health", "/health/live", "/health/ready", "/metrics"}
)


class DrainState:
    """Process-singleton drain coordinator.

    Tracks in-flight request count behind an ``asyncio.Lock`` and exposes
    an ``asyncio.Event`` that fires whenever the counter hits zero. The
    shutdown path awaits that event with a timeout so the lifespan can
    bound the drain window.

    The class is reentrant via ``async with`` — each entry increments the
    counter, each exit decrements; the zero-event is set on the trailing
    edge (counter == 0) so a stalled wait_drained() resumes immediately.

    Concurrency note: the lock guards the counter + flag transition. The
    503-on-drain check (``_draining``) is read under the lock so a request
    that "wins the race" past begin_drain() can't slip through after the
    flag has been flipped.
    """

    def __init__(self) -> None:
        self._draining: bool = False
        self._inflight: int = 0
        self._lock: asyncio.Lock = asyncio.Lock()
        # Start as ``set`` because zero in-flight at init means "drained".
        # ``wait_drained`` would otherwise hang for a service that received
        # zero requests before shutdown.
        self._zero_event: asyncio.Event = asyncio.Event()
        self._zero_event.set()

    # ----- public state -----

    @property
    def draining(self) -> bool:
        """Whether ``begin_drain()`` has been called."""
        return self._draining

    @property
    def inflight(self) -> int:
        """Current count of in-flight requests (read without locking; safe
        for diagnostics, race-y for control flow)."""
        return self._inflight

    # ----- request lifecycle -----

    async def __aenter__(self) -> "DrainState":
        async with self._lock:
            if self._draining:
                # Reject before incrementing so the rejected request never
                # contributes to inflight (and thus never delays drain).
                raise HTTPException(
                    status_code=503,
                    detail="shutting down",
                    headers={"Retry-After": "5"},
                )
            self._inflight += 1
            # Counter went non-zero → clear the zero-event.
            self._zero_event.clear()
        return self

    async def __aexit__(self, exc_type, exc, tb) -> None:
        async with self._lock:
            self._inflight -= 1
            if self._inflight <= 0:
                self._inflight = 0
                self._zero_event.set()

    # ----- shutdown control -----

    def begin_drain(self) -> None:
        """Flip the drain flag (idempotent).

        Called by the signal handler. Subsequent ``__aenter__`` calls raise
        503. In-flight requests finish naturally; ``wait_drained`` blocks
        until the inflight counter reaches zero.
        """
        if not self._draining:
            self._draining = True
            logger.info("drain initiated — inflight=%d", self._inflight)

    async def wait_drained(self, timeout: float = 30.0) -> bool:
        """Wait for the inflight counter to reach zero.

        Returns
        -------
        bool
            True if the counter reached zero within ``timeout`` seconds.
            False on timeout — caller is expected to log and proceed with
            forced shutdown.
        """
        try:
            await asyncio.wait_for(self._zero_event.wait(), timeout=timeout)
            return True
        except asyncio.TimeoutError:
            return False

    # ----- test helpers -----

    def _reset_for_tests(self) -> None:
        """Reset state — TESTS ONLY. Production code never calls this."""
        self._draining = False
        self._inflight = 0
        self._zero_event = asyncio.Event()
        self._zero_event.set()


# Module-level process singleton. Importers reach for this directly; the
# DrainMiddleware constructor accepts an override only for unit tests.
DRAIN: DrainState = DrainState()


class DrainMiddleware(BaseHTTPMiddleware):
    """Reference-count every non-probe request via ``DRAIN``.

    Excluded paths (``/health*``, ``/metrics``) bypass DRAIN entirely so
    that probes still answer during a drain. The orchestrator's readiness
    check is what turns the load balancer off in the first place — if we
    503'd /health/ready during drain we'd race the LB into routing more
    traffic at a dying replica.
    """

    def __init__(
        self,
        app,
        *,
        drain: DrainState | None = None,
        exclude_paths: Iterable[str] | None = None,
    ) -> None:
        super().__init__(app)
        self._drain = drain if drain is not None else DRAIN
        self._exclude_paths = (
            frozenset(exclude_paths)
            if exclude_paths is not None
            else _DEFAULT_EXCLUDE_PATHS
        )

    async def dispatch(self, request: Request, call_next) -> Response:
        path = request.url.path
        if path in self._exclude_paths:
            # Probes are never counted, even during drain.
            return await call_next(request)

        try:
            async with self._drain:
                return await call_next(request)
        except HTTPException as exc:
            # ``DrainState.__aenter__`` raises HTTPException(503) when
            # drain has begun. BaseHTTPMiddleware doesn't auto-translate
            # those to a Response (it bubbles to the app's exception
            # handler), so render here to keep the 503 contract local
            # to the middleware.
            if exc.status_code == 503:
                return JSONResponse(
                    status_code=503,
                    content={"detail": exc.detail or "shutting down"},
                    headers=exc.headers or {"Retry-After": "5"},
                )
            raise


def install_signal_handlers(
    loop: asyncio.AbstractEventLoop,
    drain: DrainState | None = None,
) -> None:
    """Wire SIGTERM / SIGINT to ``drain.begin_drain()``.

    Uses ``loop.add_signal_handler`` so the flip happens on the running
    event loop without requiring a task. No-op on Windows because
    ``add_signal_handler`` is unsupported there (asyncio raises
    NotImplementedError on Proactor / Selector loops on win32).
    """
    if sys.platform == "win32":
        logger.debug("signal handlers skipped (win32)")
        return

    target = drain if drain is not None else DRAIN

    def _on_signal(sig_name: str) -> None:
        logger.info("received %s — initiating drain", sig_name)
        target.begin_drain()

    for sig in (signal.SIGTERM, signal.SIGINT):
        try:
            loop.add_signal_handler(sig, _on_signal, sig.name)
        except (NotImplementedError, RuntimeError) as exc:
            # Some environments (notebooks, threads-not-main) refuse;
            # log and keep going. The lifespan shutdown still drains
            # correctly when uvicorn delivers KeyboardInterrupt.
            logger.warning(
                "could not install %s handler: %s", sig.name, exc
            )


__all__ = [
    "DRAIN",
    "DrainState",
    "DrainMiddleware",
    "install_signal_handlers",
]
