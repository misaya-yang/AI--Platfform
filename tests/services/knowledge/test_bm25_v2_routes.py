from __future__ import annotations

from types import SimpleNamespace

import pytest
from fastapi import HTTPException
from knowledge_service.api.routes.bm25_v2 import (
    Bm25CutoverSchema,
    Bm25RollbackSchema,
    cutover_bm25_v2,
    dry_run_bm25_v2_cutover,
    get_bm25_v2_state,
    rollback_bm25_v2,
    verify_bm25_v2,
)
from knowledge_service.auth.user_context import UserContext


class Lifecycle:
    def __init__(self) -> None:
        self.calls: list[tuple] = []

    async def get_lifecycle_state(self, dataset_id: str):
        self.calls.append(("get", dataset_id))
        return {"dataset_id": dataset_id, "state": "shadow"}

    async def verify_cross_authority(self, dataset_id: str):
        self.calls.append(("verify", dataset_id))
        return {"dataset_id": dataset_id, "agreement": True}

    async def cutover(self, dataset_id: str, **kwargs):
        self.calls.append(("cutover", dataset_id, kwargs))
        return {"dataset_id": dataset_id, "applied": kwargs["apply"]}

    async def rollback(self, dataset_id: str, **kwargs):
        self.calls.append(("rollback", dataset_id, kwargs))
        return {"dataset_id": dataset_id, "applied": kwargs["apply"]}


class Service:
    def __init__(self, *, allowlist: str = "") -> None:
        self.bm25_v2_lifecycle_service = Lifecycle()
        self.settings = SimpleNamespace(
            knowledge=SimpleNamespace(
                qdrant=SimpleNamespace(bm25_v2_cutover_test_tenants=allowlist)
            )
        )

    async def require_dataset_access(self, user, dataset_id: str, *, required: str):
        assert required == "owner"
        assert user.tenant_id == "tenant-a"
        return {"dataset_id": dataset_id, "tenant_id": "tenant-a"}


OWNER = UserContext(user_id="owner-a", tenant_id="tenant-a")
OPERATOR = UserContext(
    user_id="operator-a",
    tenant_id="tenant-a",
    user_tier="admin",
    roles=["operator"],
)


@pytest.mark.asyncio
async def test_owner_can_inspect_verify_and_dry_run_but_not_cut_over() -> None:
    svc = Service(allowlist="tenant-a")
    state = await get_bm25_v2_state("dataset-a", svc=svc, user=OWNER)
    verify = await verify_bm25_v2("dataset-a", svc=svc, user=OWNER)
    dry_run = await dry_run_bm25_v2_cutover(
        "dataset-a",
        body=Bm25CutoverSchema(),
        svc=svc,
        user=OWNER,
    )
    assert state["release_status"] == "BLOCKED"
    assert verify["agreement"] is True
    assert dry_run == {
        "dataset_id": "dataset-a",
        "applied": False,
        "release_status": "BLOCKED",
    }

    with pytest.raises(HTTPException) as error:
        await cutover_bm25_v2(
            "dataset-a",
            body=Bm25CutoverSchema(),
            svc=svc,
            user=OWNER,
        )
    assert error.value.status_code == 403


@pytest.mark.asyncio
async def test_operator_cutover_is_test_allowlisted_but_rollback_is_always_available() -> None:
    blocked = Service(allowlist="tenant-b")
    with pytest.raises(HTTPException) as error:
        await cutover_bm25_v2(
            "dataset-a",
            body=Bm25CutoverSchema(),
            svc=blocked,
            user=OPERATOR,
        )
    assert error.value.status_code == 403
    assert blocked.bm25_v2_lifecycle_service.calls == []

    allowed = Service(allowlist="tenant-a, tenant-b")
    result = await cutover_bm25_v2(
        "dataset-a",
        body=Bm25CutoverSchema(),
        svc=allowed,
        user=OPERATOR,
    )
    assert result["release_status"] == "BLOCKED"
    assert result["scope"] == "test_tenant_only"

    # Rollback intentionally ignores the release allowlist and kill switch.
    rollback = await rollback_bm25_v2(
        "dataset-a",
        body=Bm25RollbackSchema(keep_shadow_writes=False),
        svc=blocked,
        user=OPERATOR,
    )
    assert rollback["applied"] is True
    assert blocked.bm25_v2_lifecycle_service.calls == [
        (
            "rollback",
            "dataset-a",
            {"apply": True, "keep_shadow_writes": False},
        )
    ]
