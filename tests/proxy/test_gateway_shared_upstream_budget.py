from __future__ import annotations

import pytest

from src.core.gateway.admission import CapacityAdmissionController, CapacityRejected
from src.core.gateway.capacity import CapacityBudget


class FakeRedisBudget:
    def __init__(self, *, fail: bool = False) -> None:
        self.fail = fail
        self.zsets: dict[str, dict[str, float]] = {}

    async def zremrangebyscore(self, key: str, minimum: float, maximum: float) -> int:
        if self.fail:
            raise ConnectionError("redis unavailable")
        zset = self.zsets.setdefault(key, {})
        expired = [member for member, score in zset.items() if minimum <= score <= maximum]
        for member in expired:
            zset.pop(member, None)
        return len(expired)

    async def zcard(self, key: str) -> int:
        if self.fail:
            raise ConnectionError("redis unavailable")
        return len(self.zsets.setdefault(key, {}))

    async def zadd(self, key: str, mapping: dict[str, float]) -> int:
        if self.fail:
            raise ConnectionError("redis unavailable")
        self.zsets.setdefault(key, {}).update(mapping)
        return len(mapping)

    async def zrem(self, key: str, member: str) -> int:
        if self.fail:
            raise ConnectionError("redis unavailable")
        existed = member in self.zsets.setdefault(key, {})
        self.zsets[key].pop(member, None)
        return 1 if existed else 0


def _shared_budget() -> CapacityBudget:
    return CapacityBudget(
        key="upstream.langgraph_agent",
        limit=4,
        queue_max=0,
        queue_timeout_ms=100,
        scope="upstream",
        source="default",
        enforced=True,
        shared=True,
    )


@pytest.mark.asyncio
async def test_two_gateway_instances_share_one_upstream_redis_budget():
    redis = FakeRedisBudget()
    first_gateway = CapacityAdmissionController(
        redis_client=redis,
        gateway_instance_id="gw-a",
        cluster_epoch="uat-2026-05",
    )
    second_gateway = CapacityAdmissionController(
        redis_client=redis,
        gateway_instance_id="gw-b",
        cluster_epoch="uat-2026-05",
    )
    budget = _shared_budget()

    leases = []
    for index in range(4):
        controller = first_gateway if index % 2 == 0 else second_gateway
        leases.append(
            await controller.acquire(
                budgets=[budget],
                tenant_id="tenant-a",
                user_id=f"user-{index}",
                service_id="local-2024-agent",
                request_class="sync",
                request_id=f"req-{index}",
            )
        )

    with pytest.raises(CapacityRejected):
        await second_gateway.acquire(
            budgets=[budget],
            tenant_id="tenant-a",
            user_id="user-over",
            service_id="local-2024-agent",
            request_class="sync",
            request_id="req-over",
        )

    await leases[0].release()
    admitted_after_release = await second_gateway.acquire(
        budgets=[budget],
        tenant_id="tenant-a",
        user_id="user-next",
        service_id="local-2024-agent",
        request_class="sync",
        request_id="req-next",
    )
    await admitted_after_release.release()
    for lease in leases[1:]:
        await lease.release()


@pytest.mark.asyncio
async def test_redis_unavailable_fails_closed_for_shared_sync_budget():
    controller = CapacityAdmissionController(
        redis_client=FakeRedisBudget(fail=True),
        gateway_instance_id="gw-a",
    )

    with pytest.raises(CapacityRejected) as exc_info:
        await controller.acquire(
            budgets=[_shared_budget()],
            tenant_id="tenant-a",
            user_id="user-a",
            service_id="local-2024-agent",
            request_class="sync",
            request_id="req-fail",
        )

    assert exc_info.value.status_code == 503
    assert exc_info.value.code == "GATEWAY_CAPACITY_DEGRADED"
