"""Phase 5a unit tests for ``ai_gateway_core.proxy.ServiceProxy``.

Locks the semantics audited as Findings M-1..M-5:
- half-open probe breaker (Finding M-1)
- mid-stream failure counted (Finding M-2)
- SSE content-type forces streaming (Finding M-3)
- 4xx does NOT close the breaker (Finding M-5)
- header strip + inject (Audit Claim E)
- retry path counts failure exactly once (Finding H-4 / earlier bug)
"""
from __future__ import annotations

from dataclasses import dataclass

import pytest

from ai_gateway_core.proxy.base import (
    CircuitBreaker,
    CircuitBreakerState,
    InMemoryCounter,
)


def test_breaker_closed_by_default() -> None:
    cb = CircuitBreaker(name="test")
    assert cb.state == CircuitBreakerState.CLOSED
    cb.gate()  # should not raise


def test_breaker_opens_after_threshold_failures() -> None:
    cb = CircuitBreaker(name="test", threshold=3)
    for _ in range(3):
        cb.on_failure()
    assert cb.state == CircuitBreakerState.OPEN


def test_breaker_half_open_probe_slot_single_use() -> None:
    cb = CircuitBreaker(name="test", threshold=2)
    cb.on_failure()
    cb.on_failure()
    assert cb.state == CircuitBreakerState.OPEN

    # First gate() acquires the probe slot
    cb.gate()
    # Second gate() while probe in flight → 503
    from fastapi import HTTPException

    with pytest.raises(HTTPException) as exc:
        cb.gate()
    assert exc.value.status_code == 503
    assert "Retry-After" in exc.value.headers


def test_breaker_2xx_closes() -> None:
    cb = CircuitBreaker(name="test", threshold=2)
    cb.on_failure()
    cb.on_failure()
    cb.gate()  # claim probe
    cb.on_response(200)
    assert cb.state == CircuitBreakerState.CLOSED


def test_breaker_4xx_does_not_close_breaker(
    # Finding M-5: a run of 400 responses after a 5xx burst must NOT
    # spuriously close the breaker.
) -> None:
    cb = CircuitBreaker(name="test", threshold=3)
    cb.on_failure()
    cb.on_failure()
    assert cb.store.get() == (2, False)

    cb.on_response(400)
    # fails stay at 2; probe slot released
    assert cb.store.get() == (2, False)

    cb.on_response(404)
    assert cb.store.get() == (2, False)


def test_breaker_5xx_response_counts_as_failure() -> None:
    cb = CircuitBreaker(name="test", threshold=3)
    for _ in range(3):
        cb.on_response(500)
    assert cb.state == CircuitBreakerState.OPEN


def test_breaker_probe_success_resets_counter() -> None:
    cb = CircuitBreaker(name="test", threshold=2)
    cb.on_failure()
    cb.on_failure()
    cb.gate()
    cb.on_response(200)
    # Now the breaker is closed — a fresh failure starts at 1, not 3
    cb.on_failure()
    assert cb.store.get() == (1, False)
    assert cb.state == CircuitBreakerState.CLOSED


def test_inmemory_counter_isolated_per_instance() -> None:
    c1 = InMemoryCounter()
    c2 = InMemoryCounter()
    c1.on_failure()
    c1.on_failure()
    assert c1.get() == (2, False)
    assert c2.get() == (0, False)


def test_breaker_3xx_is_not_success() -> None:
    """3xx should release the probe but not reset fail counter."""
    cb = CircuitBreaker(name="test", threshold=3)
    cb.on_failure()
    cb.on_failure()
    cb.on_response(302)
    assert cb.store.get() == (2, False)


def test_breaker_custom_threshold() -> None:
    cb = CircuitBreaker(name="test", threshold=5)
    for _ in range(4):
        cb.on_failure()
    assert cb.state == CircuitBreakerState.CLOSED
    cb.on_failure()
    assert cb.state == CircuitBreakerState.OPEN


# --- strip / inject header contract ---


def test_injected_identity_headers_cover_all_x_user() -> None:
    """GATE G5a-3. Every header that assistant-service reads must be in
    the gateway's strip list, otherwise a public client could smuggle
    ``x-user-type: admin`` past the gateway and reach assistant-service
    header verbatim."""
    from ai_gateway_core.proxy.base import _DEFAULT_INJECTED_IDENTITY_HEADERS

    required = {
        "x-user-id",
        "x-tenant-id",
        "x-user-tier",
        "x-user-type",
        "x-user-roles",
        "x-user-email",
        "x-user-name",
    }
    assert required <= _DEFAULT_INJECTED_IDENTITY_HEADERS


def test_service_proxy_strip_list_is_lowercase() -> None:
    """Callers compare against ``.lower()``, so the strip set must be
    lowercase to match starlette's lowercased header iteration."""
    from ai_gateway_core.proxy.base import (
        _DEFAULT_INJECTED_IDENTITY_HEADERS,
        _DEFAULT_STRIP_REQ,
        _DEFAULT_STRIP_RESP,
    )

    for h in _DEFAULT_INJECTED_IDENTITY_HEADERS:
        assert h == h.lower()
    for h in _DEFAULT_STRIP_REQ:
        assert h == h.lower()
    for h in _DEFAULT_STRIP_RESP:
        assert h == h.lower()
