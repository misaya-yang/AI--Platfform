from __future__ import annotations

import pytest
from fastapi import HTTPException

from src.api.v1.proxy import check_proxy_rate_limit
from src.core.auth.user_resolver import UserContext
from src.core.gateway.multi_dimension_rate_limiter import RateLimitResult
from src.proxy.config_loader import ProxyServiceConfig


class _FakeRateLimiter:
    def __init__(self, *, custom_result: RateLimitResult, global_result: RateLimitResult):
        self.custom_result = custom_result
        self.global_result = global_result
        self.custom_calls = []
        self.global_calls = []

    async def check_custom_limit(self, **kwargs):
        self.custom_calls.append(kwargs)
        return self.custom_result

    async def check(self, context):
        self.global_calls.append(context)
        return self.global_result


def _make_user() -> UserContext:
    return UserContext(
        user_id="user_1",
        tenant_id="tenant_1",
        tier="normal",
        is_authenticated=True,
        roles=["user"],
        ip="127.0.0.1",
    )


def _make_service_config(enabled: bool) -> ProxyServiceConfig:
    return ProxyServiceConfig(
        service_id="imam",
        service_name="imam",
        upstream_url="http://localhost:2024",
        rate_limit_enabled=enabled,
        rate_limit_requests=5,
        rate_limit_window=60,
    )


@pytest.mark.asyncio
async def test_service_rate_limit_override_takes_precedence() -> None:
    limiter = _FakeRateLimiter(
        custom_result=RateLimitResult(allowed=True, dimension="service:imam"),
        global_result=RateLimitResult(allowed=True, dimension="user"),
    )
    await check_proxy_rate_limit(
        user=_make_user(),
        rate_limiter=limiter,
        service_name="imam",
        operation="run_wait",
        service_config=_make_service_config(True),
    )
    assert len(limiter.custom_calls) == 1
    assert len(limiter.global_calls) == 0
    assert "tenant_1" in limiter.custom_calls[0]["key"]


@pytest.mark.asyncio
async def test_global_rate_limit_used_when_service_override_disabled() -> None:
    limiter = _FakeRateLimiter(
        custom_result=RateLimitResult(allowed=True, dimension="service:imam"),
        global_result=RateLimitResult(allowed=True, dimension="user"),
    )
    await check_proxy_rate_limit(
        user=_make_user(),
        rate_limiter=limiter,
        service_name="imam",
        operation="run_wait",
        service_config=_make_service_config(False),
    )
    assert len(limiter.custom_calls) == 0
    assert len(limiter.global_calls) == 1


@pytest.mark.asyncio
async def test_service_rate_limit_rejects_with_429() -> None:
    limiter = _FakeRateLimiter(
        custom_result=RateLimitResult(
            allowed=False,
            dimension="service:imam",
            limit=5,
            remaining=0,
            reset_at=9999999999,
            retry_after=30,
        ),
        global_result=RateLimitResult(allowed=True),
    )
    with pytest.raises(HTTPException) as exc:
        await check_proxy_rate_limit(
            user=_make_user(),
            rate_limiter=limiter,
            service_name="imam",
            operation="run_wait",
            service_config=_make_service_config(True),
        )
    assert exc.value.status_code == 429
    assert exc.value.headers["Retry-After"] == "30"
    assert exc.value.headers["X-RateLimit-Limit"] == "5"
    assert exc.value.detail["error"]["code"] == "RATE_LIMIT_EXCEEDED"
