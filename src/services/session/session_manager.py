from __future__ import annotations

import asyncio
import uuid
from collections import OrderedDict
from datetime import datetime, timedelta
from typing import Dict, List, Optional

from ...models.session import Session, SessionMessage
from ...core.exceptions import PermissionDeniedError


class SessionManager:
    """
    内存会话管理器，支持 LRU 淘汰和最大会话数限制。

    特性：
    - LRU 淘汰：当会话数超过上限时，淘汰最久未使用的会话
    - 定期清理：自动清理过期会话
    - 内存保护：防止内存无限增长
    - 线程安全：使用 asyncio.Lock 保护并发访问
    """

    def __init__(
        self,
        default_session_ttl: int = 86400 * 7,
        anonymous_session_ttl: int = 86400,
        max_sessions: int = 10000,
        cleanup_interval: int = 300,  # 每 5 分钟检查一次
    ):
        # 使用 OrderedDict 实现 LRU 缓存
        self._sessions: OrderedDict[str, Session] = OrderedDict()
        self.default_session_ttl = default_session_ttl
        self.anonymous_session_ttl = anonymous_session_ttl
        self.max_sessions = max_sessions
        self.cleanup_interval = cleanup_interval
        self._last_cleanup = datetime.utcnow()
        # Lock to protect concurrent access to _sessions
        self._lock = asyncio.Lock()

    async def _cleanup_expired(self, now: Optional[datetime] = None) -> int:
        """清理过期会话，返回清理数量。线程安全。"""
        if now is None:
            now = datetime.utcnow()
        async with self._lock:
            # Create snapshot to avoid modification during iteration
            expired_keys = [
                sid for sid, session in self._sessions.items()
                if session.expires_at and session.expires_at < now
            ]
            for sid in expired_keys:
                del self._sessions[sid]
            # 更新最后清理时间
            self._last_cleanup = now
            return len(expired_keys)

    async def _ensure_capacity(self) -> None:
        """确保会话数不超过上限，必要时执行 LRU 淘汰。线程安全。"""
        async with self._lock:
            while len(self._sessions) >= self.max_sessions:
                # OrderedDict 保证最左边（最早加入且未被 move_to_end）的是最久未使用的
                oldest_key, _ = self._sessions.popitem(last=False)

    async def create(
        self,
        user_id: str,
        tenant_id: str = "",
        service_id: Optional[str] = None,
        session_id: Optional[str] = None,
        metadata: Optional[dict] = None,
        ttl: Optional[int] = None,
    ) -> Session:
        # 定期清理过期会话
        now = datetime.utcnow()
        if (now - self._last_cleanup).total_seconds() >= self.cleanup_interval:
            await self._cleanup_expired(now)

        # 确保容量
        await self._ensure_capacity()

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

        # 使用 OrderedDict 的 move_to_end 实现 LRU
        async with self._lock:
            self._sessions[session.session_id] = session
        return session

    async def get(self, session_id: str) -> Optional[Session]:
        async with self._lock:
            session = self._sessions.get(session_id)
            if not session:
                return None

            # 检查过期
            if session.expires_at and session.expires_at < datetime.utcnow():
                del self._sessions[session_id]
                return None

            # 更新 LRU 顺序（移到末尾表示最近使用）
            self._sessions.move_to_end(session_id)
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
        async with self._lock:
            # Create snapshot to avoid modification during iteration
            sessions = [
                s
                for s in list(self._sessions.values())
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
        async with self._lock:
            self._sessions.pop(session_id, None)

    async def add_message(
        self,
        session_id: str,
        role: str,
        content: str,
        metadata: Optional[dict] = None,
    ) -> None:
        async with self._lock:
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
            # 更新 LRU 顺序
            self._sessions.move_to_end(session_id)

    async def history(self, session_id: str, limit: int = 50) -> List[SessionMessage]:
        async with self._lock:
            session = self._sessions.get(session_id)
            if not session:
                return []
            # Return a copy to avoid external modification
            return list(session.history[-limit:])

    async def get_stats(self) -> dict:
        """获取会话统计信息"""
        now = datetime.utcnow()
        async with self._lock:
            total = len(self._sessions)
            expired = sum(
                1 for s in self._sessions.values()
                if s.expires_at and s.expires_at < now
            )
        return {
            "total_sessions": total,
            "active_sessions": total - expired,
            "expired_sessions": expired,
            "max_sessions": self.max_sessions,
        }
