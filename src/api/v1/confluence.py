"""
Confluence Integration API Endpoints.

Provides REST API for Confluence integration management:
- Connection configuration
- Space binding
- URL import
- Sync operations
- Status monitoring
"""

from __future__ import annotations

import logging
from typing import Any, Dict, List, Optional

from fastapi import APIRouter, Body, Depends, HTTPException, Query, Request

from ..deps import get_knowledge_service, get_user_context
from ..schemas.confluence import (
    ConfluenceBatchSyncResultSchema,
    ConfluenceConnectionCreateSchema,
    ConfluenceConnectionResponseSchema,
    ConfluenceConnectionTestResponseSchema,
    ConfluenceConnectionUpdateSchema,
    ConfluenceImportResultSchema,
    ConfluencePageListResponseSchema,
    ConfluencePageRecordSchema,
    ConfluenceSchedulerStatusSchema,
    ConfluenceSpaceBindingCreateSchema,
    ConfluenceSpaceBindingResponseSchema,
    ConfluenceSpaceBindingUpdateSchema,
    ConfluenceSpaceImportSchema,
    ConfluenceSpaceInfoSchema,
    ConfluenceSpaceListResponseSchema,
    ConfluenceSyncStatusSchema,
    ConfluenceSyncTaskSchema,
    ConfluenceSyncTriggerSchema,
    ConfluenceUrlImportSchema,
)
from ...core.auth.user_resolver import UserContext
from ...core.exceptions import PermissionDeniedError, ValidationFailedError

logger = logging.getLogger(__name__)

router = APIRouter(prefix="/confluence", tags=["confluence"])


def get_confluence_sync_service(request: Request):
    """Get ConfluenceSyncService from app state."""
    svc = getattr(request.app.state, "confluence_sync_service", None)
    if svc is None:
        raise HTTPException(
            status_code=503,
            detail="Confluence integration is not enabled. Set GATEWAY_CONFLUENCE__ENABLED=true.",
        )
    return svc


def get_confluence_scheduler(request: Request):
    """Get ConfluenceScheduler from app state."""
    scheduler = getattr(request.app.state, "confluence_scheduler", None)
    return scheduler


# ============================================================
# Connection Management
# ============================================================

@router.post("/connections", response_model=ConfluenceConnectionResponseSchema)
async def create_connection(
    request: Request,
    payload: ConfluenceConnectionCreateSchema = Body(...),
    user: UserContext = Depends(get_user_context),
):
    """Create a new Confluence connection."""
    try:
        request.app.state.dispatcher.rbac.require(user.roles, "confluence:manage")
        svc = get_confluence_sync_service(request)
        connection = await svc.create_connection(
            tenant_id=user.tenant_id,
            name=payload.name,
            domain=payload.domain,
            email=payload.email,
            api_token=payload.api_token,
            sync_mode=payload.sync_mode,
            polling_interval_minutes=payload.polling_interval_minutes,
            created_by=user.user_id,
        )
        return _connection_to_response(connection)
    except PermissionDeniedError as exc:
        raise HTTPException(status_code=403, detail=str(exc))
    except ValidationFailedError as exc:
        raise HTTPException(status_code=400, detail=str(exc))


@router.get("/connections", response_model=List[ConfluenceConnectionResponseSchema])
async def list_connections(
    request: Request,
    status: Optional[str] = Query(None, description="Filter by status"),
    user: UserContext = Depends(get_user_context),
):
    """List all Confluence connections for the tenant."""
    try:
        svc = get_confluence_sync_service(request)
        connections = await svc.list_connections(
            tenant_id=user.tenant_id,
            status=status,
        )
        return [_connection_to_response(c) for c in connections]
    except Exception as exc:
        logger.error(f"Failed to list connections: {exc}")
        raise HTTPException(status_code=500, detail=str(exc))


@router.get("/connections/{connection_id}", response_model=ConfluenceConnectionResponseSchema)
async def get_connection(
    request: Request,
    connection_id: str,
    user: UserContext = Depends(get_user_context),
):
    """Get a Confluence connection by ID."""
    try:
        svc = get_confluence_sync_service(request)
        connection = await svc.get_connection(connection_id)
        if not connection:
            raise HTTPException(status_code=404, detail="Connection not found")
        if connection.get("tenant_id") != user.tenant_id and "admin" not in user.roles:
            raise HTTPException(status_code=403, detail="Access denied")
        return _connection_to_response(connection)
    except HTTPException:
        raise
    except Exception as exc:
        logger.error(f"Failed to get connection: {exc}")
        raise HTTPException(status_code=500, detail=str(exc))


