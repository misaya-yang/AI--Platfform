from __future__ import annotations

import asyncio

import pytest

from src.core.gateway.admission import CapacityAdmissionController, CapacityRejected
from src.core.gateway.capacity import CapacityBudget


def _budget(
    key: str = "gateway.total_inflight",
    *,
    limit: int = 1,
    queue_max: int = 1,
    queue_timeout_ms: int = 200,
) -> CapacityBudget:
    return CapacityBudget(
        key=key,
        limit=limit,
        queue_max=queue_max,
        queue_timeout_ms=queue_timeout_ms,
        scope="gateway",
        source="test",
        enforced=True,
    )


@pytest.mark.asyncio
async def test_second_request_waits_until_capacity_is_released():
    controller = CapacityAdmissionController()
    budget = _budget(limit=1, queue_max=1, queue_timeout_ms=500)

    first = await controller.acquire(
        budgets=[budget],
        tenant_id="tenant-a",
        user_id="user-a",
        service_id="svc",
        request_class="sync",
        request_id="req-1",
    )
    blocked = asyncio.create_task(
        controller.acquire(
            budgets=[budget],
            tenant_id="tenant-a",
            user_id="user-b",
            service_id="svc",
            request_class="sync",
            request_id="req-2",
        )
    )
    await asyncio.sleep(0.03)
    assert not blocked.done()

    await first.release()
    second = await blocked
    assert second.queue_wait_ms > 0
    await second.release()


@pytest.mark.asyncio
async def test_capacity_rejection_has_stable_503_headers_and_code():
    controller = CapacityAdmissionController()
    budget = _budget(key="upstream.langgraph_agent", limit=1, queue_max=0)

    first = await controller.acquire(
        budgets=[budget],
        tenant_id="tenant-a",
        user_id="user-a",
        service_id="local-2024-agent",
        request_class="sync",
        request_id="req-1",
    )

    with pytest.raises(CapacityRejected) as exc_info:
        await controller.acquire(
            budgets=[budget],
            tenant_id="tenant-a",
            user_id="user-b",
            service_id="local-2024-agent",
            request_class="sync",
            request_id="req-2",
        )

    rejection = exc_info.value
    assert rejection.status_code == 503
    assert rejection.code == "GATEWAY_CAPACITY_EXHAUSTED"
    assert rejection.headers["X-Gateway-Capacity-Key"] == "upstream.langgraph_agent"
    assert "Retry-After" in rejection.headers
    assert "X-Gateway-Queue-Wait-Ms" in rejection.headers

    await first.release()


@pytest.mark.asyncio
async def test_capacity_context_releases_on_cancellation_path():
    controller = CapacityAdmissionController()
    budget = _budget(limit=1, queue_max=0)

    with pytest.raises(asyncio.CancelledError):
        async with await controller.acquire(
            budgets=[budget],
            tenant_id="tenant-a",
            user_id="user-a",
            service_id="svc",
            request_class="stream",
            request_id="req-stream",
        ):
            assert controller.snapshot()["gateway.total_inflight"]["inflight"] == 1
            raise asyncio.CancelledError()

    assert controller.snapshot()["gateway.total_inflight"]["inflight"] == 0
