from __future__ import annotations

import uuid
from datetime import datetime, timedelta
from typing import Dict, List, Optional

from ...models.session import Session, SessionMessage
from ...core.exceptions import PermissionDeniedError


class SessionManager:
    def __init__(
        self,
        default_session_ttl: int = 86400 * 7,
        anonymous_session_ttl: int = 86400,
    ):
        self._sessions: Dict[str, Session] = {}
        self.default_session_ttl = default_session_ttl
        self.anonymous_session_ttl = anonymous_session_ttl

    async def create(
        self,
        user_id: str,
        tenant_id: str = "",
        service_id: Optional[str] = None,
        session_id: Optional[str] = None,
        metadata: Optional[dict] = None,
        ttl: Optional[int] = None,
    ) -> Session:
        now = datetime.utcnow()
        expires_at = None
        ttl_seconds = ttl
        if ttl_seconds is None:
            ttl_seconds = (
                self.anonymous_session_ttl
                if (user_id or "").startswith("anon:")
                else self.default_session_ttl
            )
        if ttl_seconds and ttl_seconds > 0:
            expires_at = now + timedelta(seconds=int(ttl_seconds))
        session = Session(
            session_id=session_id or str(uuid.uuid4()),
            user_id=user_id,
            tenant_id=tenant_id,
            service_id=service_id,
            created_at=now,
            updated_at=now,
            expires_at=expires_at,
            metadata=metadata or {},
        )
        self._sessions[session.session_id] = session
        return session

    async def get(self, session_id: str) -> Optional[Session]:
        session = self._sessions.get(session_id)
        if not session:
            return None
        if session.expires_at and session.expires_at < datetime.utcnow():
            return None
        return session

    async def get_or_create(
        self,
        session_id: Optional[str],
        user_id: str,
        tenant_id: str = "",
        service_id: Optional[str] = None,
        ttl: Optional[int] = None,
    ) -> Session:
        if session_id:
            existing = await self.get(session_id)
            if existing:
                if existing.user_id != user_id or existing.tenant_id != tenant_id:
                    raise PermissionDeniedError("Session does not belong to current user")
                if service_id:
                    if existing.service_id and existing.service_id != service_id:
                        raise PermissionDeniedError("Session is bound to a different service")
                    if not existing.service_id:
                        existing.service_id = service_id
                return existing

        # Do not accept client-supplied unknown session IDs; mint a new one.
        return await self.create(
            user_id=user_id,
            tenant_id=tenant_id,
            service_id=service_id,
            session_id=None,
            ttl=ttl,
        )

    async def list_sessions(
        self,
        user_id: Optional[str] = None,
        tenant_id: Optional[str] = None,
        service_id: Optional[str] = None,
        limit: int = 100,
    ) -> List[Session]:
        now = datetime.utcnow()
        sessions = [
            s
            for s in self._sessions.values()
            if not s.expires_at or s.expires_at >= now
        ]
        if user_id:
            sessions = [s for s in sessions if s.user_id == user_id]
        if tenant_id:
            sessions = [s for s in sessions if s.tenant_id == tenant_id]
        if service_id:
            sessions = [s for s in sessions if s.service_id == service_id]
        sessions.sort(key=lambda s: s.updated_at or s.created_at, reverse=True)
        return sessions[:limit]

    async def delete(self, session_id: str) -> None:
        self._sessions.pop(session_id, None)

    async def add_message(
        self,
        session_id: str,
        role: str,
        content: str,
        metadata: Optional[dict] = None,
    ) -> None:
        session = self._sessions.get(session_id)
        if not session:
            return
        session.history.append(SessionMessage(role=role, content=content, metadata=metadata))
        # Auto-title from first user message (ChatGPT-like sidebar)
        if role == "user":
            session.metadata = session.metadata or {}
            if not session.metadata.get("title"):
                title = str(content).strip().splitlines()[0][:40]
                if title:
                    session.metadata["title"] = title
        session.updated_at = datetime.utcnow()

    async def history(self, session_id: str, limit: int = 50) -> List[SessionMessage]:
        session = self._sessions.get(session_id)
        if not session:
            return []
        return session.history[-limit:]
