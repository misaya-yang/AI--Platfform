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
from typing import Any

from fastapi import APIRouter, Body, Depends, HTTPException, Query, Request

from ...core.auth.user_resolver import UserContext
from ...core.exceptions import PermissionDeniedError, ValidationFailedError
from ...services.knowledge.confluence.sync_service import (
    ConfluenceAccessDeniedError,
    ConfluenceSyncError,
)
from ..deps import get_knowledge_service, get_user_context
from ..schemas.confluence import (
    ConfluenceBatchSyncRequestSchema,
    ConfluenceBatchSyncResultSchema,
    ConfluenceConnectionCreateSchema,
    ConfluenceConnectionResponseSchema,
    ConfluenceConnectionTestResponseSchema,
    ConfluenceConnectionUpdateSchema,
    ConfluenceImportResultSchema,
    ConfluencePageListResponseSchema,
    ConfluencePageRecordSchema,
    ConfluencePageSyncConfigUpdateSchema,
    ConfluencePageTreeResponseSchema,
    ConfluenceRemovePagesRequestSchema,
    ConfluenceRemovePagesResultSchema,
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


@router.get("/connections", response_model=list[ConfluenceConnectionResponseSchema])
async def list_connections(
    request: Request,
    status: str | None = Query(None, description="Filter by status"),
    user: UserContext = Depends(get_user_context),
):
    """List all Confluence connections for the tenant."""
    try:
        svc = get_confluence_sync_service(request)
        connections = await svc.list_connections(
            user=user,
            tenant_id=user.tenant_id,
            status=status,
        )
        return [_connection_to_response(c) for c in connections]
    except ConfluenceAccessDeniedError as exc:
        raise HTTPException(status_code=403, detail=str(exc))
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
        connection = await svc.get_connection(connection_id, user=user)
        return _connection_to_response(connection)
    except ConfluenceAccessDeniedError as exc:
        raise HTTPException(status_code=403, detail=str(exc))
    except ConfluenceSyncError as exc:
        raise HTTPException(status_code=404, detail=str(exc))
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

        # 验证用户对 connection 的访问权限
        await svc.get_connection(connection_id, user=user)

        connection = await svc.update_connection(
            connection_id=connection_id,
            tenant_id=user.tenant_id,
            **payload.model_dump(exclude_none=True),
        )
        return _connection_to_response(connection)
    except ConfluenceAccessDeniedError as exc:
        raise HTTPException(status_code=403, detail=str(exc))
    except ConfluenceSyncError as exc:
        raise HTTPException(status_code=404, detail=str(exc))
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

        # 验证用户对 connection 的访问权限
        await svc.get_connection(connection_id, user=user)

        ok = await svc.delete_connection(connection_id, user.tenant_id)
        return {"status": "success" if ok else "not_found", "connection_id": connection_id}
    except ConfluenceAccessDeniedError as exc:
        raise HTTPException(status_code=403, detail=str(exc))
    except ConfluenceSyncError as exc:
        raise HTTPException(status_code=404, detail=str(exc))
    except PermissionDeniedError as exc:
        raise HTTPException(status_code=403, detail=str(exc))


@router.post("/connections/test", response_model=ConfluenceConnectionTestResponseSchema)
async def test_connection_credentials(
    request: Request,
    payload: dict[str, Any] = Body(...),
    user: UserContext = Depends(get_user_context),
):
    """Test Confluence credentials without creating a connection."""
    from ...services.knowledge.confluence.client import ConfluenceAPIError, ConfluenceClient
    from ...services.knowledge.confluence.models import ConfluenceCredentials

    try:
        # RBAC 检查：测试连接需要与创建连接相同的权限
        request.app.state.dispatcher.rbac.require(user.roles, "confluence:manage")

        domain = payload.get("domain", "")
        email = payload.get("email", "")
        api_token = payload.get("api_token", "")

        logger.info(f"Testing Confluence connection: domain={domain}, email={email}")

        if not all([domain, email, api_token]):
            return {
                "status": "error",
                "message": "Missing required fields: domain, email, api_token",
            }

        credentials = ConfluenceCredentials(
            domain=domain,
            email=email,
            api_token=api_token,
        )

        logger.info(f"Created credentials, API URL: {credentials.api_v2_url}")

        async with ConfluenceClient(credentials) as client:
            result = await client.test_connection()
            logger.info(f"Test connection result: {result}")
            return result

    except ConfluenceAPIError as exc:
        logger.error(
            f"Confluence API error during test: {exc}, status_code={exc.status_code}, body={exc.response_body}"
        )
        return {
            "status": "error",
            "message": f"API error ({exc.status_code}): {str(exc)}",
        }
    except Exception as exc:
        import traceback

        tb = traceback.format_exc()
        logger.error(f"Connection credentials test failed: {exc}\n{tb}")
        return {
            "status": "error",
            "message": f"Connection test failed: {type(exc).__name__}: {str(exc)}",
        }


@router.post(
    "/connections/{connection_id}/test", response_model=ConfluenceConnectionTestResponseSchema
)
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
    type_filter: str | None = Query(None, description="Filter by type: global | personal"),
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


@router.get(
    "/connections/{connection_id}/discover/spaces/{space_key}/pages",
    response_model=ConfluencePageTreeResponseSchema,
)
async def discover_space_pages(
    request: Request,
    connection_id: str,
    space_key: str,
    max_depth: int = Query(3, ge=1, le=10, description="Maximum depth to fetch"),
    user: UserContext = Depends(get_user_context),
):
    """
    Discover page hierarchy in a Confluence space.

    Returns a tree structure of pages for selection as root_page_id.
    """
    try:
        svc = get_confluence_sync_service(request)
        page_tree = await svc.discover_space_page_tree(
            connection_id=connection_id,
            tenant_id=user.tenant_id,
            space_key=space_key,
            max_depth=max_depth,
        )
        return page_tree
    except Exception as exc:
        logger.error(f"Failed to discover space pages: {exc}")
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

        # Build root_page_ids list, supporting both old single ID and new list format
        root_page_ids = list(payload.root_page_ids) if payload.root_page_ids else []
        # Backward compatibility: if root_page_id is set but not in root_page_ids, add it
        if payload.root_page_id and payload.root_page_id not in root_page_ids:
            root_page_ids.append(payload.root_page_id)

        binding = await svc.create_space_binding(
            connection_id=connection_id,
            tenant_id=user.tenant_id,
            dataset_id=payload.dataset_id,
            space_key=payload.space_key,
            root_page_ids=root_page_ids,
            include_patterns=payload.include_patterns,
            exclude_patterns=payload.exclude_patterns,
            max_depth=payload.max_depth,
            include_attachments=payload.include_attachments,
            include_comments=payload.include_comments,
            sync_images=payload.sync_images,
            image_max_size_bytes=payload.image_max_size_bytes,
            created_by=user.user_id,
        )
        return binding
    except PermissionDeniedError as exc:
        raise HTTPException(status_code=403, detail=str(exc))
    except ValidationFailedError as exc:
        raise HTTPException(status_code=400, detail=str(exc))
    except ConfluenceSyncError as exc:
        # 包括重复绑定、连接不存在等业务错误
        raise HTTPException(status_code=400, detail=str(exc))
    except Exception as exc:
        logger.error(f"Failed to create binding: {exc}", exc_info=True)
        raise HTTPException(status_code=500, detail=str(exc))


@router.get(
    "/connections/{connection_id}/bindings",
    response_model=list[ConfluenceSpaceBindingResponseSchema],
)
async def list_space_bindings(
    request: Request,
    connection_id: str,
    user: UserContext = Depends(get_user_context),
):
    """List all space bindings for a connection."""
    try:
        svc = get_confluence_sync_service(request)
        bindings = await svc.list_bindings(
            user=user,
            connection_id=connection_id,
            tenant_id=user.tenant_id,
        )
        return bindings
    except ConfluenceAccessDeniedError as exc:
        raise HTTPException(status_code=403, detail=str(exc))
    except Exception as exc:
        logger.error(f"Failed to list bindings: {exc}")
        raise HTTPException(status_code=500, detail=str(exc))


@router.get("/bindings", response_model=list[ConfluenceSpaceBindingResponseSchema])
async def list_all_bindings(
    request: Request,
    connection_id: str | None = Query(None, description="Filter by connection ID"),
    dataset_id: str | None = Query(None, description="Filter by dataset ID"),
    status: str | None = Query(None, description="Filter by status"),
    user: UserContext = Depends(get_user_context),
):
    """List all space bindings for the current tenant."""
    try:
        svc = get_confluence_sync_service(request)
        bindings = await svc.list_all_bindings(
            user=user,
            tenant_id=user.tenant_id,
            connection_id=connection_id,
            dataset_id=dataset_id,
            status=status,
        )
        return bindings
    except ConfluenceAccessDeniedError as exc:
        raise HTTPException(status_code=403, detail=str(exc))
    except Exception as exc:
        logger.error(f"Failed to list all bindings: {exc}")
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
        binding = await svc.get_binding(binding_id, user=user)
        return binding
    except ConfluenceAccessDeniedError as exc:
        raise HTTPException(status_code=403, detail=str(exc))
    except ConfluenceSyncError as exc:
        raise HTTPException(status_code=404, detail=str(exc))
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

        # 验证用户对 binding 的访问权限
        await svc.get_binding(binding_id, user=user)

        binding = await svc.update_binding(
            binding_id=binding_id,
            **payload.model_dump(exclude_none=True),
        )

        # 如果更新了同步配置，通知调度器
        scheduler = getattr(request.app.state, "confluence_scheduler", None)
        sync_config_changed = (
            payload.sync_mode is not None
            or payload.polling_interval_minutes is not None
            or payload.sync_enabled is not None
        )
        if scheduler and sync_config_changed:
            await scheduler.reload_bindings()

        return binding
    except ConfluenceAccessDeniedError as exc:
        raise HTTPException(status_code=403, detail=str(exc))
    except ConfluenceSyncError as exc:
        raise HTTPException(status_code=404, detail=str(exc))
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

        # 验证用户对 binding 的访问权限
        await svc.get_binding(binding_id, user=user)

        ok = await svc.delete_binding(binding_id, delete_documents=delete_documents)
        return {"status": "success" if ok else "not_found", "binding_id": binding_id}
    except ConfluenceAccessDeniedError as exc:
        raise HTTPException(status_code=403, detail=str(exc))
    except ConfluenceSyncError as exc:
        raise HTTPException(status_code=404, detail=str(exc))
    except PermissionDeniedError as exc:
        raise HTTPException(status_code=403, detail=str(exc))


@router.post("/bindings/{binding_id}/pages")
async def add_pages_to_binding(
    request: Request,
    binding_id: str,
    payload: dict[str, Any] = Body(...),
    user: UserContext = Depends(get_user_context),
):
    """
    Add specific Confluence pages to an existing binding.

    This fetches the pages from Confluence and syncs them to the knowledge base.

    Request body:
        page_ids: List of Confluence page IDs to add
    """
    try:
        request.app.state.dispatcher.rbac.require(user.roles, "confluence:import")

        page_ids = payload.get("page_ids", [])
        if not page_ids:
            raise HTTPException(status_code=400, detail="page_ids is required")

        svc = get_confluence_sync_service(request)

        # Verify binding exists and user has access
        binding = await svc.get_binding(binding_id, user=user)

        # Verify dataset access
        knowledge_svc = get_knowledge_service(request)
        await knowledge_svc.require_dataset_access(user, binding["dataset_id"], required="editor")

        results = []
        for page_id in page_ids:
            try:
                doc_id = await svc.sync_page_by_id(
                    binding_id=binding_id,
                    page_id=str(page_id),
                    event_type="created",
                )
                results.append(
                    {
                        "page_id": page_id,
                        "status": "success" if doc_id else "skipped",
                        "document_id": doc_id,
                    }
                )
            except Exception as e:
                logger.warning(f"Failed to sync page {page_id}: {e}")
                results.append(
                    {
                        "page_id": page_id,
                        "status": "error",
                        "error": str(e),
                    }
                )

        # Update binding page counts
        await svc.refresh_binding_stats(binding_id)

        success_count = sum(1 for r in results if r["status"] == "success")
        return {
            "status": "success",
            "binding_id": binding_id,
            "total": len(page_ids),
            "success_count": success_count,
            "results": results,
        }

    except HTTPException:
        raise
    except PermissionDeniedError as exc:
        raise HTTPException(status_code=403, detail=str(exc))
    except Exception as exc:
        logger.error(f"Failed to add pages to binding: {exc}")
        raise HTTPException(status_code=500, detail=str(exc))


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

        # 验证用户对 binding 的访问权限
        await svc.get_binding(payload.binding_id, user=user)

        result = await svc.import_space(
            binding_id=payload.binding_id,
            force_full_sync=payload.force_full_sync,
        )
        return result.to_dict()
    except ConfluenceAccessDeniedError as exc:
        raise HTTPException(status_code=403, detail=str(exc))
    except ConfluenceSyncError as exc:
        raise HTTPException(status_code=404, detail=str(exc))
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

        # 验证用户对 binding 的访问权限
        await svc.get_binding(binding_id, user=user)

        force = payload.force if payload else False
        page_ids = payload.page_ids if payload else None

        task_id = await svc.trigger_sync(
            binding_id=binding_id,
            force=force,
            page_ids=page_ids,
        )
        return {"status": "triggered", "task_id": task_id, "binding_id": binding_id}
    except ConfluenceAccessDeniedError as exc:
        raise HTTPException(status_code=403, detail=str(exc))
    except ConfluenceSyncError as exc:
        raise HTTPException(status_code=404, detail=str(exc))
    except PermissionDeniedError as exc:
        raise HTTPException(status_code=403, detail=str(exc))
    except ValidationFailedError as exc:
        raise HTTPException(status_code=400, detail=str(exc))


@router.post("/bindings/{binding_id}/incremental-sync")
async def trigger_incremental_sync(
    request: Request,
    binding_id: str,
    force: bool = False,
    user: UserContext = Depends(get_user_context),
):
    """
    Trigger an incremental sync for a binding.

    This endpoint is useful for testing the incremental sync functionality.
    It only syncs pages that have been modified since the last sync.

    Args:
        binding_id: The binding ID
        force: Force sync even if another sync is in progress
    """
    try:
        request.app.state.dispatcher.rbac.require(user.roles, "confluence:sync")
        svc = get_confluence_sync_service(request)

        # 验证用户对 binding 的访问权限
        await svc.get_binding(binding_id, user=user)

        task_id = await svc.incremental_sync(
            binding_id=binding_id,
            force=force,
        )
        return {
            "status": "triggered",
            "task_id": task_id,
            "binding_id": binding_id,
            "sync_type": "incremental",
        }
    except ConfluenceAccessDeniedError as exc:
        raise HTTPException(status_code=403, detail=str(exc))
    except ConfluenceSyncError as exc:
        raise HTTPException(status_code=404, detail=str(exc))
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
        status = await svc.get_sync_status(binding_id, user=user)
        return status
    except ConfluenceAccessDeniedError as exc:
        raise HTTPException(status_code=403, detail=str(exc))
    except ConfluenceSyncError as exc:
        raise HTTPException(status_code=404, detail=str(exc))
    except Exception as exc:
        logger.error(f"Failed to get sync status: {exc}")
        raise HTTPException(status_code=500, detail=str(exc))


@router.get("/bindings/{binding_id}/pages", response_model=ConfluencePageListResponseSchema)
async def list_synced_pages(
    request: Request,
    binding_id: str,
    status: str | None = Query(None, description="Filter by status"),
    limit: int = Query(100, ge=1, le=500),
    offset: int = Query(0, ge=0),
    synced_only: bool = Query(
        True, description="Only return pages with document_id (synced to knowledge base)"
    ),
    user: UserContext = Depends(get_user_context),
):
    """List synced pages for a binding."""
    try:
        svc = get_confluence_sync_service(request)
        pages = await svc.list_pages(
            binding_id=binding_id,
            user=user,
            status=status,
            limit=limit,
            offset=offset,
            synced_only=synced_only,
        )

        # Validate pages have required ID field
        valid_pages = []
        for p in pages:
            if not p.get("id"):
                logger.error(
                    f"Page record missing 'id' field: {p.get('title', 'unknown')} (page_id={p.get('page_id')})"
                )
                continue
            valid_pages.append(p)

        # Count by status
        synced = sum(1 for p in valid_pages if p.get("status") == "synced")
        pending = sum(1 for p in valid_pages if p.get("status") == "pending")
        error = sum(1 for p in valid_pages if p.get("status") == "error")
        # Count pages that need resync (effective_status = 'needs_resync')
        needs_resync = sum(1 for p in valid_pages if p.get("effective_status") == "needs_resync")

        return {
            "pages": valid_pages,
            "total": len(pages),
            "synced": synced,
            "pending": pending,
            "error": error,
            "needs_resync": needs_resync,
        }
    except ConfluenceAccessDeniedError as exc:
        raise HTTPException(status_code=403, detail=str(exc))
    except ConfluenceSyncError as exc:
        raise HTTPException(status_code=404, detail=str(exc))
    except Exception as exc:
        logger.error(f"Failed to list pages: {exc}")
        raise HTTPException(status_code=500, detail=str(exc))


@router.post("/bindings/{binding_id}/cleanup")
async def cleanup_unsynced_pages(
    request: Request,
    binding_id: str,
    user: UserContext = Depends(get_user_context),
):
    """
    清理未同步的页面记录。

    删除所有 document_id 为空的记录（从未真正同步到知识库的页面）。
    这些记录可能是之前发现但从未选择同步的页面。
    """
    try:
        request.app.state.dispatcher.rbac.require(user.roles, "confluence:manage")
        svc = get_confluence_sync_service(request)
        result = await svc.cleanup_unsynced_pages(binding_id, user)
        return result
    except ConfluenceAccessDeniedError as exc:
        raise HTTPException(status_code=403, detail=str(exc))
    except PermissionDeniedError as exc:
        raise HTTPException(status_code=403, detail=str(exc))
    except Exception as exc:
        logger.error(f"Cleanup failed: {exc}")
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

        # 传递 user 以验证对页面所属 binding 的访问权限
        result = await svc.sync_page(page_record_id, user=user)
        return {"status": "success", "result": result}
    except ConfluenceAccessDeniedError as exc:
        raise HTTPException(status_code=403, detail=str(exc))
    except ConfluenceSyncError as exc:
        raise HTTPException(status_code=404, detail=str(exc))
    except PermissionDeniedError as exc:
        raise HTTPException(status_code=403, detail=str(exc))
    except Exception as exc:
        logger.error(f"Page sync failed: {exc}")
        return {"status": "failed", "message": str(exc)}


@router.put("/pages/{page_record_id}/sync-config", response_model=ConfluencePageRecordSchema)
async def update_page_sync_config(
    request: Request,
    page_record_id: str,
    payload: ConfluencePageSyncConfigUpdateSchema = Body(...),
    user: UserContext = Depends(get_user_context),
):
    """
    Update sync configuration for a specific page.

    Allows setting page-level sync mode to override binding defaults:
    - sync_mode: NULL (inherit from binding), "manual", or "polling"
    - polling_interval_minutes: 5-1440 minutes (only for polling mode)
    - sync_enabled: Enable/disable sync for this page
    - sync_priority: 0-100, higher priority pages sync first
    """
    try:
        request.app.state.dispatcher.rbac.require(user.roles, "confluence:manage")
        svc = get_confluence_sync_service(request)

        # 获取页面信息并验证权限
        page = await svc.get_page(page_record_id, user=user)
        if not page:
            raise HTTPException(status_code=404, detail="Page not found")

        # 更新同步配置
        # 使用 exclude_unset=True 而不是 exclude_none=True
        # 这样可以保留显式设置为 null 的字段（用于"继承"选项）
        updates = payload.model_dump(exclude_unset=True)

        # 如果设置为 polling 模式，计算下次同步时间并确保 interval 有值
        if updates.get("sync_mode") == "polling":
            from datetime import datetime, timedelta

            # 确保 polling_interval_minutes 有默认值（防止 NULL 导致调度器崩溃）
            interval = updates.get("polling_interval_minutes")
            if interval is None:
                interval = 60  # 默认 60 分钟
                updates["polling_interval_minutes"] = interval
            updates["next_sync_at"] = datetime.utcnow() + timedelta(minutes=interval)
        elif updates.get("sync_mode") is None and "sync_mode" in updates:
            # 如果显式设置 sync_mode 为 null（继承），清除 next_sync_at
            updates["next_sync_at"] = None

        updated_page = await svc.db.update_confluence_page_sync_config(
            page_id=page_record_id,
            updates=updates,
        )

        if not updated_page:
            raise HTTPException(status_code=400, detail="No valid fields to update")

        # 通知调度器配置已更新
        scheduler = getattr(request.app.state, "confluence_scheduler", None)
        if scheduler:
            new_sync_mode = updated_page.get("sync_mode")
            new_sync_enabled = updated_page.get("sync_enabled", True)
            new_interval = updated_page.get("polling_interval_minutes", 60)
            new_priority = updated_page.get("sync_priority", 0)

            if new_sync_mode == "polling" and new_sync_enabled:
                # 添加或更新页面轮询任务
                await scheduler.reschedule_page(
                    page_record_id=page_record_id,
                    interval_minutes=new_interval,
                    priority=new_priority,
                )
            else:
                # 移除页面轮询任务（如果存在）
                await scheduler.disable_page(page_record_id)

        return updated_page

    except ConfluenceAccessDeniedError as exc:
        raise HTTPException(status_code=403, detail=str(exc))
    except ConfluenceSyncError as exc:
        raise HTTPException(status_code=404, detail=str(exc))
    except PermissionDeniedError as exc:
        raise HTTPException(status_code=403, detail=str(exc))
    except HTTPException:
        raise
    except Exception as exc:
        logger.error(f"Update page sync config failed: {exc}")
        raise HTTPException(status_code=500, detail=str(exc))


@router.post("/pages/batch-sync")
async def batch_sync_pages(
    request: Request,
    payload: ConfluenceBatchSyncRequestSchema,
    user: UserContext = Depends(get_user_context),
):
    """
    Batch sync multiple pages by their record IDs.

    This endpoint accepts a list of page_record_ids (from confluence_pages table)
    and triggers sync for all of them. Pages are grouped by binding and synced
    as separate tasks.

    Returns task IDs for progress tracking.
    """
    try:
        request.app.state.dispatcher.rbac.require(user.roles, "confluence:sync")
        svc = get_confluence_sync_service(request)

        # 传递 user 以验证对每个页面所属 binding 的访问权限
        result = await svc.batch_sync_pages(
            page_record_ids=payload.page_record_ids,
            force=payload.force,
            user=user,
        )
        return result
    except ConfluenceAccessDeniedError as exc:
        raise HTTPException(status_code=403, detail=str(exc))
    except PermissionDeniedError as exc:
        raise HTTPException(status_code=403, detail=str(exc))
    except Exception as exc:
        logger.error(f"Batch sync failed: {exc}")
        raise HTTPException(status_code=500, detail=str(exc))


@router.post("/pages/remove", response_model=ConfluenceRemovePagesResultSchema)
async def remove_pages(
    request: Request,
    payload: ConfluenceRemovePagesRequestSchema,
    user: UserContext = Depends(get_user_context),
):
    """
    Remove pages from confluence_pages table.

    This removes page records and optionally deletes corresponding documents
    from the knowledge base. Use this to clean up unwanted pages that were
    added by mistake or are no longer needed.
    """
    try:
        request.app.state.dispatcher.rbac.require(user.roles, "confluence:manage")
        svc = get_confluence_sync_service(request)

        result = await svc.remove_pages(
            page_record_ids=payload.page_record_ids,
            user=user,
            delete_documents=payload.delete_documents,
        )
        return result
    except ConfluenceAccessDeniedError as exc:
        raise HTTPException(status_code=403, detail=str(exc))
    except PermissionDeniedError as exc:
        raise HTTPException(status_code=403, detail=str(exc))
    except Exception as exc:
        logger.error(f"Remove pages failed: {exc}")
        raise HTTPException(status_code=500, detail=str(exc))


# ============================================================
# Sync Tasks
# ============================================================


@router.get("/tasks", response_model=list[ConfluenceSyncTaskSchema])
async def list_sync_tasks(
    request: Request,
    binding_id: str | None = Query(None),
    status: str | None = Query(None),
    limit: int = Query(100, ge=1, le=500),
    user: UserContext = Depends(get_user_context),
):
    """List sync tasks."""
    try:
        svc = get_confluence_sync_service(request)
        tasks = await svc.list_sync_tasks(
            user=user,
            binding_id=binding_id,
            status=status,
            limit=limit,
        )
        return tasks
    except ConfluenceAccessDeniedError as exc:
        raise HTTPException(status_code=403, detail=str(exc))
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
        task = await svc.get_sync_task(task_id, user=user)
        if not task:
            raise HTTPException(status_code=404, detail="Task not found")
        return task
    except ConfluenceAccessDeniedError as exc:
        raise HTTPException(status_code=403, detail=str(exc))
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


def _connection_to_response(connection: dict[str, Any]) -> ConfluenceConnectionResponseSchema:
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
