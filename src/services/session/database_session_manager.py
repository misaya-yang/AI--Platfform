"""
数据库会话管理器

基于 PostgreSQL 的 SessionManager 实现，支持会话持久化
"""

from __future__ import annotations

import uuid
from datetime import datetime, timedelta
from typing import TYPE_CHECKING, Any

from ...core.exceptions import PermissionDeniedError
from ...models.session import Session, SessionMessage

if TYPE_CHECKING:
    from ...persistence.database import DatabaseStorage
    from ...persistence.redis import RedisStorage


class DatabaseSessionManager:
    """基于数据库的会话管理器"""

    def __init__(
        self,
        database: DatabaseStorage,
        redis: RedisStorage | None = None,
        cache_ttl: int = 3600,  # Redis 缓存 1 小时
        default_session_ttl: int = 86400 * 7,  # 默认会话有效期 7 天
        anonymous_session_ttl: int = 86400,  # 匿名会话有效期 24 小时
    ):
        self.database = database
        self.redis = redis
        self.cache_ttl = cache_ttl
        self.default_session_ttl = default_session_ttl
        self.anonymous_session_ttl = anonymous_session_ttl

        # 内存缓存，用于没有 Redis 时
        self._memory_cache: dict[str, Session] = {}

    async def create(
        self,
        user_id: str,
        tenant_id: str = "",
        service_id: str = None,
        session_id: str = None,
        metadata: dict | None = None,
        config: dict | None = None,
        ttl: int = None,
    ) -> Session:
        """创建新会话"""
        session_id_value = session_id or str(uuid.uuid4())
        now = datetime.utcnow()

        ttl_seconds = ttl
        if ttl_seconds is None:
            ttl_seconds = (
                self.anonymous_session_ttl
                if (user_id or "").startswith("anon:")
                else self.default_session_ttl
            )
        expires_at = now + timedelta(seconds=int(ttl_seconds)) if ttl_seconds else None

        session = Session(
            session_id=session_id_value,
            user_id=user_id,
            tenant_id=tenant_id,
            service_id=service_id,
            created_at=now,
            updated_at=now,
            metadata=metadata or {},
            history=[],
            state={},
            config=config or {},
            status="active",
            expires_at=expires_at,
        )

        # 保存到数据库
        await self._save_to_db(session)

        # 写入缓存
        await self._cache_session(session)

        return session

    async def get(self, session_id: str) -> Session | None:
        """获取会话"""
        # 尝试从缓存获取
        session = await self._get_from_cache(session_id)
        if session:
            return session

        # 从数据库获取
        session_dict = await self.database.get_session(session_id)
        if not session_dict:
            return None

        session = self._dict_to_session(session_dict)

        # 检查是否过期 (处理时区感知和时区无关的 datetime 比较)
        if session.expires_at:
            expires = (
                session.expires_at.replace(tzinfo=None)
                if session.expires_at.tzinfo
                else session.expires_at
            )
            if expires < datetime.utcnow():
                return None

        # 写入缓存
        await self._cache_session(session)

        return session

    async def get_or_create(
        self,
        session_id: str | None,
        user_id: str,
        tenant_id: str = "",
        service_id: str = None,
        ttl: int = None,
    ) -> Session:
        """获取或创建会话"""
        if session_id:
            session = await self.get(session_id)
            if session:
                if session.user_id != user_id or session.tenant_id != tenant_id:
                    raise PermissionDeniedError("Session does not belong to current user")
                if service_id:
                    if session.service_id and session.service_id != service_id:
                        raise PermissionDeniedError("Session is bound to a different service")
                    if not session.service_id:
                        session.service_id = service_id
                return session

        return await self.create(
            user_id=user_id,
            tenant_id=tenant_id,
            service_id=service_id,
            session_id=None,  # do not accept unknown client-provided IDs
            ttl=ttl,
        )

    async def list_sessions(
        self,
        user_id: str | None = None,
        tenant_id: str | None = None,
        service_id: str | None = None,
        status: str = "active",
        limit: int = 100,
    ) -> list[Session]:
        """获取会话列表"""
        sessions_dict = await self.database.list_sessions(
            user_id=user_id,
            tenant_id=tenant_id,
            service_id=service_id,
            status=status,
            limit=limit,
        )
        now = datetime.utcnow()
        sessions: list[Session] = []
        for row in sessions_dict:
            s = self._dict_to_session(row)
            if s.expires_at:
                expires = s.expires_at.replace(tzinfo=None) if s.expires_at.tzinfo else s.expires_at
                if expires < now:
                    continue
            sessions.append(s)
        return sessions

    async def delete(self, session_id: str) -> bool:
        """删除会话"""
        result = await self.database.delete_session(session_id)

        # 清除缓存
        await self._remove_from_cache(session_id)

        return result

    async def add_message(
        self,
        session_id: str,
        role: str,
        content: str,
        metadata: dict | None = None,
    ) -> bool:
        """
        添加消息到会话历史

        使用数据库原子操作追加消息，避免竞态条件导致的消息丢失。
        """
        now = datetime.utcnow()

        # 构建消息字典
        message_dict = {
            "role": role,
            "content": content,
            "timestamp": now.isoformat(),
            "metadata": metadata,
        }

        # Auto-title from first user message (ChatGPT-like sidebar)
        metadata_update = None
        if role == "user":
            # 检查是否需要设置标题
            session = await self.get(session_id)
            if session and not session.metadata.get("title"):
                title = str(content).strip().splitlines()[0][:40]
                if title:
                    metadata_update = {"title": title}

        # 使用原子操作追加消息（避免竞态条件）
        result = await self.database.append_session_message(
            session_id=session_id,
            message=message_dict,
            metadata_update=metadata_update,
        )

        if result:
            # 清除缓存，下次获取时从数据库重新加载
            await self._remove_from_cache(session_id)

        return result

    async def history(
        self,
        session_id: str,
        limit: int = 50,
    ) -> list[SessionMessage]:
        """获取会话历史"""
        session = await self.get(session_id)
        if not session:
            return []
        return session.history[-limit:]

    async def update_state(
        self,
        session_id: str,
        state: dict[str, Any],
    ) -> bool:
        """更新会话状态"""
        session = await self.get(session_id)
        if not session:
            return False

        session.state = state
        session.updated_at = datetime.utcnow()

        # 更新数据库
        await self.database.update_session_state(session_id, state)

        # 更新缓存
        await self._cache_session(session)

        return True

    async def update_metadata(
        self,
        session_id: str,
        metadata: dict[str, Any],
    ) -> bool:
        """更新会话元数据"""
        session = await self.get(session_id)
        if not session:
            return False

        session.metadata.update(metadata)
        session.updated_at = datetime.utcnow()

        # 保存到数据库
        await self._save_to_db(session)

        # 更新缓存
        await self._cache_session(session)

        return True

    async def update_config(
        self,
        session_id: str,
        config: dict[str, Any],
    ) -> bool:
        """更新会话配置（知识库、模型等）"""
        session = await self.get(session_id)
        if not session:
            return False

        # 确保 session.config 是字典类型
        existing_config = getattr(session, "config", None)
        if existing_config is None or not isinstance(existing_config, dict):
            existing_config = {}
        existing_config.update(config)
        session.config = existing_config
        session.updated_at = datetime.utcnow()

        # 保存到数据库
        await self._save_to_db(session)

        # 更新缓存
        await self._cache_session(session)

        return True

    async def extend_ttl(
        self,
        session_id: str,
        additional_seconds: int = None,
    ) -> bool:
        """延长会话有效期"""
        session = await self.get(session_id)
        if not session:
            return False

        seconds = additional_seconds or self.default_session_ttl
        session.expires_at = datetime.utcnow() + timedelta(seconds=seconds)
        session.updated_at = datetime.utcnow()

        # 保存到数据库
        await self._save_to_db(session)

        # 更新缓存
        await self._cache_session(session)

        return True

    async def cleanup_expired(self) -> int:
        """清理过期会话"""
        count = await self.database.cleanup_expired_sessions()
        return count

    # =========================================================================
    # 私有方法
    # =========================================================================

    async def _save_to_db(self, session: Session) -> None:
        """保存会话到数据库"""
        session_dict = self._session_to_dict(session)
        await self.database.save_session(session_dict)

    async def _cache_session(self, session: Session) -> None:
        """缓存会话"""
        if self.redis and self.redis.enabled:
            session_dict = self._session_to_dict(session)
            await self.redis.save_session(session.session_id, session_dict, self.cache_ttl)
        else:
            self._memory_cache[session.session_id] = session

    async def _get_from_cache(self, session_id: str) -> Session | None:
        """从缓存获取会话"""
        if self.redis and self.redis.enabled:
            cached = await self.redis.get_session(session_id)
            if cached:
                return self._dict_to_session(cached)
            return None
        else:
            return self._memory_cache.get(session_id)

    async def _remove_from_cache(self, session_id: str) -> None:
        """从缓存移除会话"""
        if self.redis and self.redis.enabled:
            await self.redis.delete_session(session_id)
        else:
            self._memory_cache.pop(session_id, None)

    def _session_to_dict(self, session: Session) -> dict[str, Any]:
        """将 Session 转换为字典"""
        return {
            "session_id": session.session_id,
            "service_id": session.service_id,
            "user_id": session.user_id,
            "tenant_id": session.tenant_id,
            "state": session.state or {},
            "history": [self._message_to_dict(m) for m in session.history],
            "metadata": session.metadata or {},
            "config": getattr(session, "config", {}) or {},
            "status": getattr(session, "status", "active"),
            "expires_at": session.expires_at,
        }

    def _message_to_dict(self, message: SessionMessage) -> dict[str, Any]:
        """将 SessionMessage 转换为字典"""
        return {
            "role": message.role,
            "content": message.content,
            "timestamp": message.timestamp.isoformat() if message.timestamp else None,
            "metadata": getattr(message, "metadata", None),
        }

    def _dict_to_session(self, data: dict[str, Any]) -> Session:
        """将字典转换为 Session"""
        history = []
        for msg_data in data.get("history", []):
            if isinstance(msg_data, dict):
                timestamp = msg_data.get("timestamp")
                if isinstance(timestamp, str):
                    timestamp = datetime.fromisoformat(timestamp.replace("Z", "+00:00"))
                history.append(
                    SessionMessage(
                        role=msg_data.get("role", "user"),
                        content=msg_data.get("content", ""),
                        timestamp=timestamp,
                        metadata=msg_data.get("metadata"),
                    )
                )

        created_at = data.get("created_at")
        if isinstance(created_at, str):
            created_at = datetime.fromisoformat(created_at.replace("Z", "+00:00"))

        updated_at = data.get("updated_at")
        if isinstance(updated_at, str):
            updated_at = datetime.fromisoformat(updated_at.replace("Z", "+00:00"))

        expires_at = data.get("expires_at")
        if isinstance(expires_at, str):
            expires_at = datetime.fromisoformat(expires_at.replace("Z", "+00:00"))

        return Session(
            session_id=data.get("session_id"),
            service_id=data.get("service_id"),
            user_id=data.get("user_id", ""),
            tenant_id=data.get("tenant_id", ""),
            created_at=created_at or datetime.utcnow(),
            updated_at=updated_at,
            metadata=data.get("metadata") or {},
            history=history,
            state=data.get("state") or {},
            config=data.get("config") or {},
            status=data.get("status", "active"),
            expires_at=expires_at,
        )
