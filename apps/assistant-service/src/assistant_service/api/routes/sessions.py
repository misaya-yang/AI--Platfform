"""Session management endpoints — CRUD + history."""

from __future__ import annotations

import uuid
from datetime import datetime

from fastapi import APIRouter, Depends, HTTPException, Query, Request

from ...auth import UserContext, get_user_context
from ..deps import get_session_manager

router = APIRouter()


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
    if not session or session.user_id != user.user_id:
        raise HTTPException(404, "Session not found")

    return {
        "session_id": session.session_id,
        "user_id": session.user_id,
        "created_at": session.created_at,
        "updated_at": session.updated_at,
        "metadata": session.metadata,
        "message_count": len(session.messages) if session.messages else 0,
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

    session = await sm.get(session_id)
    if not session or session.user_id != user.user_id:
        raise HTTPException(404, "Session not found")

    await sm.delete(session_id)
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
    if not session or session.user_id != user.user_id:
        raise HTTPException(404, "Session not found")

    messages = session.messages or []
    return {"messages": messages[-limit:], "total": len(messages)}
