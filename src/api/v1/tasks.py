from __future__ import annotations

from fastapi import APIRouter, Depends, HTTPException

from ..deps import AuthContext, get_auth_context, get_task_manager
from ..schemas.response import UnifiedResponseSchema
from ..schemas.task import TaskSchema
from ...core.exceptions import GatewayError, TaskNotFoundError
from ...services.task.task_manager import TaskManager


router = APIRouter()


@router.get("/tasks/{task_id}", response_model=TaskSchema)
async def get_task(
    task_id: str,
    task_manager: TaskManager = Depends(get_task_manager),
    auth: AuthContext = Depends(get_auth_context),
):
    try:
        task = await task_manager.get_task(task_id)
    except TaskNotFoundError as exc:
        raise HTTPException(status_code=404, detail=str(exc))
    return TaskSchema.from_domain(task)


@router.delete("/tasks/{task_id}")
async def cancel_task(
    task_id: str,
    task_manager: TaskManager = Depends(get_task_manager),
    auth: AuthContext = Depends(get_auth_context),
):
    try:
        await task_manager.cancel(task_id)
    except TaskNotFoundError as exc:
        raise HTTPException(status_code=404, detail=str(exc))
    return {"task_id": task_id, "status": "cancelled"}


@router.get("/tasks/{task_id}/result", response_model=UnifiedResponseSchema)
async def get_task_result(
    task_id: str,
    task_manager: TaskManager = Depends(get_task_manager),
    auth: AuthContext = Depends(get_auth_context),
):
    try:
        resp = await task_manager.get_result(task_id)
    except GatewayError as exc:
        raise HTTPException(status_code=404, detail=str(exc))
    return UnifiedResponseSchema.from_domain(resp)