@router.put("/connections/{connection_id}", response_model=ConfluenceConnectionResponseSchema)
async def update_connection(
    request: Request,
    connection_id: str,
    payload: ConfluenceConnectionUpdateSchema = Body(...),
    user: UserContext = Depends(get_user_context),
):
    """Update a Confluence connection."""
    try:
        request.app.state.dispatcher.rbac.require(user.roles, "confluence:manage")
        svc = get_confluence_sync_service(request)
        connection = await svc.update_connection(
            connection_id=connection_id,
            tenant_id=user.tenant_id,
            **payload.model_dump(exclude_none=True),
        )
        return _connection_to_response(connection)
    except PermissionDeniedError as exc:
        raise HTTPException(status_code=403, detail=str(exc))
    except ValidationFailedError as exc:
        raise HTTPException(status_code=400, detail=str(exc))


@router.delete("/connections/{connection_id}")
async def delete_connection(
    request: Request,
    connection_id: str,
    user: UserContext = Depends(get_user_context),
):
    """Delete a Confluence connection and all its bindings."""
    try:
        request.app.state.dispatcher.rbac.require(user.roles, "confluence:manage")
        svc = get_confluence_sync_service(request)
        ok = await svc.delete_connection(connection_id, user.tenant_id)
        return {"status": "success" if ok else "not_found", "connection_id": connection_id}
    except PermissionDeniedError as exc:
        raise HTTPException(status_code=403, detail=str(exc))


@router.post("/connections/{connection_id}/test", response_model=ConfluenceConnectionTestResponseSchema)
async def test_connection(
    request: Request,
    connection_id: str,
    user: UserContext = Depends(get_user_context),
):
    """Test a Confluence connection."""
    try:
        svc = get_confluence_sync_service(request)
        result = await svc.test_connection(connection_id, user.tenant_id)
        return result
    except Exception as exc:
        logger.error(f"Connection test failed: {exc}")
        return {
            "status": "error",
            "message": str(exc),
        }


# ============================================================
# Space Discovery
# ============================================================

@router.get(
    "/connections/{connection_id}/discover/spaces",
    response_model=ConfluenceSpaceListResponseSchema,
)
async def discover_spaces(
    request: Request,
    connection_id: str,
    type_filter: Optional[str] = Query(None, description="Filter by type: global | personal"),
    user: UserContext = Depends(get_user_context),
):
    """Discover available spaces in the Confluence instance."""
    try:
        svc = get_confluence_sync_service(request)
        spaces = await svc.discover_spaces(
            connection_id=connection_id,
            tenant_id=user.tenant_id,
            type_filter=type_filter,
        )
        return {
            "spaces": [
                ConfluenceSpaceInfoSchema(
                    space_id=s.space_id,
                    space_key=s.space_key,
                    name=s.name,
                    type=s.type,
                    status=s.status,
                    homepage_id=s.homepage_id,
                    description=s.description,
                )
                for s in spaces
            ],
            "total": len(spaces),
        }
    except Exception as exc:
        logger.error(f"Failed to discover spaces: {exc}")
        raise HTTPException(status_code=500, detail=str(exc))


# ============================================================
# Space Binding
# ============================================================

@router.post(
    "/connections/{connection_id}/bindings",
    response_model=ConfluenceSpaceBindingResponseSchema,
)
async def create_space_binding(
    request: Request,
    connection_id: str,
    payload: ConfluenceSpaceBindingCreateSchema = Body(...),
    user: UserContext = Depends(get_user_context),
):
    """Bind a Confluence space to a dataset."""
    try:
        request.app.state.dispatcher.rbac.require(user.roles, "confluence:manage")

        # Verify dataset access
        knowledge_svc = get_knowledge_service(request)
        await knowledge_svc.require_dataset_access(user, payload.dataset_id, required="editor")

        svc = get_confluence_sync_service(request)
        binding = await svc.create_space_binding(
            connection_id=connection_id,
            tenant_id=user.tenant_id,
            dataset_id=payload.dataset_id,
            space_key=payload.space_key,
            include_patterns=payload.include_patterns,
            exclude_patterns=payload.exclude_patterns,
            max_depth=payload.max_depth,
            include_attachments=payload.include_attachments,
            include_comments=payload.include_comments,
            created_by=user.user_id,
        )
        return binding
    except PermissionDeniedError as exc:
        raise HTTPException(status_code=403, detail=str(exc))
    except ValidationFailedError as exc:
        raise HTTPException(status_code=400, detail=str(exc))


