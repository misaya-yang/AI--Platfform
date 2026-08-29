"""Pre-release BM25 v2 lifecycle control plane.

Read-only state, verification, and dry-run planning are dataset-owner scoped.
State-changing cutover/rollback additionally require an operator identity.
Cutover is limited to an explicit test-tenant allowlist; the production release
decision remains BLOCKED. Emergency rollback intentionally ignores both that
allowlist and the BM25 v2 kill switch.
"""

from __future__ import annotations

from typing import Any

from fastapi import APIRouter, Body, Depends, HTTPException
from pydantic import BaseModel, Field

from ...core.auth.user_resolver import UserContext
from ...core.exceptions import PermissionDeniedError, ValidationFailedError
from ...persistence.bm25_v2_lifecycle import (
    Bm25V2LifecycleDbError,
    LifecycleStateConflict,
    LifecycleTransitionBusy,
)
from ...services.knowledge.bm25_v2_lifecycle import Bm25V2LifecycleError
from ...services.knowledge.knowledge_service import KnowledgeService
from ...services.knowledge.vector_store import VectorStoreError
from ..deps import get_knowledge_service, get_user_context

router = APIRouter(tags=["Maintenance"])


class Bm25CutoverSchema(BaseModel):
    expected_manifest_sha256: str | None = Field(default=None, min_length=64, max_length=64)


class Bm25RollbackSchema(BaseModel):
    keep_shadow_writes: bool = True


def _lifecycle_service(svc: KnowledgeService | None) -> Any:
    service = getattr(svc, "bm25_v2_lifecycle_service", None) if svc else None
    if service is None:
        raise HTTPException(status_code=503, detail="BM25 v2 lifecycle store unavailable")
    return service


async def _owner_dataset(
    svc: KnowledgeService,
    user: UserContext,
    dataset_id: str,
) -> dict[str, Any]:
    try:
        return await svc.require_dataset_access(user, dataset_id, required="owner")
    except PermissionDeniedError as exc:
        raise HTTPException(status_code=403, detail=str(exc)) from exc
    except ValidationFailedError as exc:
        raise HTTPException(status_code=404, detail=str(exc)) from exc


def _require_operator(user: UserContext) -> None:
    roles = {str(role).strip().lower() for role in (user.roles or [])}
    tier = str(getattr(user, "tier", "normal") or "normal").strip().lower()
    if tier != "admin" and not roles.intersection({"admin", "operator"}):
        raise HTTPException(status_code=403, detail="BM25 v2 operator role required")


def _test_tenant_allowlist(svc: KnowledgeService) -> set[str]:
    qdrant = getattr(getattr(svc.settings, "knowledge", None), "qdrant", None)
    raw = str(getattr(qdrant, "bm25_v2_cutover_test_tenants", "") or "")
    return {value.strip() for value in raw.split(",") if value.strip()}


def _map_error(exc: Exception) -> HTTPException:
    if isinstance(exc, Bm25V2LifecycleError):
        return HTTPException(
            status_code=exc.http_status,
            detail={"code": exc.code, "message": str(exc)},
        )
    if isinstance(exc, LifecycleTransitionBusy):
        return HTTPException(status_code=503, detail=str(exc))
    if isinstance(exc, (LifecycleStateConflict, VectorStoreError)):
        return HTTPException(status_code=409, detail=str(exc))
    if isinstance(exc, Bm25V2LifecycleDbError):
        return HTTPException(status_code=503, detail=str(exc))
    return HTTPException(status_code=500, detail="BM25 v2 lifecycle operation failed")


@router.get("/knowledge/datasets/{dataset_id}/bm25-v2")
async def get_bm25_v2_state(
    dataset_id: str,
    svc: KnowledgeService = Depends(get_knowledge_service),
    user: UserContext = Depends(get_user_context),
):
    service = _lifecycle_service(svc)
    await _owner_dataset(svc, user, dataset_id)
    try:
        state = await service.get_lifecycle_state(dataset_id)
    except Exception as exc:
        raise _map_error(exc) from exc
    return {**state, "release_status": "BLOCKED"}


@router.post("/knowledge/datasets/{dataset_id}/bm25-v2/verify")
async def verify_bm25_v2(
    dataset_id: str,
    svc: KnowledgeService = Depends(get_knowledge_service),
    user: UserContext = Depends(get_user_context),
):
    service = _lifecycle_service(svc)
    await _owner_dataset(svc, user, dataset_id)
    try:
        return await service.verify_cross_authority(dataset_id)
    except Exception as exc:
        raise _map_error(exc) from exc


@router.post("/knowledge/datasets/{dataset_id}/bm25-v2/cutover/dry-run")
async def dry_run_bm25_v2_cutover(
    dataset_id: str,
    body: Bm25CutoverSchema = Body(default_factory=Bm25CutoverSchema),
    svc: KnowledgeService = Depends(get_knowledge_service),
    user: UserContext = Depends(get_user_context),
):
    service = _lifecycle_service(svc)
    await _owner_dataset(svc, user, dataset_id)
    try:
        result = await service.cutover(
            dataset_id,
            apply=False,
            expected_manifest_sha256=body.expected_manifest_sha256,
        )
    except Exception as exc:
        raise _map_error(exc) from exc
    return {**result, "release_status": "BLOCKED"}


@router.post("/knowledge/datasets/{dataset_id}/bm25-v2/cutover")
async def cutover_bm25_v2(
    dataset_id: str,
    body: Bm25CutoverSchema = Body(default_factory=Bm25CutoverSchema),
    svc: KnowledgeService = Depends(get_knowledge_service),
    user: UserContext = Depends(get_user_context),
):
    service = _lifecycle_service(svc)
    dataset = await _owner_dataset(svc, user, dataset_id)
    _require_operator(user)
    tenant_id = str(dataset.get("tenant_id") or "").strip()
    if not tenant_id or tenant_id not in _test_tenant_allowlist(svc):
        raise HTTPException(
            status_code=403,
            detail="BM25 v2 cutover is restricted to configured test tenants",
        )
    try:
        result = await service.cutover(
            dataset_id,
            apply=True,
            expected_manifest_sha256=body.expected_manifest_sha256,
        )
    except Exception as exc:
        raise _map_error(exc) from exc
    return {**result, "release_status": "BLOCKED", "scope": "test_tenant_only"}


@router.post("/knowledge/datasets/{dataset_id}/bm25-v2/rollback")
async def rollback_bm25_v2(
    dataset_id: str,
    body: Bm25RollbackSchema = Body(default_factory=Bm25RollbackSchema),
    svc: KnowledgeService = Depends(get_knowledge_service),
    user: UserContext = Depends(get_user_context),
):
    service = _lifecycle_service(svc)
    await _owner_dataset(svc, user, dataset_id)
    _require_operator(user)
    try:
        return await service.rollback(
            dataset_id,
            apply=True,
            keep_shadow_writes=body.keep_shadow_writes,
        )
    except Exception as exc:
        raise _map_error(exc) from exc
