"""Internal Agent governance cleanup routes (Gateway HMAC only)."""

from __future__ import annotations

from typing import Any, cast

from ai_gateway_core.agents import validate_runtime_cleanup_plan
from fastapi import APIRouter, Depends, HTTPException, Request
from pydantic import BaseModel, ConfigDict

from ...auth import UserContext, get_user_context
from ...core.runtime.memory.governance_cleanup import (
    AgentRuntimeMemoryCleanupService,
    RuntimeMemoryCleanupError,
)

router = APIRouter(prefix="/internal/runtime-memory-cleanup")


class RuntimeCleanupInspectRequest(BaseModel):
    model_config = ConfigDict(extra="forbid")

    plan: dict[str, Any]


class RuntimeCleanupExecuteRequest(BaseModel):
    model_config = ConfigDict(extra="forbid")

    plan: dict[str, Any]
    inventory: dict[str, Any]


def _get_cleanup_service(request: Request) -> AgentRuntimeMemoryCleanupService:
    service = getattr(request.app.state, "runtime_memory_cleanup_service", None)
    if service is None:
        raise HTTPException(
            status_code=503,
            detail={"code": "AGENT_RUNTIME_CLEANUP_UNAVAILABLE"},
        )
    return cast(AgentRuntimeMemoryCleanupService, service)


def _authorize(
    request: Request,
    user: UserContext,
    plan_value: object,
) -> dict[str, Any]:
    if getattr(request.state, "gateway_secret_verified", False) is not True:
        raise HTTPException(status_code=401, detail={"code": "AUTH_DENIED"})
    if user.user_type != "system" or "admin" not in set(user.roles or []):
        raise HTTPException(status_code=403, detail={"code": "AUTH_FORBIDDEN"})
    try:
        plan = validate_runtime_cleanup_plan(plan_value)
    except (TypeError, ValueError) as exc:
        raise HTTPException(
            status_code=422,
            detail={"code": "AGENT_RUNTIME_CLEANUP_PLAN_INVALID"},
        ) from exc
    if plan["tenant_id"] != user.tenant_id:
        raise HTTPException(
            status_code=403,
            detail={"code": "AGENT_RUNTIME_CLEANUP_TENANT_MISMATCH"},
        )
    return plan


@router.post("/inventory")
async def freeze_runtime_memory_inventory(
    payload: RuntimeCleanupInspectRequest,
    request: Request,
    user: UserContext = Depends(get_user_context),
) -> dict[str, Any]:
    plan = _authorize(request, user, payload.plan)
    try:
        return await _get_cleanup_service(request).inspect(plan)
    except RuntimeMemoryCleanupError as exc:
        raise HTTPException(status_code=503, detail={"code": exc.code}) from exc


@router.post("/execute")
async def execute_runtime_memory_cleanup(
    payload: RuntimeCleanupExecuteRequest,
    request: Request,
    user: UserContext = Depends(get_user_context),
) -> dict[str, Any]:
    plan = _authorize(request, user, payload.plan)
    try:
        return await _get_cleanup_service(request).execute(
            plan_value=plan,
            inventory_value=payload.inventory,
        )
    except ValueError as exc:
        raise HTTPException(
            status_code=422,
            detail={"code": "AGENT_RUNTIME_CLEANUP_INVENTORY_INVALID"},
        ) from exc
    except RuntimeMemoryCleanupError as exc:
        raise HTTPException(status_code=503, detail={"code": exc.code}) from exc