@router.get(
    "/connections/{connection_id}/bindings",
    response_model=List[ConfluenceSpaceBindingResponseSchema],
)
async def list_space_bindings(
    request: Request,
    connection_id: str,
    user: UserContext = Depends(get_user_context),
):
    """List all space bindings for a connection."""
    try:
        svc = get_confluence_sync_service(request)
        bindings = await svc.list_bindings(connection_id, user.tenant_id)
        return bindings
    except Exception as exc:
        logger.error(f"Failed to list bindings: {exc}")
        raise HTTPException(status_code=500, detail=str(exc))


@router.get("/bindings/{binding_id}", response_model=ConfluenceSpaceBindingResponseSchema)
async def get_space_binding(
    request: Request,
    binding_id: str,
    user: UserContext = Depends(get_user_context),
):
    """Get a space binding by ID."""
    try:
        svc = get_confluence_sync_service(request)
        binding = await svc.get_binding(binding_id)
        if not binding:
            raise HTTPException(status_code=404, detail="Binding not found")
        return binding
    except HTTPException:
        raise
    except Exception as exc:
        logger.error(f"Failed to get binding: {exc}")
        raise HTTPException(status_code=500, detail=str(exc))


@router.put("/bindings/{binding_id}", response_model=ConfluenceSpaceBindingResponseSchema)
async def update_space_binding(
    request: Request,
    binding_id: str,
    payload: ConfluenceSpaceBindingUpdateSchema = Body(...),
    user: UserContext = Depends(get_user_context),
):
    """Update a space binding."""
    try:
        request.app.state.dispatcher.rbac.require(user.roles, "confluence:manage")
        svc = get_confluence_sync_service(request)
        binding = await svc.update_binding(
            binding_id=binding_id,
            **payload.model_dump(exclude_none=True),
        )
        return binding
    except PermissionDeniedError as exc:
        raise HTTPException(status_code=403, detail=str(exc))
    except ValidationFailedError as exc:
        raise HTTPException(status_code=400, detail=str(exc))


@router.delete("/bindings/{binding_id}")
async def delete_space_binding(
    request: Request,
    binding_id: str,
    delete_documents: bool = Query(False, description="Also delete imported documents"),
    user: UserContext = Depends(get_user_context),
):
    """Delete a space binding."""
    try:
        request.app.state.dispatcher.rbac.require(user.roles, "confluence:manage")
        svc = get_confluence_sync_service(request)
        ok = await svc.delete_binding(binding_id, delete_documents=delete_documents)
        return {"status": "success" if ok else "not_found", "binding_id": binding_id}
    except PermissionDeniedError as exc:
        raise HTTPException(status_code=403, detail=str(exc))


# ============================================================
# Import Operations
# ============================================================

@router.post("/import/url", response_model=ConfluenceImportResultSchema)
async def import_from_url(
    request: Request,
    payload: ConfluenceUrlImportSchema = Body(...),
    user: UserContext = Depends(get_user_context),
):
    """Import a single Confluence page by URL."""
    try:
        request.app.state.dispatcher.rbac.require(user.roles, "confluence:import")

        # Verify dataset access
        knowledge_svc = get_knowledge_service(request)
        await knowledge_svc.require_dataset_access(user, payload.dataset_id, required="editor")

        svc = get_confluence_sync_service(request)
        result = await svc.import_from_url(
            url=payload.url,
            dataset_id=payload.dataset_id,
            connection_id=payload.connection_id,
            tenant_id=user.tenant_id,
            metadata=payload.metadata,
            created_by=user.user_id,
        )
        return result
    except PermissionDeniedError as exc:
        raise HTTPException(status_code=403, detail=str(exc))
    except ValidationFailedError as exc:
        raise HTTPException(status_code=400, detail=str(exc))
    except Exception as exc:
        logger.error(f"URL import failed: {exc}")
        return ConfluenceImportResultSchema(
            status="failed",
            message=str(exc),
        )


