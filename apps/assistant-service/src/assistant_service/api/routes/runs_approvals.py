"""Run-status + approval routes.

Both routes read from / write to the DB-authoritative
``AssistantExecutionGateway`` registries (ADR-004 §B). Response shapes
mirror the gateway's ``RunStatusResponse`` / ``ApprovalResponse`` so the
Phase 5c proxy flip is a drop-in swap.

The GW counterparts in ``src/api/v1/assistant.py`` currently call the
in-process ``AssistantService`` execution_gateway; once the corresponding
feature flags (``ASSISTANT_ROUTE_RUNS_PROXIED`` /
``ASSISTANT_ROUTE_APPROVALS_PROXIED``) are flipped ON, they proxy here.
Because step 2 of ADR-004 made the registries DB-authoritative, the
split-brain that blocked 5b is gone — a run started by the GW's
AssistantService instance and a run started by the AS container's
instance both land in the same DB rows.
"""
from __future__ import annotations

from fastapi import APIRouter, Depends, HTTPException, Request
from pydantic import BaseModel

from ...auth import UserContext, get_user_context
from ..deps import get_assistant_service

router = APIRouter()


class ApprovalRequest(BaseModel):
    approved: bool
    reason: str | None = None


class ResumeRequest(BaseModel):
    approval_id: str | None = None


@router.get("/runs/{run_id}")
async def get_run_status(
    run_id: str,
    request: Request,
    user: UserContext = Depends(get_user_context),
):
    """Return run status for the current user/tenant.

    Shape matches gateway's ``RunStatusResponse``: ``{"run": {...}}``.
    """
    assistant = get_assistant_service(request)
    run = await assistant.get_run_status(
        run_id=run_id,
        tenant_id=user.tenant_id,
        user_id=user.user_id,
    )
    if not run:
        raise HTTPException(status_code=404, detail="Run not found")
    return {"run": run}


@router.post("/runs/{run_id}/resume")
async def prepare_run_resume(
    run_id: str,
    request: Request,
    body: ResumeRequest | None = None,
    user: UserContext = Depends(get_user_context),
):
    """Return a non-executing resume plan from the latest safe checkpoint."""
    assistant = get_assistant_service(request)
    resume = await assistant.prepare_run_resume(
        run_id=run_id,
        tenant_id=user.tenant_id,
        user_id=user.user_id,
        approval_id=body.approval_id if body else None,
    )
    if not resume:
        raise HTTPException(status_code=404, detail="Run not found")
    return {"resume": resume}


@router.post("/approvals/{approval_id}")
async def approve_tool_call(
    approval_id: str,
    body: ApprovalRequest,
    request: Request,
    user: UserContext = Depends(get_user_context),
):
    """Approve or reject a pending tool invocation.

    Shape matches gateway's ``ApprovalResponse``: ``{"approval": {...}}``.
    """
    assistant = get_assistant_service(request)
    approval = await assistant.approve_tool_request(
        approval_id=approval_id,
        tenant_id=user.tenant_id,
        user_id=user.user_id,
        approved=body.approved,
        approver_user_id=user.user_id,
        reason=body.reason,
    )
    if not approval:
        raise HTTPException(status_code=404, detail="Approval not found")
    return {"approval": approval}
