from __future__ import annotations

from typing import Any, Dict, Optional

from fastapi import APIRouter, Body, Depends, HTTPException, Query
from pydantic import BaseModel

from ..deps import get_session_manager, get_user_context
from ...core.auth.user_resolver import UserContext
from ...services.session.session_manager import SessionManager


router = APIRouter()


class SessionCreate(BaseModel):
    service_id: Optional[str] = None
    metadata: Optional[Dict[str, Any]] = None


class SessionUpdate(BaseModel):
    metadata: Optional[Dict[str, Any]] = None


class SessionMessageCreate(BaseModel):
    role: str
    content: Any
    metadata: Optional[Dict[str, Any]] = None


@router.get("/sessions")
async def list_sessions(
    service_id: Optional[str] = Query(default=None),
    limit: int = Query(100, ge=1, le=500),
    session_manager: SessionManager = Depends(get_session_manager),
    user: UserContext = Depends(get_user_context),
):
    sessions = await session_manager.list_sessions(
        user_id=user.user_id,
        tenant_id=user.tenant_id,
        service_id=service_id,
        limit=limit,
    )
    return [
        {
            "session_id": s.session_id,
            "user_id": s.user_id,
            "tenant_id": s.tenant_id,
            "service_id": getattr(s, "service_id", None),
            "created_at": s.created_at,
            "updated_at": s.updated_at,
            "metadata": s.metadata,
        }
        for s in sessions
    ]


@router.post("/sessions")
async def create_session(
    body: SessionCreate = Body(default_factory=SessionCreate),
    session_manager: SessionManager = Depends(get_session_manager),
    user: UserContext = Depends(get_user_context),
):
    session = await session_manager.create(
        user_id=user.user_id,
        tenant_id=user.tenant_id,
        service_id=body.service_id,
        metadata=body.metadata,
    )
    return {"session_id": session.session_id}


@router.get("/sessions/{session_id}")
async def get_session(
    session_id: str,
    session_manager: SessionManager = Depends(get_session_manager),
    user: UserContext = Depends(get_user_context),
):
    session = await session_manager.get(session_id)
    if not session or (session.user_id != user.user_id or session.tenant_id != user.tenant_id):
        raise HTTPException(status_code=404, detail="session not found")
    return {
        "session_id": session.session_id,
        "user_id": session.user_id,
        "tenant_id": session.tenant_id,
        "created_at": session.created_at,
        "updated_at": session.updated_at,
        "metadata": session.metadata,
    }


@router.patch("/sessions/{session_id}")
async def update_session(
    session_id: str,
    body: SessionUpdate,
    session_manager: SessionManager = Depends(get_session_manager),
    user: UserContext = Depends(get_user_context),
):
    session = await session_manager.get(session_id)
    if not session or (session.user_id != user.user_id or session.tenant_id != user.tenant_id):
        raise HTTPException(status_code=404, detail="session not found")
    
    if body.metadata:
        # 支持 update_metadata 方法（DatabaseSessionManager）或直接更新（内存版本）
        if hasattr(session_manager, 'update_metadata'):
            await session_manager.update_metadata(session_id, body.metadata)
        else:
            session.metadata = session.metadata or {}
            session.metadata.update(body.metadata)
    
    return {"session_id": session_id, "status": "updated"}


@router.delete("/sessions/{session_id}")
async def delete_session(
    session_id: str,
    session_manager: SessionManager = Depends(get_session_manager),
    user: UserContext = Depends(get_user_context),
):
    session = await session_manager.get(session_id)
    if not session or (session.user_id != user.user_id or session.tenant_id != user.tenant_id):
        raise HTTPException(status_code=404, detail="session not found")
    await session_manager.delete(session_id)
    return {"session_id": session_id, "status": "deleted"}


@router.post("/sessions/{session_id}/messages")
async def add_session_message(
    session_id: str,
    body: SessionMessageCreate,
    session_manager: SessionManager = Depends(get_session_manager),
    user: UserContext = Depends(get_user_context),
):
    session = await session_manager.get(session_id)
    if not session or (session.user_id != user.user_id or session.tenant_id != user.tenant_id):
        raise HTTPException(status_code=404, detail="session not found")

    await session_manager.add_message(
        session_id=session_id,
        role=body.role,
        content=body.content,
        metadata=body.metadata,
    )

    return {"session_id": session_id, "status": "added"}


@router.get("/sessions/{session_id}/history")
async def get_history(
    session_id: str,
    limit: int = Query(50, ge=1, le=500),
    session_manager: SessionManager = Depends(get_session_manager),
    user: UserContext = Depends(get_user_context),
):
    session = await session_manager.get(session_id)
    if not session or (session.user_id != user.user_id or session.tenant_id != user.tenant_id):
        raise HTTPException(status_code=404, detail="session not found")
    history = await session_manager.history(session_id, limit=limit)
    return [
        {
            "role": m.role,
            "content": m.content,
            "timestamp": m.timestamp,
            "metadata": m.metadata if hasattr(m, "metadata") and m.metadata else None,
        }
        for m in history
    ]