@router.post("/import/space", response_model=ConfluenceBatchSyncResultSchema)
async def import_space(
    request: Request,
    payload: ConfluenceSpaceImportSchema = Body(...),
    user: UserContext = Depends(get_user_context),
):
    """Trigger a full space import (batch operation)."""
    try:
        request.app.state.dispatcher.rbac.require(user.roles, "confluence:import")
        svc = get_confluence_sync_service(request)
        result = await svc.import_space(
            binding_id=payload.binding_id,
            force_full_sync=payload.force_full_sync,
        )
        return result.to_dict()
    except PermissionDeniedError as exc:
        raise HTTPException(status_code=403, detail=str(exc))
    except ValidationFailedError as exc:
        raise HTTPException(status_code=400, detail=str(exc))


# ============================================================
# Sync Operations
# ============================================================

@router.post("/bindings/{binding_id}/sync")
async def trigger_sync(
    request: Request,
    binding_id: str,
    payload: ConfluenceSyncTriggerSchema = Body(default=None),
    user: UserContext = Depends(get_user_context),
):
    """Trigger a sync operation for a binding."""
    try:
        request.app.state.dispatcher.rbac.require(user.roles, "confluence:sync")
        svc = get_confluence_sync_service(request)

        force = payload.force if payload else False
        page_ids = payload.page_ids if payload else None

        task_id = await svc.trigger_sync(
            binding_id=binding_id,
            force=force,
            page_ids=page_ids,
        )
        return {"status": "triggered", "task_id": task_id, "binding_id": binding_id}
    except PermissionDeniedError as exc:
        raise HTTPException(status_code=403, detail=str(exc))
    except ValidationFailedError as exc:
        raise HTTPException(status_code=400, detail=str(exc))


@router.get("/bindings/{binding_id}/status", response_model=ConfluenceSyncStatusSchema)
async def get_sync_status(
    request: Request,
    binding_id: str,
    user: UserContext = Depends(get_user_context),
):
    """Get sync status for a binding."""
    try:
        svc = get_confluence_sync_service(request)
        status = await svc.get_sync_status(binding_id)
        return status
    except Exception as exc:
        logger.error(f"Failed to get sync status: {exc}")
        raise HTTPException(status_code=500, detail=str(exc))


@router.get("/bindings/{binding_id}/pages", response_model=ConfluencePageListResponseSchema)
async def list_synced_pages(
    request: Request,
    binding_id: str,
    status: Optional[str] = Query(None, description="Filter by status"),
    limit: int = Query(100, ge=1, le=500),
    offset: int = Query(0, ge=0),
    user: UserContext = Depends(get_user_context),
):
    """List synced pages for a binding."""
    try:
        svc = get_confluence_sync_service(request)
        pages = await svc.list_pages(
            binding_id=binding_id,
            status=status,
            limit=limit,
            offset=offset,
        )

        # Count by status
        synced = sum(1 for p in pages if p.get("status") == "synced")
        pending = sum(1 for p in pages if p.get("status") == "pending")
        error = sum(1 for p in pages if p.get("status") == "error")

        return {
            "pages": pages,
            "total": len(pages),
            "synced": synced,
            "pending": pending,
            "error": error,
        }
    except Exception as exc:
        logger.error(f"Failed to list pages: {exc}")
        raise HTTPException(status_code=500, detail=str(exc))


@router.post("/pages/{page_record_id}/sync")
async def sync_single_page(
    request: Request,
    page_record_id: str,
    user: UserContext = Depends(get_user_context),
):
    """Sync a single page."""
    try:
        request.app.state.dispatcher.rbac.require(user.roles, "confluence:sync")
        svc = get_confluence_sync_service(request)
        result = await svc.sync_page(page_record_id)
        return {"status": "success", "result": result}
    except PermissionDeniedError as exc:
        raise HTTPException(status_code=403, detail=str(exc))
    except Exception as exc:
        logger.error(f"Page sync failed: {exc}")
        return {"status": "failed", "message": str(exc)}


# ============================================================
# Sync Tasks
# ============================================================

