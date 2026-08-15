"""Session management endpoints — CRUD + history."""

from __future__ import annotations

import uuid

from ai_gateway_core.logging import get_logger, record_internal_exception
from ai_gateway_core.tasks.task_manager import SessionDeletionBusyError
from fastapi import APIRouter, Depends, HTTPException, Query, Request

from ...auth import UserContext, get_user_context
from ...core.tasks.task_manager import get_task_manager
from ..deps import get_session_manager

router = APIRouter()
logger = get_logger(__name__)


@router.post("/sessions")
async def create_session(
    request: Request,
    user: UserContext = Depends(get_user_context),
):
    """Create a new conversation session."""
    sm = get_session_manager(request)
    if not sm:
        raise HTTPException(503, "Session manager not available")

    session_id = str(uuid.uuid4())
    session = await sm.create(
        session_id=session_id,
        user_id=user.user_id,
        tenant_id=user.tenant_id,
        service_id="__builtin_assistant__",
        metadata={},
    )
    return {
        "session_id": session.session_id,
        "created_at": session.created_at,
    }


@router.get("/sessions")
async def list_sessions(
    request: Request,
    limit: int = Query(50, ge=1, le=200),
    user: UserContext = Depends(get_user_context),
):
    """List user's conversation sessions."""
    sm = get_session_manager(request)
    if not sm:
        return []

    sessions = await sm.list_sessions(
        user_id=user.user_id,
        tenant_id=user.tenant_id,
        service_id="__builtin_assistant__",
        limit=limit,
    )
    return [
        {
            "session_id": s.session_id,
            "user_id": s.user_id,
            "created_at": s.created_at,
            "updated_at": s.updated_at,
            "metadata": s.metadata,
        }
        for s in sessions
    ]


@router.get("/sessions/{session_id}")
async def get_session(
    session_id: str,
    request: Request,
    user: UserContext = Depends(get_user_context),
):
    """Get session details."""
    sm = get_session_manager(request)
    if not sm:
        raise HTTPException(503, "Session manager not available")

    session = await sm.get(session_id)
    if not session or session.user_id != user.user_id or session.tenant_id != user.tenant_id:
        raise HTTPException(404, "Session not found")

    return {
        "session_id": session.session_id,
        "user_id": session.user_id,
        "created_at": session.created_at,
        "updated_at": session.updated_at,
        "metadata": session.metadata,
        "message_count": len(session.history) if session.history else 0,
    }


@router.delete("/sessions/{session_id}")
async def delete_session(
    session_id: str,
    request: Request,
    user: UserContext = Depends(get_user_context),
):
    """Delete a session."""
    sm = get_session_manager(request)
    if not sm:
        raise HTTPException(503, "Session manager not available")

    try:
        session = await sm.get(session_id)
    except Exception as exc:
        record_internal_exception(
            __name__, "assistant.api.routes.sessions.internal_failure", exc
        )
        raise HTTPException(503, "Session deletion storage is unavailable") from None
    # Gateway-created sessions live in the gateway schema during the staged
    # route migration.  They may therefore be absent from assistant.sessions;
    # the signed gateway identity still scopes all cleanup below.  A row that
    # does exist must continue to pass strict owner validation.
    if session and (
        session.user_id != user.user_id or session.tenant_id != user.tenant_id
    ):
        raise HTTPException(404, "Session not found")

    memory_service = getattr(request.app.state, "memory_service", None)
    if memory_service is None or not hasattr(
        memory_service,
        "delete_all_session_memories",
    ):
        raise HTTPException(503, "Session deletion storage is unavailable")

    task_manager = get_task_manager()
    try:
        session_context = task_manager.session_deletion_context(
            session_id=session_id,
            tenant_id=user.tenant_id,
            user_id=user.user_id,
        )
        async with session_context:
            try:
                memories_deleted = await memory_service.delete_all_session_memories(
                    tenant_id=user.tenant_id,
                    session_id=session_id,
                )
            except Exception as exc:
                record_internal_exception(
                    __name__, "assistant.api.routes.sessions.internal_failure", exc
                )
                memories_deleted = False
            if memories_deleted is not True:
                raise HTTPException(503, "Session deletion was not completed")

            assistant_service = getattr(request.app.state, "assistant_service", None)
            if assistant_service is not None:
                clear_runtime_state = getattr(
                    assistant_service,
                    "clear_session_runtime_state",
                    None,
                )
                if not callable(clear_runtime_state):
                    raise HTTPException(503, "Session deletion was not completed")
                try:
                    runtime_clearance = clear_runtime_state(
                        tenant_id=user.tenant_id,
                        user_id=user.user_id,
                        session_id=session_id,
                    )
                except Exception as exc:
                    record_internal_exception(
                        __name__, "assistant.api.routes.sessions.internal_failure", exc
                    )
                    raise HTTPException(503, "Session deletion was not completed") from None
                if (
                    not isinstance(runtime_clearance, dict)
                    or runtime_clearance.get("cleared") is not True
                ):
                    raise HTTPException(503, "Session deletion was not completed")

            try:
                durable_deleted = await sm.delete(session_id)
            except Exception as exc:
                record_internal_exception(
                    __name__, "assistant.api.routes.sessions.internal_failure", exc
                )
                durable_deleted = False
            if durable_deleted is not True:
                # DatabaseSessionManager can resolve a gateway-schema session
                # through the shared Redis cache while DELETE targets the
                # assistant schema.  Treat DELETE 0 as success only after a
                # cache-clearing readback proves no assistant row remains.
                try:
                    durable_remaining = await sm.get(session_id)
                except Exception as exc:
                    record_internal_exception(
                        __name__, "assistant.api.routes.sessions.internal_failure", exc
                    )
                    raise HTTPException(
                        503, "Session deletion was not completed"
                    ) from None
                if durable_remaining is not None:
                    raise HTTPException(503, "Session deletion was not completed")
    except HTTPException:
        raise
    except SessionDeletionBusyError:
        raise HTTPException(409, "Session has an active run") from None
    except PermissionError:
        raise HTTPException(409, "Session has conflicting live ownership") from None
    except Exception as exc:
        record_internal_exception(
            __name__, "assistant.api.routes.sessions.internal_failure", exc
        )
        raise HTTPException(503, "Session deletion was not completed") from None

    return {"status": "deleted", "session_id": session_id}


@router.get("/sessions/{session_id}/history")
async def get_session_history(
    session_id: str,
    request: Request,
    limit: int = Query(50, ge=1, le=200),
    user: UserContext = Depends(get_user_context),
):
    """Get session message history."""
    sm = get_session_manager(request)
    if not sm:
        raise HTTPException(503, "Session manager not available")

    session = await sm.get(session_id)
    if not session or session.user_id != user.user_id or session.tenant_id != user.tenant_id:
        raise HTTPException(404, "Session not found")

    messages = session.history or []
    return {"messages": messages[-limit:], "total": len(messages)}
