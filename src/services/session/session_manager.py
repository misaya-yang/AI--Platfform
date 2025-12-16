from __future__ import annotations

import uuid
from datetime import datetime
from typing import Dict, List, Optional

from ...models.session import Session, SessionMessage


class SessionManager:
    def __init__(self):
        self._sessions: Dict[str, Session] = {}

    async def create(
        self,
        user_id: str,
        tenant_id: str = "",
        service_id: Optional[str] = None,
        session_id: Optional[str] = None,
        metadata: Optional[dict] = None,
    ) -> Session:
        session = Session(
            session_id=session_id or str(uuid.uuid4()),
            user_id=user_id,
            tenant_id=tenant_id,
            service_id=service_id,
            created_at=datetime.utcnow(),
            metadata=metadata or {},
        )
        self._sessions[session.session_id] = session
        return session

    async def get(self, session_id: str) -> Optional[Session]:
        return self._sessions.get(session_id)

    async def get_or_create(
        self,
        session_id: Optional[str],
        user_id: str,
        tenant_id: str = "",
        service_id: Optional[str] = None,
    ) -> Session:
        if session_id and session_id in self._sessions:
            session = self._sessions[session_id]
            if service_id and not session.service_id:
                session.service_id = service_id
            return session
        return await self.create(
            user_id=user_id,
            tenant_id=tenant_id,
            service_id=service_id,
            session_id=session_id,
        )

    async def list_sessions(
        self,
        user_id: Optional[str] = None,
        tenant_id: Optional[str] = None,
        service_id: Optional[str] = None,
        limit: int = 100,
    ) -> List[Session]:
        sessions = list(self._sessions.values())
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