@router.get("/tasks", response_model=List[ConfluenceSyncTaskSchema])
async def list_sync_tasks(
    request: Request,
    binding_id: Optional[str] = Query(None),
    status: Optional[str] = Query(None),
    limit: int = Query(100, ge=1, le=500),
    user: UserContext = Depends(get_user_context),
):
    """List sync tasks."""
    try:
        svc = get_confluence_sync_service(request)
        tasks = await svc.list_sync_tasks(
            binding_id=binding_id,
            status=status,
            limit=limit,
        )
        return tasks
    except Exception as exc:
        logger.error(f"Failed to list tasks: {exc}")
        raise HTTPException(status_code=500, detail=str(exc))


@router.get("/tasks/{task_id}", response_model=ConfluenceSyncTaskSchema)
async def get_sync_task(
    request: Request,
    task_id: str,
    user: UserContext = Depends(get_user_context),
):
    """Get a sync task by ID."""
    try:
        svc = get_confluence_sync_service(request)
        task = await svc.get_sync_task(task_id)
        if not task:
            raise HTTPException(status_code=404, detail="Task not found")
        return task
    except HTTPException:
        raise
    except Exception as exc:
        logger.error(f"Failed to get task: {exc}")
        raise HTTPException(status_code=500, detail=str(exc))


# ============================================================
# Scheduler
# ============================================================

@router.get("/scheduler/status", response_model=ConfluenceSchedulerStatusSchema)
async def get_scheduler_status(
    request: Request,
    user: UserContext = Depends(get_user_context),
):
    """Get scheduler status."""
    try:
        scheduler = get_confluence_scheduler(request)
        if not scheduler:
            return {
                "is_running": False,
                "task_count": 0,
                "active_sync_count": 0,
                "max_concurrent": 0,
                "tasks": [],
            }
        return await scheduler.get_status()
    except Exception as exc:
        logger.error(f"Failed to get scheduler status: {exc}")
        raise HTTPException(status_code=500, detail=str(exc))


@router.post("/scheduler/start")
async def start_scheduler(
    request: Request,
    user: UserContext = Depends(get_user_context),
):
    """Start the polling scheduler."""
    try:
        request.app.state.dispatcher.rbac.require(user.roles, "confluence:manage")
        scheduler = get_confluence_scheduler(request)
        if not scheduler:
            raise HTTPException(status_code=503, detail="Scheduler not initialized")
        await scheduler.start()
        return {"status": "started"}
    except PermissionDeniedError as exc:
        raise HTTPException(status_code=403, detail=str(exc))
    except HTTPException:
        raise
    except Exception as exc:
        logger.error(f"Failed to start scheduler: {exc}")
        raise HTTPException(status_code=500, detail=str(exc))


@router.post("/scheduler/stop")
async def stop_scheduler(
    request: Request,
    user: UserContext = Depends(get_user_context),
):
    """Stop the polling scheduler."""
    try:
        request.app.state.dispatcher.rbac.require(user.roles, "confluence:manage")
        scheduler = get_confluence_scheduler(request)
        if not scheduler:
            raise HTTPException(status_code=503, detail="Scheduler not initialized")
        await scheduler.stop()
        return {"status": "stopped"}
    except PermissionDeniedError as exc:
        raise HTTPException(status_code=403, detail=str(exc))
    except HTTPException:
        raise
    except Exception as exc:
        logger.error(f"Failed to stop scheduler: {exc}")
        raise HTTPException(status_code=500, detail=str(exc))


# ============================================================
# Helper Functions
# ============================================================

def _connection_to_response(connection: Dict[str, Any]) -> ConfluenceConnectionResponseSchema:
    """Convert connection dict to response schema (excluding sensitive data)."""
    return ConfluenceConnectionResponseSchema(
        connection_id=connection.get("connection_id", ""),
        tenant_id=connection.get("tenant_id", ""),
        name=connection.get("name", ""),
        domain=connection.get("domain", ""),
        email=connection.get("email", ""),
        sync_mode=connection.get("sync_mode", "manual"),
        polling_interval_minutes=connection.get("polling_interval_minutes", 60),
        status=connection.get("status", "active"),
        last_sync_at=connection.get("last_sync_at"),
        last_error=connection.get("last_error"),
        created_by=connection.get("created_by"),
        created_at=connection.get("created_at"),
        updated_at=connection.get("updated_at"),
    )
