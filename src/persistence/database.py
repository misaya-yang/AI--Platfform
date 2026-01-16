"""
PostgreSQL 数据库存储层

提供与 database/schema.sql 表结构对应的完整 CRUD 操作
"""
from __future__ import annotations

import asyncio
import json
import logging
import time
from datetime import date, datetime
from pathlib import Path
from typing import Any, Dict, List, Optional

try:
    import asyncpg
    HAS_ASYNCPG = True
except ImportError:
    HAS_ASYNCPG = False
    asyncpg = None

logger = logging.getLogger(__name__)


class DatabaseStorage:
    """PostgreSQL 数据库存储"""

    def __init__(
        self,
        dsn: Optional[str] = None,
        enabled: bool = False,
        auto_init: bool = True,
        schema_path: Optional[str] = None,
        permission_cache_ttl_seconds: int = 60,
        pool_min_size: int = 2,
        pool_max_size: int = 10,
    ):
        self.dsn = dsn
        self.enabled = enabled and HAS_ASYNCPG and dsn
        self.auto_init = bool(auto_init)
        self.schema_path = schema_path or str(
            Path(__file__).resolve().parent.parent.parent / "database" / "schema.sql"
        )
        self._pool: Optional[Any] = None
        self._pool_min_size = max(int(pool_min_size), 1)
        self._pool_max_size = max(int(pool_max_size), self._pool_min_size)
        self._permission_cache: Dict[str, tuple[List[str], float]] = {}
        self._permission_cache_ttl_seconds = max(int(permission_cache_ttl_seconds or 0), 0)
        self._permission_cache_lock = asyncio.Lock()

    async def _get_cached_permissions(self, user_id: str) -> Optional[List[str]]:
        if self._permission_cache_ttl_seconds <= 0:
            return None
        async with self._permission_cache_lock:
            entry = self._permission_cache.get(user_id)
            if not entry:
                return None
            permissions, expires_at = entry
            if time.time() >= expires_at:
                self._permission_cache.pop(user_id, None)
                return None
            return list(permissions)

    async def _set_cached_permissions(self, user_id: str, permissions: List[str]) -> None:
        if self._permission_cache_ttl_seconds <= 0:
            return
        async with self._permission_cache_lock:
            self._permission_cache[user_id] = (
                list(permissions),
                time.time() + self._permission_cache_ttl_seconds,
            )

    async def _invalidate_permission_cache(self, user_id: Optional[str] = None) -> None:
        async with self._permission_cache_lock:
            if user_id:
                self._permission_cache.pop(user_id, None)
            else:
                self._permission_cache.clear()

    async def connect(self) -> None:
        """建立数据库连接池"""
        if not self.enabled:
            return
        if not HAS_ASYNCPG:
            raise RuntimeError("asyncpg is not installed. Run: pip install asyncpg")
        self._pool = await asyncpg.create_pool(
            self.dsn,
            min_size=self._pool_min_size,
            max_size=self._pool_max_size,
        )
        logger.info(f"Database pool created: min_size={self._pool_min_size}, max_size={self._pool_max_size}")
        if self.auto_init:
            await self._auto_initialize_schema()
            await self._auto_apply_account_permission_migration()
            await self._auto_apply_user_extra_permissions_migration()

    async def close(self) -> None:
        """关闭连接池"""
        if self._pool:
            await self._pool.close()
            self._pool = None

    async def fetchrow(self, query: str, *args) -> Optional[Any]:
        """执行查询并返回单行结果"""
        if not self._pool:
            return None
        async with self._pool.acquire() as conn:
            return await conn.fetchrow(query, *args)

    async def fetch(self, query: str, *args) -> List[Any]:
        """执行查询并返回多行结果"""
        if not self._pool:
            return []
        async with self._pool.acquire() as conn:
            return await conn.fetch(query, *args)

    async def execute(self, query: str, *args) -> str:
        """执行 SQL 语句（INSERT/UPDATE/DELETE）并返回状态字符串"""
        if not self._pool:
            return ""
        async with self._pool.acquire() as conn:
            return await conn.execute(query, *args)

    async def execute_schema(self, schema_path: str) -> None:
        """执行 SQL 建表脚本"""
        if not self._pool:
            return
        with open(schema_path, 'r', encoding='utf-8') as f:
            sql = f.read()
        async with self._pool.acquire() as conn:
            await conn.execute(sql)

    async def _schema_is_missing(self) -> bool:
        """Detect whether core tables are missing (e.g., first run)."""
        if not self._pool:
            return False
        async with self._pool.acquire() as conn:
            # Use to_regclass for a cheap existence check.
            services = await conn.fetchval("SELECT to_regclass('public.services')")
            datasets = await conn.fetchval("SELECT to_regclass('public.datasets')")
            return services is None or datasets is None

    async def _auto_initialize_schema(self) -> None:
        """Auto-run schema.sql when core tables are missing (idempotent)."""
        if not self._pool:
            return
        try:
            if not await self._schema_is_missing():
                return
        except Exception:
            # If we cannot check, skip auto-init to avoid masking the real error.
            return

        schema_path = self.schema_path
        if not schema_path or not Path(schema_path).exists():
            raise RuntimeError(f"Database schema not found: {schema_path}")

        await self.execute_schema(schema_path)

    async def _account_permission_schema_missing(self) -> bool:
        """Check if account/permission schema pieces are missing."""
        if not self._pool:
            return False
        async with self._pool.acquire() as conn:
            password_col = await conn.fetchval(
                """
                SELECT 1
                FROM information_schema.columns
                WHERE table_name = 'users' AND column_name = 'password_hash'
                """
            )
            permissions_table = await conn.fetchval(
                "SELECT to_regclass('public.permissions')"
            )
            return password_col is None or permissions_table is None

    async def _auto_apply_account_permission_migration(self) -> None:
        """Apply account/permission migration when required."""
        if not self._pool:
            return
        try:
            missing = await self._account_permission_schema_missing()
        except Exception:
            # If we cannot determine schema state, skip auto-migration.
            return
        if not missing:
            return

        migration_path = (
            Path(__file__).resolve().parent.parent.parent
            / "database"
            / "migrations"
            / "005_account_permission_system.sql"
        )
        if not migration_path.exists():
            raise RuntimeError(f"Migration not found: {migration_path}")

        await self.execute_schema(str(migration_path))

    async def _user_permissions_schema_missing(self) -> bool:
        """Check if user_permissions table is missing."""
        if not self._pool:
            return False
        async with self._pool.acquire() as conn:
            table_exists = await conn.fetchval(
                "SELECT to_regclass('public.user_permissions')"
            )
            return table_exists is None

    async def _auto_apply_user_extra_permissions_migration(self) -> None:
        """Apply user extra permissions migration (006) when required."""
        if not self._pool:
            return
        try:
            missing = await self._user_permissions_schema_missing()
        except Exception as e:
            logger.warning(f"Could not check user_permissions schema: {e}")
            return
        if not missing:
            return

        migration_path = (
            Path(__file__).resolve().parent.parent.parent
            / "database"
            / "migrations"
            / "006_user_extra_permissions.sql"
        )
        if not migration_path.exists():
            logger.warning(f"Migration 006 not found: {migration_path}")
            return

        try:
            await self.execute_schema(str(migration_path))
            logger.info("Applied migration 006_user_extra_permissions.sql")
        except Exception as e:
            # If table already exists or other non-critical error, log and continue
            if "already exists" in str(e).lower():
                logger.info("Migration 006 already applied (user_permissions table exists)")
            else:
                logger.error(f"Failed to apply migration 006: {e}")

    # =========================================================================
    # 服务定义表 (services)
    # =========================================================================

    async def save_service(self, service: Dict[str, Any]) -> None:
        """保存或更新服务"""
        if not self._pool:
            return
        async with self._pool.acquire() as conn:
            await conn.execute("""
                INSERT INTO services (
                    service_id, name, description, version, service_type, 
                    connector_type, connector_config, supported_modes, 
                    accepted_content_types, output_content_types,
                    input_schema, output_schema, session_enabled, session_adapter,
                    timeout, max_retries, retry_delay, circuit_breaker_enabled,
                    failure_threshold, recovery_timeout, rate_limit, concurrency_limit,
                    service_config, async_config, status, tags, metadata
                ) VALUES (
                    $1, $2, $3, $4, $5, $6, $7, $8, $9, $10,
                    $11, $12, $13, $14, $15, $16, $17, $18, $19, $20,
                    $21, $22, $23, $24, $25, $26, $27
                )
                ON CONFLICT (service_id) DO UPDATE SET
                    name = EXCLUDED.name,
                    description = EXCLUDED.description,
                    version = EXCLUDED.version,
                    service_type = EXCLUDED.service_type,
                    connector_type = EXCLUDED.connector_type,
                    connector_config = EXCLUDED.connector_config,
                    supported_modes = EXCLUDED.supported_modes,
                    accepted_content_types = EXCLUDED.accepted_content_types,
                    output_content_types = EXCLUDED.output_content_types,
                    input_schema = EXCLUDED.input_schema,
                    output_schema = EXCLUDED.output_schema,
                    session_enabled = EXCLUDED.session_enabled,
                    session_adapter = EXCLUDED.session_adapter,
                    timeout = EXCLUDED.timeout,
                    max_retries = EXCLUDED.max_retries,
                    retry_delay = EXCLUDED.retry_delay,
                    circuit_breaker_enabled = EXCLUDED.circuit_breaker_enabled,
                    failure_threshold = EXCLUDED.failure_threshold,
                    recovery_timeout = EXCLUDED.recovery_timeout,
                    rate_limit = EXCLUDED.rate_limit,
                    concurrency_limit = EXCLUDED.concurrency_limit,
                    service_config = EXCLUDED.service_config,
                    async_config = EXCLUDED.async_config,
                    status = EXCLUDED.status,
                    tags = EXCLUDED.tags,
                    metadata = EXCLUDED.metadata,
                    updated_at = NOW()
            """,
                service.get("service_id"),
                service.get("name"),
                service.get("description"),
                service.get("version", "1.0.0"),
                service.get("service_type", "custom"),
                service.get("connector_type", "http"),
                json.dumps(service.get("connector_config", {})),
                service.get("supported_modes", ["sync", "stream"]),
                service.get("accepted_content_types", ["text"]),
                service.get("output_content_types", ["text"]),
                json.dumps(service.get("input_schema", {})),
                json.dumps(service.get("output_schema", {})),
                service.get("session_enabled", False),
                service.get("session_adapter"),
                service.get("timeout", 60),
                service.get("max_retries", 3),
                service.get("retry_delay", 1.0),
                service.get("circuit_breaker_enabled", True),
                service.get("failure_threshold", 5),
                service.get("recovery_timeout", 30),
                json.dumps(service.get("rate_limit")) if service.get("rate_limit") else None,
                service.get("concurrency_limit"),
                json.dumps(service.get("service_config", {})),
                json.dumps(service.get("async_config")) if service.get("async_config") else None,
                service.get("status", "active"),
                service.get("tags", []),
                json.dumps(service.get("metadata", {})),
            )

    async def get_service(self, service_id: str) -> Optional[Dict[str, Any]]:
        """获取服务定义"""
        if not self._pool:
            return None
        async with self._pool.acquire() as conn:
            row = await conn.fetchrow(
                "SELECT * FROM services WHERE service_id = $1", service_id
            )
            return self._row_to_dict(row) if row else None

    async def list_services(
        self, 
        status: Optional[str] = None,
        service_type: Optional[str] = None,
        tags: Optional[List[str]] = None
    ) -> List[Dict[str, Any]]:
        """获取服务列表"""
        if not self._pool:
            return []
        
        query = "SELECT * FROM services WHERE 1=1"
        params = []
        param_idx = 1
        
        if status:
            query += f" AND status = ${param_idx}"
            params.append(status)
            param_idx += 1
        
        if service_type:
            query += f" AND service_type = ${param_idx}"
            params.append(service_type)
            param_idx += 1
        
        if tags:
            query += f" AND tags && ${param_idx}"
            params.append(tags)
            param_idx += 1
        
        query += " ORDER BY created_at DESC"
        
        async with self._pool.acquire() as conn:
            rows = await conn.fetch(query, *params)
            return [self._row_to_dict(row) for row in rows]

    async def delete_service(self, service_id: str) -> bool:
        """删除服务"""
        if not self._pool:
            return False
        async with self._pool.acquire() as conn:
            result = await conn.execute(
                "DELETE FROM services WHERE service_id = $1", service_id
            )
            return result == "DELETE 1"

    async def update_service_status(self, service_id: str, status: str) -> None:
        """更新服务状态"""
        if not self._pool:
            return
        async with self._pool.acquire() as conn:
            await conn.execute(
                "UPDATE services SET status = $1, updated_at = NOW() WHERE service_id = $2",
                status, service_id
            )

    # =========================================================================
    # 会话表 (sessions)
    # =========================================================================

    async def save_session(self, session: Dict[str, Any]) -> None:
        """保存或更新会话"""
        if not self._pool:
            return
        async with self._pool.acquire() as conn:
            await conn.execute("""
                INSERT INTO sessions (
                    session_id, service_id, user_id, tenant_id,
                    state, history, metadata, config, status, expires_at
                ) VALUES ($1, $2, $3, $4, $5, $6, $7, $8, $9, $10)
                ON CONFLICT (session_id) DO UPDATE SET
                    service_id = EXCLUDED.service_id,
                    state = EXCLUDED.state,
                    history = EXCLUDED.history,
                    metadata = EXCLUDED.metadata,
                    config = EXCLUDED.config,
                    status = EXCLUDED.status,
                    expires_at = EXCLUDED.expires_at,
                    updated_at = NOW()
            """,
                session.get("session_id"),
                session.get("service_id"),
                session.get("user_id"),
                session.get("tenant_id"),
                json.dumps(session.get("state", {})),
                json.dumps(session.get("history", [])),
                json.dumps(session.get("metadata", {})),
                json.dumps(session.get("config", {})),
                session.get("status", "active"),
                session.get("expires_at"),
            )

    async def get_session(self, session_id: str) -> Optional[Dict[str, Any]]:
        """获取会话"""
        if not self._pool:
            return None
        async with self._pool.acquire() as conn:
            row = await conn.fetchrow(
                "SELECT * FROM sessions WHERE session_id = $1", session_id
            )
            return self._row_to_dict(row) if row else None

    async def append_session_message(
        self,
        session_id: str,
        message: Dict[str, Any],
        metadata_update: Optional[Dict[str, Any]] = None,
    ) -> bool:
        """
        原子追加消息到会话历史（避免竞态条件）

        使用 PostgreSQL 的 JSONB 操作原子地追加消息，而不是读取-修改-写入模式。

        Args:
            session_id: 会话 ID
            message: 消息字典 {role, content, timestamp, metadata}
            metadata_update: 可选的 metadata 更新（如自动标题）

        Returns:
            是否成功
        """
        if not self._pool:
            return False
        async with self._pool.acquire() as conn:
            # 使用 JSONB || 操作符原子追加消息
            if metadata_update:
                result = await conn.execute("""
                    UPDATE sessions
                    SET history = history || $2::jsonb,
                        metadata = metadata || $3::jsonb,
                        updated_at = NOW()
                    WHERE session_id = $1
                """, session_id, json.dumps([message]), json.dumps(metadata_update))
            else:
                result = await conn.execute("""
                    UPDATE sessions
                    SET history = history || $2::jsonb,
                        updated_at = NOW()
                    WHERE session_id = $1
                """, session_id, json.dumps([message]))
            return result == "UPDATE 1"

    async def list_sessions(
        self,
        user_id: Optional[str] = None,
        tenant_id: Optional[str] = None,
        service_id: Optional[str] = None,
        status: str = "active",
        limit: int = 100
    ) -> List[Dict[str, Any]]:
        """获取会话列表"""
        if not self._pool:
            return []
        
        query = "SELECT * FROM sessions WHERE 1=1"
        params = []
        param_idx = 1
        
        if user_id:
            query += f" AND user_id = ${param_idx}"
            params.append(user_id)
            param_idx += 1
        
        if tenant_id:
            query += f" AND tenant_id = ${param_idx}"
            params.append(tenant_id)
            param_idx += 1
        
        if service_id:
            query += f" AND service_id = ${param_idx}"
            params.append(service_id)
            param_idx += 1
        
        if status:
            query += f" AND status = ${param_idx}"
            params.append(status)
            param_idx += 1

        # By default, hide expired sessions from list views.
        if status == "active":
            query += " AND (expires_at IS NULL OR expires_at > NOW())"
        
        query += f" ORDER BY updated_at DESC LIMIT ${param_idx}"
        params.append(limit)
        
        async with self._pool.acquire() as conn:
            rows = await conn.fetch(query, *params)
            return [self._row_to_dict(row) for row in rows]

    async def update_session_history(
        self, 
        session_id: str, 
        history: List[Dict[str, Any]]
    ) -> None:
        """更新会话历史"""
        if not self._pool:
            return
        async with self._pool.acquire() as conn:
            await conn.execute(
                "UPDATE sessions SET history = $1, updated_at = NOW() WHERE session_id = $2",
                json.dumps(history), session_id
            )

    async def update_session_state(
        self, 
        session_id: str, 
        state: Dict[str, Any]
    ) -> None:
        """更新会话状态"""
        if not self._pool:
            return
        async with self._pool.acquire() as conn:
            await conn.execute(
                "UPDATE sessions SET state = $1, updated_at = NOW() WHERE session_id = $2",
                json.dumps(state), session_id
            )

    async def delete_session(self, session_id: str) -> bool:
        """删除会话"""
        if not self._pool:
            return False
        async with self._pool.acquire() as conn:
            result = await conn.execute(
                "DELETE FROM sessions WHERE session_id = $1", session_id
            )
            return result == "DELETE 1"

    async def cleanup_expired_sessions(self) -> int:
        """清理过期会话"""
        if not self._pool:
            return 0
        async with self._pool.acquire() as conn:
            result = await conn.execute(
                "DELETE FROM sessions WHERE expires_at IS NOT NULL AND expires_at < NOW()"
            )
            # 解析结果获取删除行数
            if result.startswith("DELETE "):
                return int(result.split()[-1])
            return 0

    # =========================================================================
    # 异步任务表 (tasks)
    # =========================================================================

    async def save_task(self, task: Dict[str, Any]) -> None:
        """保存或更新任务"""
        if not self._pool:
            return
        async with self._pool.acquire() as conn:
            await conn.execute("""
                INSERT INTO tasks (
                    task_id, request_id, service_id, user_id, tenant_id,
                    status, progress, request_data, result, error,
                    callback_url, callback_sent, priority, retry_count, max_retries,
                    metadata, started_at, completed_at
                ) VALUES (
                    $1, $2, $3, $4, $5, $6, $7, $8, $9, $10,
                    $11, $12, $13, $14, $15, $16, $17, $18
                )
                ON CONFLICT (task_id) DO UPDATE SET
                    status = EXCLUDED.status,
                    progress = EXCLUDED.progress,
                    result = EXCLUDED.result,
                    error = EXCLUDED.error,
                    callback_sent = EXCLUDED.callback_sent,
                    retry_count = EXCLUDED.retry_count,
                    metadata = EXCLUDED.metadata,
                    started_at = EXCLUDED.started_at,
                    completed_at = EXCLUDED.completed_at,
                    updated_at = NOW()
            """,
                task.get("task_id"),
                task.get("request_id"),
                task.get("service_id"),
                task.get("user_id"),
                task.get("tenant_id"),
                task.get("status", "pending"),
                task.get("progress", 0),
                json.dumps(task.get("request_data")) if task.get("request_data") else None,
                json.dumps(task.get("result")) if task.get("result") else None,
                task.get("error"),
                task.get("callback_url"),
                task.get("callback_sent", False),
                task.get("priority", 0),
                task.get("retry_count", 0),
                task.get("max_retries", 3),
                json.dumps(task.get("metadata", {})),
                task.get("started_at"),
                task.get("completed_at"),
            )

    async def get_task(self, task_id: str) -> Optional[Dict[str, Any]]:
        """获取任务"""
        if not self._pool:
            return None
        async with self._pool.acquire() as conn:
            row = await conn.fetchrow(
                "SELECT * FROM tasks WHERE task_id = $1", task_id
            )
            return self._row_to_dict(row) if row else None

    async def list_tasks(
        self,
        user_id: Optional[str] = None,
        tenant_id: Optional[str] = None,
        service_id: Optional[str] = None,
        status: Optional[str] = None,
        limit: int = 100,
        offset: int = 0
    ) -> List[Dict[str, Any]]:
        """获取任务列表"""
        if not self._pool:
            return []
        
        query = "SELECT * FROM tasks WHERE 1=1"
        params = []
        param_idx = 1
        
        if user_id:
            query += f" AND user_id = ${param_idx}"
            params.append(user_id)
            param_idx += 1
        
        if tenant_id:
            query += f" AND tenant_id = ${param_idx}"
            params.append(tenant_id)
            param_idx += 1
        
        if service_id:
            query += f" AND service_id = ${param_idx}"
            params.append(service_id)
            param_idx += 1
        
        if status:
            query += f" AND status = ${param_idx}"
            params.append(status)
            param_idx += 1
        
        query += f" ORDER BY created_at DESC LIMIT ${param_idx} OFFSET ${param_idx + 1}"
        params.extend([limit, offset])
        
        async with self._pool.acquire() as conn:
            rows = await conn.fetch(query, *params)
            return [self._row_to_dict(row) for row in rows]

    async def update_task_status(
        self,
        task_id: str,
        status: str,
        progress: float = None,
        result: Any = None,
        error: str = None
    ) -> None:
        """更新任务状态"""
        if not self._pool:
            return
        
        updates = ["status = $1", "updated_at = NOW()"]
        params = [status]
        param_idx = 2
        
        if progress is not None:
            updates.append(f"progress = ${param_idx}")
            params.append(progress)
            param_idx += 1
        
        if result is not None:
            updates.append(f"result = ${param_idx}")
            params.append(json.dumps(result))
            param_idx += 1
        
        if error is not None:
            updates.append(f"error = ${param_idx}")
            params.append(error)
            param_idx += 1
        
        if status == "processing":
            updates.append(f"started_at = ${param_idx}")
            params.append(datetime.utcnow())
            param_idx += 1
        elif status in ("completed", "failed", "cancelled"):
            updates.append(f"completed_at = ${param_idx}")
            params.append(datetime.utcnow())
            param_idx += 1
        
        params.append(task_id)
        query = f"UPDATE tasks SET {', '.join(updates)} WHERE task_id = ${param_idx}"
        
        async with self._pool.acquire() as conn:
            await conn.execute(query, *params)

    async def mark_callback_sent(self, task_id: str) -> None:
        """标记回调已发送"""
        if not self._pool:
            return
        async with self._pool.acquire() as conn:
            await conn.execute(
                "UPDATE tasks SET callback_sent = TRUE, updated_at = NOW() WHERE task_id = $1",
                task_id
            )

    async def get_pending_tasks(self, limit: int = 10) -> List[Dict[str, Any]]:
        """获取待处理任务"""
        if not self._pool:
            return []
        async with self._pool.acquire() as conn:
            rows = await conn.fetch("""
                SELECT * FROM tasks 
                WHERE status = 'pending' 
                ORDER BY priority DESC, created_at ASC 
                LIMIT $1
            """, limit)
            return [self._row_to_dict(row) for row in rows]

    # =========================================================================
    # Knowledge Base (KBMS)
    # =========================================================================

    async def save_dataset(self, dataset: Dict[str, Any]) -> None:
        """保存或更新知识库 Dataset"""
        if not self._pool:
            return
        async with self._pool.acquire() as conn:
            await conn.execute(
                """
                INSERT INTO datasets (
                    dataset_id, name, description, tenant_id, visibility,
                    embedding_provider, embedding_model, embedding_dimension,
                    embedding_config, index_config, collection_name, created_by
                ) VALUES (
                    $1, $2, $3, $4, $5,
                    $6, $7, $8,
                    $9, $10, $11, $12
                )
                ON CONFLICT (dataset_id) DO UPDATE SET
                    name = EXCLUDED.name,
                    description = EXCLUDED.description,
                    tenant_id = EXCLUDED.tenant_id,
                    visibility = EXCLUDED.visibility,
                    embedding_provider = EXCLUDED.embedding_provider,
                    embedding_model = EXCLUDED.embedding_model,
                    embedding_dimension = EXCLUDED.embedding_dimension,
                    embedding_config = EXCLUDED.embedding_config,
                    index_config = EXCLUDED.index_config,
                    collection_name = EXCLUDED.collection_name,
                    created_by = EXCLUDED.created_by,
                    updated_at = NOW()
                """,
                dataset.get("dataset_id"),
                dataset.get("name"),
                dataset.get("description"),
                dataset.get("tenant_id", ""),
                dataset.get("visibility", "private"),
                dataset.get("embedding_provider", "openai"),
                dataset.get("embedding_model", "text-embedding-3-small"),
                int(dataset.get("embedding_dimension") or 0) or 1536,
                json.dumps(dataset.get("embedding_config", {})),
                json.dumps(dataset.get("index_config", {})),
                dataset.get("collection_name"),
                dataset.get("created_by"),
            )

    async def get_dataset(self, dataset_id: str) -> Optional[Dict[str, Any]]:
        """获取 Dataset"""
        if not self._pool:
            return None
        async with self._pool.acquire() as conn:
            row = await conn.fetchrow(
                "SELECT * FROM datasets WHERE dataset_id = $1", dataset_id
            )
            return self._row_to_dict(row) if row else None

    async def list_datasets(
        self,
        tenant_id: Optional[str] = None,
        include_public: bool = True,
        limit: int = 100,
        offset: int = 0,
    ) -> List[Dict[str, Any]]:
        """列出 Dataset"""
        if not self._pool:
            return []

        query = "SELECT * FROM datasets WHERE 1=1"
        params: List[Any] = []
        param_idx = 1

        if tenant_id:
            if include_public:
                query += f" AND (tenant_id = ${param_idx} OR visibility = 'public')"
                params.append(tenant_id)
                param_idx += 1
            else:
                query += f" AND tenant_id = ${param_idx}"
                params.append(tenant_id)
                param_idx += 1
        else:
            if not include_public:
                query += " AND visibility != 'public'"

        query += (
            f" ORDER BY created_at DESC LIMIT ${param_idx} OFFSET ${param_idx + 1}"
        )
        params.extend([limit, offset])

        async with self._pool.acquire() as conn:
            rows = await conn.fetch(query, *params)
            return [self._row_to_dict(row) for row in rows]

    async def delete_dataset(self, dataset_id: str) -> bool:
        """删除 Dataset（级联删除文档/片段/权限）"""
        if not self._pool:
            return False
        async with self._pool.acquire() as conn:
            result = await conn.execute(
                "DELETE FROM datasets WHERE dataset_id = $1", dataset_id
            )
            if result.startswith("DELETE "):
                return int(result.split()[-1]) > 0
            return False

    async def get_datasets_statistics_batch(
        self, dataset_ids: List[str]
    ) -> Dict[str, Dict[str, int]]:
        """获取多个 Dataset 的统计数据（批量查询优化）"""
        if not self._pool or not dataset_ids:
            return {}

        async with self._pool.acquire() as conn:
            # Use a single query with LEFT JOINs and GROUP BY for efficiency
            query = """
                SELECT
                    d.dataset_id,
                    COUNT(DISTINCT doc.document_id) as document_count,
                    COUNT(DISTINCT seg.segment_id) as segment_count
                FROM datasets d
                LEFT JOIN documents doc ON d.dataset_id = doc.dataset_id
                LEFT JOIN segments seg ON d.dataset_id = seg.dataset_id
                WHERE d.dataset_id = ANY($1)
                GROUP BY d.dataset_id
            """
            rows = await conn.fetch(query, dataset_ids)

            result: Dict[str, Dict[str, int]] = {}
            for row in rows:
                result[row["dataset_id"]] = {
                    "document_count": row["document_count"] or 0,
                    "segment_count": row["segment_count"] or 0,
                }

            # Ensure all requested dataset_ids have entries (even if empty)
            for ds_id in dataset_ids:
                if ds_id not in result:
                    result[ds_id] = {"document_count": 0, "segment_count": 0}

            return result

    async def grant_dataset_permission(
        self,
        dataset_id: str,
        subject_type: str,
        subject_id: str,
        permission: str,
    ) -> None:
        """授予 Dataset 权限（subject_type=user|role）"""
        if not self._pool:
            return
        async with self._pool.acquire() as conn:
            await conn.execute(
                """
                INSERT INTO dataset_permissions (
                    dataset_id, subject_type, subject_id, permission
                ) VALUES ($1, $2, $3, $4)
                ON CONFLICT (dataset_id, subject_type, subject_id) DO UPDATE SET
                    permission = EXCLUDED.permission,
                    updated_at = NOW()
                """,
                dataset_id,
                subject_type,
                subject_id,
                permission,
            )

    async def revoke_dataset_permission(
        self, dataset_id: str, subject_type: str, subject_id: str
    ) -> bool:
        """撤销 Dataset 权限"""
        if not self._pool:
            return False
        async with self._pool.acquire() as conn:
            result = await conn.execute(
                """
                DELETE FROM dataset_permissions
                WHERE dataset_id = $1 AND subject_type = $2 AND subject_id = $3
                """,
                dataset_id,
                subject_type,
                subject_id,
            )
            if result.startswith("DELETE "):
                return int(result.split()[-1]) > 0
            return False

    async def list_dataset_permissions(self, dataset_id: str) -> List[Dict[str, Any]]:
        """列出 Dataset 权限"""
        if not self._pool:
            return []
        async with self._pool.acquire() as conn:
            rows = await conn.fetch(
                """
                SELECT * FROM dataset_permissions
                WHERE dataset_id = $1
                ORDER BY created_at ASC
                """,
                dataset_id,
            )
            return [self._row_to_dict(row) for row in rows]

    async def get_dataset_permission(
        self, dataset_id: str, subject_type: str, subject_id: str
    ) -> Optional[Dict[str, Any]]:
        """获取指定 subject 对 Dataset 的权限记录"""
        if not self._pool:
            return None
        async with self._pool.acquire() as conn:
            row = await conn.fetchrow(
                """
                SELECT * FROM dataset_permissions
                WHERE dataset_id = $1 AND subject_type = $2 AND subject_id = $3
                """,
                dataset_id,
                subject_type,
                subject_id,
            )
            return self._row_to_dict(row) if row else None

    async def save_document(self, document: Dict[str, Any]) -> None:
        """保存或更新文档 Document"""
        if not self._pool:
            return
        async with self._pool.acquire() as conn:
            await conn.execute(
                """
                INSERT INTO documents (
                    document_id, dataset_id, title, source_type, source_uri,
                    mime_type, size_bytes, status, progress, error, content,
                    metadata, started_at, completed_at
                ) VALUES (
                    $1, $2, $3, $4, $5,
                    $6, $7, $8, $9, $10, $11,
                    $12, $13, $14
                )
                ON CONFLICT (document_id) DO UPDATE SET
                    dataset_id = EXCLUDED.dataset_id,
                    title = EXCLUDED.title,
                    source_type = EXCLUDED.source_type,
                    source_uri = EXCLUDED.source_uri,
                    mime_type = EXCLUDED.mime_type,
                    size_bytes = EXCLUDED.size_bytes,
                    status = EXCLUDED.status,
                    progress = EXCLUDED.progress,
                    error = EXCLUDED.error,
                    content = EXCLUDED.content,
                    metadata = EXCLUDED.metadata,
                    started_at = EXCLUDED.started_at,
                    completed_at = EXCLUDED.completed_at,
                    updated_at = NOW()
                """,
                document.get("document_id"),
                document.get("dataset_id"),
                document.get("title"),
                document.get("source_type", "upload"),
                document.get("source_uri"),
                document.get("mime_type"),
                document.get("size_bytes"),
                document.get("status", "uploaded"),
                float(document.get("progress", 0) or 0),
                document.get("error"),
                document.get("content"),
                json.dumps(document.get("metadata", {})),
                document.get("started_at"),
                document.get("completed_at"),
            )

    async def get_document(self, document_id: str) -> Optional[Dict[str, Any]]:
        """获取 Document"""
        if not self._pool:
            return None
        async with self._pool.acquire() as conn:
            row = await conn.fetchrow(
                "SELECT * FROM documents WHERE document_id = $1", document_id
            )
            return self._row_to_dict(row) if row else None

    async def list_documents(
        self,
        dataset_id: str,
        status: Optional[str] = None,
        limit: int = 100,
        offset: int = 0,
    ) -> List[Dict[str, Any]]:
        """列出文档"""
        if not self._pool:
            return []

        query = """
            SELECT document_id, dataset_id, title, source_type, source_uri, 
                   mime_type, size_bytes, status, progress, error, metadata, 
                   started_at, completed_at, created_at, updated_at 
            FROM documents WHERE dataset_id = $1
        """
        params: List[Any] = [dataset_id]
        param_idx = 2

        if status:
            query += f" AND status = ${param_idx}"
            params.append(status)
            param_idx += 1

        query += f" ORDER BY created_at DESC LIMIT ${param_idx} OFFSET ${param_idx + 1}"
        params.extend([limit, offset])

        async with self._pool.acquire() as conn:
            rows = await conn.fetch(query, *params)
            return [self._row_to_dict(row) for row in rows]

    async def update_document_status(
        self,
        document_id: str,
        status: str,
        progress: Optional[float] = None,
        error: Optional[str] = None,
    ) -> None:
        """更新 Document 状态"""
        if not self._pool:
            return

        updates = ["status = $1", "updated_at = NOW()"]
        params: List[Any] = [status]
        param_idx = 2

        if progress is not None:
            updates.append(f"progress = ${param_idx}")
            params.append(progress)
            param_idx += 1

        if error is not None:
            updates.append(f"error = ${param_idx}")
            params.append(error)
            param_idx += 1

        if status in ("parsing", "segmenting", "embedding"):
            updates.append(f"started_at = COALESCE(started_at, ${param_idx})")
            params.append(datetime.utcnow())
            param_idx += 1

        if status in ("completed", "failed"):
            updates.append(f"completed_at = ${param_idx}")
            params.append(datetime.utcnow())
            param_idx += 1

        params.append(document_id)
        query = f"UPDATE documents SET {', '.join(updates)} WHERE document_id = ${param_idx}"

        async with self._pool.acquire() as conn:
            await conn.execute(query, *params)

    async def delete_document(self, document_id: str) -> bool:
        """删除 Document（级联删除 Segment）"""
        if not self._pool:
            return False
        async with self._pool.acquire() as conn:
            result = await conn.execute(
                "DELETE FROM documents WHERE document_id = $1", document_id
            )
            if result.startswith("DELETE "):
                return int(result.split()[-1]) > 0
            return False

    async def update_document_fields(
        self, document_id: str, fields: Dict[str, Any]
    ) -> None:
        """Update arbitrary document fields (Dify-style enable/disable/archive support)"""
        if not self._pool or not fields:
            return

        # Allowed fields for update
        allowed = {
            "title", "metadata", "enabled", "disabled_at", "disabled_by",
            "archived", "archived_reason", "archived_by", "archived_at",
            "batch", "doc_type", "doc_form", "doc_language", "word_count",
            "segment_count", "tokens", "process_rule_id",
        }
        filtered = {k: v for k, v in fields.items() if k in allowed}
        if not filtered:
            return

        updates = ["updated_at = NOW()"]
        params: List[Any] = []
        param_idx = 1

        for key, value in filtered.items():
            if key == "metadata" and isinstance(value, dict):
                updates.append(f"{key} = ${param_idx}")
                params.append(json.dumps(value))
            else:
                updates.append(f"{key} = ${param_idx}")
                params.append(value)
            param_idx += 1

        params.append(document_id)
        query = f"UPDATE documents SET {', '.join(updates)} WHERE document_id = ${param_idx}"

        async with self._pool.acquire() as conn:
            await conn.execute(query, *params)

    async def insert_segments(self, segments: List[Dict[str, Any]]) -> None:
        """批量插入/更新 Segment (enhanced with Dify-style fields + content_hash)"""
        if not self._pool or not segments:
            return

        rows = []
        for seg in segments:
            rows.append(
                (
                    seg.get("segment_id"),
                    seg.get("dataset_id"),
                    seg.get("document_id"),
                    int(seg.get("position", 0) or 0),
                    seg.get("text", ""),
                    int(seg.get("token_count", 0) or 0),
                    seg.get("vector_id"),
                    json.dumps(seg.get("metadata", {})),
                    # New Dify-style fields
                    seg.get("enabled", True),
                    seg.get("status", "completed"),
                    int(seg.get("word_count", 0) or 0),
                    json.dumps(seg.get("keywords", [])),
                    seg.get("answer"),
                    seg.get("created_by"),
                    # Content hash for incremental updates
                    seg.get("content_hash"),
                )
            )

        async with self._pool.acquire() as conn:
            await conn.executemany(
                """
                INSERT INTO segments (
                    segment_id, dataset_id, document_id, position,
                    text, token_count, vector_id, metadata,
                    enabled, status, word_count, keywords, answer, created_by,
                    content_hash
                ) VALUES ($1, $2, $3, $4, $5, $6, $7, $8, $9, $10, $11, $12, $13, $14, $15)
                ON CONFLICT (document_id, position) DO UPDATE SET
                    segment_id = EXCLUDED.segment_id,
                    dataset_id = EXCLUDED.dataset_id,
                    text = EXCLUDED.text,
                    token_count = EXCLUDED.token_count,
                    vector_id = EXCLUDED.vector_id,
                    metadata = EXCLUDED.metadata,
                    enabled = EXCLUDED.enabled,
                    status = EXCLUDED.status,
                    word_count = EXCLUDED.word_count,
                    keywords = EXCLUDED.keywords,
                    answer = EXCLUDED.answer,
                    content_hash = EXCLUDED.content_hash,
                    updated_at = NOW()
                """,
                rows,
            )

    async def get_segment_hashes_by_document(
        self, document_id: str, content_type: str = "text"
    ) -> Dict[int, Dict[str, Any]]:
        """
        获取文档现有 segments 的 hash 映射，用于增量更新比对

        Args:
            document_id: 文档 ID
            content_type: 内容类型过滤 (text/image)

        Returns:
            position -> {segment_id, vector_id, content_hash} 的映射
        """
        if not self._pool:
            return {}
        async with self._pool.acquire() as conn:
            rows = await conn.fetch(
                """
                SELECT position, segment_id, vector_id, content_hash
                FROM segments
                WHERE document_id = $1 AND content_type = $2
                ORDER BY position
                """,
                document_id,
                content_type,
            )
            return {
                row["position"]: {
                    "segment_id": row["segment_id"],
                    "vector_id": row["vector_id"],
                    "content_hash": row["content_hash"],
                }
                for row in rows
            }

    async def delete_segments_by_document(
        self,
        document_id: str,
        exclude_ids: Optional[List[str]] = None,
        content_type: Optional[str] = None,
    ) -> int:
        """
        删除指定文档下的 Segment

        Args:
            document_id: 文档 ID
            exclude_ids: 要排除的 segment ID 列表（用于无感更新：先插入新的，再删除旧的）
            content_type: 可选的内容类型过滤（text/image）

        Returns:
            删除的记录数
        """
        if not self._pool:
            return 0
        async with self._pool.acquire() as conn:
            if exclude_ids and content_type:
                result = await conn.execute(
                    "DELETE FROM segments WHERE document_id = $1 AND segment_id != ALL($2::text[]) AND content_type = $3",
                    document_id,
                    exclude_ids,
                    content_type,
                )
            elif exclude_ids:
                result = await conn.execute(
                    "DELETE FROM segments WHERE document_id = $1 AND segment_id != ALL($2::text[])",
                    document_id,
                    exclude_ids,
                )
            elif content_type:
                result = await conn.execute(
                    "DELETE FROM segments WHERE document_id = $1 AND content_type = $2",
                    document_id,
                    content_type,
                )
            else:
                result = await conn.execute(
                    "DELETE FROM segments WHERE document_id = $1", document_id
                )
            if result.startswith("DELETE "):
                return int(result.split()[-1])
            return 0

    async def list_segments(
        self,
        dataset_id: str,
        document_id: Optional[str] = None,
        query_text: Optional[str] = None,
        limit: int = 100,
        offset: int = 0,
    ) -> List[Dict[str, Any]]:
        """列出 Segment"""
        if not self._pool:
            return []

        query = "SELECT * FROM segments WHERE dataset_id = $1"
        params: List[Any] = [dataset_id]
        param_idx = 2

        if document_id:
            query += f" AND document_id = ${param_idx}"
            params.append(document_id)
            param_idx += 1

        if query_text:
            query += f" AND text ILIKE ${param_idx}"
            params.append(f"%{query_text}%")
            param_idx += 1

        query += f" ORDER BY document_id ASC, position ASC LIMIT ${param_idx} OFFSET ${param_idx + 1}"
        params.extend([limit, offset])

        async with self._pool.acquire() as conn:
            rows = await conn.fetch(query, *params)
            return [self._row_to_dict(row) for row in rows]

    async def search_segments_like_any(
        self,
        dataset_id: str,
        terms: List[str],
        document_id: Optional[str] = None,
        limit: int = 200,
    ) -> List[Dict[str, Any]]:
        """Keyword candidate retrieval using ILIKE OR over terms (best-effort).

        This is intentionally lightweight and does not require extra PostgreSQL extensions.
        """
        if not self._pool:
            return []

        cleaned = [t.strip() for t in (terms or []) if str(t or "").strip()]
        if not cleaned:
            return []

        # Start with base query - only filter by enabled if the column exists
        query = "SELECT * FROM segments WHERE dataset_id = $1"
        params: List[Any] = [dataset_id]
        param_idx = 2

        if document_id:
            query += f" AND document_id = ${param_idx}"
            params.append(document_id)
            param_idx += 1

        # ILIKE any term (case-insensitive search)
        parts = []
        for t in cleaned:
            parts.append(f"text ILIKE ${param_idx}")
            params.append(f"%{t}%")
            param_idx += 1
        
        if parts:
            query += " AND (" + " OR ".join(parts) + ")"

        query += f" LIMIT ${param_idx}"
        params.append(int(limit))

        try:
            async with self._pool.acquire() as conn:
                rows = await conn.fetch(query, *params)
                return [self._row_to_dict(row) for row in rows]
        except Exception as e:
            import logging
            logging.getLogger(__name__).error(f"search_segments_like_any error: {e}, query: {query}, params: {params[:2]}...")
            return []

    async def get_segment(self, segment_id: str) -> Optional[Dict[str, Any]]:
        """获取 Segment"""
        if not self._pool:
            return None
        async with self._pool.acquire() as conn:
            row = await conn.fetchrow(
                "SELECT * FROM segments WHERE segment_id = $1", segment_id
            )
            return self._row_to_dict(row) if row else None

    async def update_segment(
        self,
        segment_id: str,
        text: str,
        token_count: Optional[int] = None,
        metadata: Optional[Dict[str, Any]] = None,
        vector_id: Optional[str] = None,
    ) -> None:
        """更新 Segment"""
        if not self._pool:
            return

        updates = ["text = $1", "updated_at = NOW()"]
        params: List[Any] = [text]
        param_idx = 2

        if token_count is not None:
            updates.append(f"token_count = ${param_idx}")
            params.append(token_count)
            param_idx += 1

        if vector_id is not None:
            updates.append(f"vector_id = ${param_idx}")
            params.append(vector_id)
            param_idx += 1

        if metadata is not None:
            updates.append(f"metadata = ${param_idx}")
            params.append(json.dumps(metadata))
            param_idx += 1

        params.append(segment_id)
        query = f"UPDATE segments SET {', '.join(updates)} WHERE segment_id = ${param_idx}"

        async with self._pool.acquire() as conn:
            await conn.execute(query, *params)

    async def update_segment_fields(
        self, segment_id: str, fields: Dict[str, Any]
    ) -> None:
        """Update arbitrary segment fields (Dify-style enable/disable support)"""
        if not self._pool or not fields:
            return

        # Allowed fields for update
        allowed = {
            "enabled", "disabled_at", "disabled_by", "status", "hit_count",
            "word_count", "keywords", "answer", "index_node_id", "index_node_hash",
            "vector_id", "error",
        }
        filtered = {k: v for k, v in fields.items() if k in allowed}
        if not filtered:
            return

        updates = ["updated_at = NOW()"]
        params: List[Any] = []
        param_idx = 1

        for key, value in filtered.items():
            if key == "keywords" and isinstance(value, list):
                updates.append(f"{key} = ${param_idx}")
                params.append(json.dumps(value))
            else:
                updates.append(f"{key} = ${param_idx}")
                params.append(value)
            param_idx += 1

        params.append(segment_id)
        query = f"UPDATE segments SET {', '.join(updates)} WHERE segment_id = ${param_idx}"

        async with self._pool.acquire() as conn:
            await conn.execute(query, *params)

    async def delete_segment(self, segment_id: str) -> bool:
        """删除 Segment"""
        if not self._pool:
            return False
        async with self._pool.acquire() as conn:
            result = await conn.execute(
                "DELETE FROM segments WHERE segment_id = $1", segment_id
            )
            if result.startswith("DELETE "):
                return int(result.split()[-1]) > 0
            return False

    async def save_image_segment(self, segment_data: Dict[str, Any]) -> None:
        """保存图片段到数据库

        Args:
            segment_data: 包含以下字段的字典:
                - segment_id: 段ID (必需)
                - document_id: 文档ID (必需)
                - dataset_id: 数据集ID (可选，如未提供会从文档中查询)
                - content_type: 内容类型，默认 'image'
                - image_url: 图片存储URL
                - image_attachment_id: Confluence附件ID
                - image_filename: 文件名
                - image_media_type: MIME类型
                - image_file_size: 文件大小
                - vector_id: 向量ID (可选)
                - text: 上下文文本 (可选)
        """
        if not self._pool:
            return

        # 获取 dataset_id，如果未提供则从文档中查询
        dataset_id = segment_data.get("dataset_id")
        if not dataset_id:
            document = await self.get_document(segment_data.get("document_id"))
            if document:
                dataset_id = document.get("dataset_id")

        if not dataset_id:
            raise ValueError("dataset_id is required but could not be determined")

        async with self._pool.acquire() as conn:
            await conn.execute(
                """
                INSERT INTO segments (
                    segment_id, dataset_id, document_id, position,
                    text, token_count, vector_id, metadata,
                    content_type, image_url, image_attachment_id,
                    image_filename, image_media_type, image_file_size,
                    enabled, status
                ) VALUES (
                    $1, $2, $3, $4, $5, $6, $7, $8,
                    $9, $10, $11, $12, $13, $14, $15, $16
                )
                ON CONFLICT (segment_id) DO UPDATE SET
                    dataset_id = EXCLUDED.dataset_id,
                    document_id = EXCLUDED.document_id,
                    vector_id = EXCLUDED.vector_id,
                    content_type = EXCLUDED.content_type,
                    image_url = EXCLUDED.image_url,
                    image_attachment_id = EXCLUDED.image_attachment_id,
                    image_filename = EXCLUDED.image_filename,
                    image_media_type = EXCLUDED.image_media_type,
                    image_file_size = EXCLUDED.image_file_size,
                    updated_at = NOW()
                """,
                segment_data.get("segment_id"),
                dataset_id,
                segment_data.get("document_id"),
                segment_data.get("position", 0),
                segment_data.get("text", ""),  # context text
                segment_data.get("token_count", 0),
                segment_data.get("vector_id"),
                json.dumps(segment_data.get("metadata", {})),
                segment_data.get("content_type", "image"),
                segment_data.get("image_url"),
                segment_data.get("image_attachment_id"),
                segment_data.get("image_filename"),
                segment_data.get("image_media_type"),
                segment_data.get("image_file_size"),
                True,  # enabled
                "completed",  # status
            )

    async def get_image_segments_by_document(
        self, document_id: str
    ) -> List[Dict[str, Any]]:
        """获取文档的所有图片段

        Args:
            document_id: 文档ID

        Returns:
            图片段列表，每个包含 segment_id, image_attachment_id, metadata 等
        """
        if not self._pool:
            return []
        async with self._pool.acquire() as conn:
            rows = await conn.fetch(
                """
                SELECT segment_id, document_id, dataset_id, position,
                       text, vector_id, metadata, content_type,
                       image_url, image_attachment_id, image_filename,
                       image_media_type, image_file_size,
                       created_at, updated_at
                FROM segments
                WHERE document_id = $1 AND content_type = 'image'
                ORDER BY position
                """,
                document_id,
            )
            return [dict(row) for row in rows]

    async def delete_image_segments_by_document(self, document_id: str) -> int:
        """删除文档的所有图片段

        Args:
            document_id: 文档ID

        Returns:
            删除的段数量
        """
        if not self._pool:
            return 0
        async with self._pool.acquire() as conn:
            result = await conn.execute(
                """
                DELETE FROM segments
                WHERE document_id = $1 AND content_type = 'image'
                """,
                document_id,
            )
            if result.startswith("DELETE "):
                return int(result.split()[-1])
            return 0

    async def count_segments_by_document(self, document_id: str) -> int:
        """Count total segments for a document.

        Args:
            document_id: The document ID

        Returns:
            Total segment count for the document
        """
        if not self._pool:
            return 0
        async with self._pool.acquire() as conn:
            row = await conn.fetchrow(
                "SELECT COUNT(*) as cnt FROM segments WHERE document_id = $1",
                document_id,
            )
            return int(row["cnt"]) if row else 0

    async def refresh_document_segment_count(self, document_id: str) -> int:
        """Recount segments and update the document's segment_count field.

        This method counts all segments (text + image) for a document and updates
        the documents.segment_count field atomically.

        Args:
            document_id: The document ID

        Returns:
            The new segment count
        """
        if not self._pool:
            return 0
        async with self._pool.acquire() as conn:
            # Use a single atomic UPDATE with subquery for consistency
            row = await conn.fetchrow(
                """
                UPDATE documents
                SET segment_count = (
                    SELECT COUNT(*) FROM segments WHERE document_id = $1
                ),
                updated_at = NOW()
                WHERE document_id = $1
                RETURNING segment_count
                """,
                document_id,
            )
            return int(row["segment_count"]) if row else 0

    # =========================================================================
    # Segment Image Associations (segment_images) - P3 Multimodal RAG
    # =========================================================================

    async def add_segment_image_association(
        self,
        segment_id: str,
        image_segment_id: str,
        position: int = 0,
        proximity_score: float = 1.0,
        char_offset: int = 0,
        page_number: Optional[int] = None,
    ) -> bool:
        """Associate an image segment with a text segment.

        Args:
            segment_id: The text segment ID
            image_segment_id: The image segment ID to associate
            position: Position of image in the chunk context (0-indexed)
            proximity_score: Relevance score [0,1] - 1.0 = inline, 0.5 = same page
            char_offset: Character offset in source document
            page_number: Page number in multi-page documents

        Returns:
            True if association was created/updated
        """
        if not self._pool:
            return False

        async with self._pool.acquire() as conn:
            try:
                await conn.execute(
                    """
                    INSERT INTO segment_images (
                        segment_id, image_segment_id, position,
                        proximity_score, char_offset, page_number
                    ) VALUES ($1, $2, $3, $4, $5, $6)
                    ON CONFLICT (segment_id, image_segment_id) DO UPDATE SET
                        position = EXCLUDED.position,
                        proximity_score = EXCLUDED.proximity_score,
                        char_offset = EXCLUDED.char_offset,
                        page_number = EXCLUDED.page_number
                    """,
                    segment_id,
                    image_segment_id,
                    position,
                    proximity_score,
                    char_offset,
                    page_number,
                )
                return True
            except Exception:
                return False

    async def add_segment_image_associations_batch(
        self,
        associations: List[Dict[str, Any]],
    ) -> int:
        """Add multiple image associations in batch.

        Args:
            associations: List of dicts with keys:
                - segment_id: Text segment ID
                - image_segment_id: Image segment ID
                - position: Position (optional, default 0)
                - proximity_score: Score (optional, default 1.0)
                - char_offset: Offset (optional, default 0)
                - page_number: Page (optional)

        Returns:
            Number of associations created/updated
        """
        if not self._pool or not associations:
            return 0

        async with self._pool.acquire() as conn:
            count = 0
            # Use a transaction for batch insert
            async with conn.transaction():
                for assoc in associations:
                    try:
                        await conn.execute(
                            """
                            INSERT INTO segment_images (
                                segment_id, image_segment_id, position,
                                proximity_score, char_offset, page_number
                            ) VALUES ($1, $2, $3, $4, $5, $6)
                            ON CONFLICT (segment_id, image_segment_id) DO UPDATE SET
                                position = EXCLUDED.position,
                                proximity_score = EXCLUDED.proximity_score,
                                char_offset = EXCLUDED.char_offset,
                                page_number = EXCLUDED.page_number
                            """,
                            assoc.get("segment_id"),
                            assoc.get("image_segment_id"),
                            assoc.get("position", 0),
                            assoc.get("proximity_score", 1.0),
                            assoc.get("char_offset", 0),
                            assoc.get("page_number"),
                        )
                        count += 1
                    except Exception:
                        continue
            return count

    async def get_segment_associated_images(
        self, segment_id: str
    ) -> List[Dict[str, Any]]:
        """Get all images associated with a text segment.

        Args:
            segment_id: The text segment ID

        Returns:
            List of associated image info including image segment details
        """
        if not self._pool:
            return []

        async with self._pool.acquire() as conn:
            rows = await conn.fetch(
                """
                SELECT
                    si.image_segment_id,
                    si.position,
                    si.proximity_score,
                    si.char_offset,
                    si.page_number,
                    s.image_url AS storage_url,
                    s.image_filename AS filename,
                    s.image_media_type AS media_type,
                    s.text AS vlm_description
                FROM segment_images si
                JOIN segments s ON s.segment_id = si.image_segment_id
                WHERE si.segment_id = $1
                ORDER BY si.proximity_score DESC, si.position
                """,
                segment_id,
            )
            return [dict(row) for row in rows]

    async def get_segment_associations_batch(
        self, segment_ids: List[str]
    ) -> Dict[str, List[Dict[str, Any]]]:
        """Get associated images for multiple segments efficiently.

        Args:
            segment_ids: List of text segment IDs

        Returns:
            Dict mapping segment_id -> list of associated image info
        """
        if not self._pool or not segment_ids:
            return {}

        async with self._pool.acquire() as conn:
            rows = await conn.fetch(
                """
                SELECT
                    si.segment_id,
                    si.image_segment_id,
                    si.position,
                    si.proximity_score,
                    si.char_offset,
                    si.page_number,
                    s.image_url AS storage_url,
                    s.image_filename AS filename,
                    s.image_media_type AS media_type,
                    s.text AS vlm_description
                FROM segment_images si
                JOIN segments s ON s.segment_id = si.image_segment_id
                WHERE si.segment_id = ANY($1)
                ORDER BY si.segment_id, si.proximity_score DESC, si.position
                """,
                segment_ids,
            )

            result: Dict[str, List[Dict[str, Any]]] = {sid: [] for sid in segment_ids}
            for row in rows:
                seg_id = row["segment_id"]
                if seg_id in result:
                    result[seg_id].append(dict(row))
            return result

    async def delete_segment_image_associations(
        self, segment_id: str
    ) -> int:
        """Delete all image associations for a text segment.

        Args:
            segment_id: The text segment ID

        Returns:
            Number of associations deleted
        """
        if not self._pool:
            return 0

        async with self._pool.acquire() as conn:
            result = await conn.execute(
                "DELETE FROM segment_images WHERE segment_id = $1",
                segment_id,
            )
            if result.startswith("DELETE "):
                return int(result.split()[-1])
            return 0

    async def delete_image_associations_by_document(
        self, document_id: str
    ) -> int:
        """Delete all image associations for segments in a document.

        Args:
            document_id: The document ID

        Returns:
            Number of associations deleted
        """
        if not self._pool:
            return 0

        async with self._pool.acquire() as conn:
            result = await conn.execute(
                """
                DELETE FROM segment_images
                WHERE segment_id IN (
                    SELECT segment_id FROM segments WHERE document_id = $1
                )
                """,
                document_id,
            )
            if result.startswith("DELETE "):
                return int(result.split()[-1])
            return 0

    async def update_segment_image_flags(
        self, segment_id: str
    ) -> None:
        """Update has_images and image_count flags for a segment.

        This should be called after adding/removing image associations.

        Args:
            segment_id: The text segment ID
        """
        if not self._pool:
            return

        async with self._pool.acquire() as conn:
            await conn.execute(
                """
                UPDATE segments
                SET
                    has_images = (SELECT COUNT(*) > 0 FROM segment_images WHERE segment_id = $1),
                    image_count = (SELECT COUNT(*) FROM segment_images WHERE segment_id = $1)
                WHERE segment_id = $1
                """,
                segment_id,
            )

    # =========================================================================
    # API Key 表 (api_keys)
    # =========================================================================

    async def save_api_key(
        self,
        key_hash: str,
        name: str = None,
        description: str = None,
        tenant_id: str = None,
        user_id: str = None,
        roles: List[str] = None,
        permissions: List[str] = None,
        tier: str = "normal",
        rate_limit: Dict = None,
        allowed_services: List[str] = None,
        expires_at: datetime = None
    ) -> int:
        """保存 API Key"""
        if not self._pool:
            return 0
        async with self._pool.acquire() as conn:
            row = await conn.fetchrow("""
                INSERT INTO api_keys (
                    key_hash, name, description, tenant_id, user_id,
                    roles, permissions, tier, rate_limit, allowed_services, expires_at
                ) VALUES ($1, $2, $3, $4, $5, $6, $7, $8, $9, $10, $11)
                ON CONFLICT (key_hash) DO UPDATE SET
                    name = EXCLUDED.name,
                    description = EXCLUDED.description,
                    tenant_id = EXCLUDED.tenant_id,
                    user_id = EXCLUDED.user_id,
                    roles = EXCLUDED.roles,
                    permissions = EXCLUDED.permissions,
                    tier = EXCLUDED.tier,
                    rate_limit = EXCLUDED.rate_limit,
                    allowed_services = EXCLUDED.allowed_services,
                    expires_at = EXCLUDED.expires_at,
                    updated_at = NOW()
                RETURNING id
            """,
                key_hash,
                name,
                description,
                tenant_id,
                user_id,
                roles or ["user"],
                permissions or [],
                tier,
                json.dumps(rate_limit) if rate_limit else None,
                allowed_services or [],
                expires_at,
            )
            return row["id"] if row else 0

    async def get_api_key(self, key_hash: str) -> Optional[Dict[str, Any]]:
        """通过哈希获取 API Key"""
        if not self._pool:
            return None
        async with self._pool.acquire() as conn:
            row = await conn.fetchrow("""
                SELECT * FROM api_keys 
                WHERE key_hash = $1 AND enabled = TRUE
                AND (expires_at IS NULL OR expires_at > NOW())
            """, key_hash)
            if row:
                # 更新使用统计
                await conn.execute("""
                    UPDATE api_keys SET 
                        last_used_at = NOW(), 
                        use_count = use_count + 1 
                    WHERE key_hash = $1
                """, key_hash)
            return self._row_to_dict(row) if row else None

    async def list_api_keys(
        self,
        tenant_id: Optional[str] = None,
        user_id: Optional[str] = None,
        enabled: bool = None
    ) -> List[Dict[str, Any]]:
        """获取 API Key 列表（不返回哈希）"""
        if not self._pool:
            return []
        
        query = """
            SELECT id, name, description, tenant_id, user_id, roles, 
                   permissions, tier, rate_limit, allowed_services,
                   expires_at, enabled, last_used_at, use_count, created_at, updated_at
            FROM api_keys WHERE 1=1
        """
        params = []
        param_idx = 1
        
        if tenant_id:
            query += f" AND tenant_id = ${param_idx}"
            params.append(tenant_id)
            param_idx += 1
        
        if user_id:
            query += f" AND user_id = ${param_idx}"
            params.append(user_id)
            param_idx += 1
        
        if enabled is not None:
            query += f" AND enabled = ${param_idx}"
            params.append(enabled)
            param_idx += 1
        
        query += " ORDER BY created_at DESC"
        
        async with self._pool.acquire() as conn:
            rows = await conn.fetch(query, *params)
            return [self._row_to_dict(row) for row in rows]

    async def disable_api_key(self, key_id: int) -> bool:
        """禁用 API Key"""
        if not self._pool:
            return False
        async with self._pool.acquire() as conn:
            result = await conn.execute(
                "UPDATE api_keys SET enabled = FALSE, updated_at = NOW() WHERE id = $1",
                key_id
            )
            return result == "UPDATE 1"

    async def delete_api_key(self, key_id: int) -> bool:
        """删除 API Key"""
        if not self._pool:
            return False
        async with self._pool.acquire() as conn:
            result = await conn.execute(
                "DELETE FROM api_keys WHERE id = $1", key_id
            )
            return result == "DELETE 1"

    # =========================================================================
    # 用户表 (users)
    # =========================================================================

    async def save_user(self, user: Dict[str, Any]) -> None:
        """保存或更新用户"""
        if not self._pool:
            return
        async with self._pool.acquire() as conn:
            await conn.execute("""
                INSERT INTO users (
                    user_id, username, email, display_name, tenant_id,
                    tier, roles, permissions, quota_config, status, metadata
                ) VALUES ($1, $2, $3, $4, $5, $6, $7, $8, $9, $10, $11)
                ON CONFLICT (user_id) DO UPDATE SET
                    username = EXCLUDED.username,
                    email = EXCLUDED.email,
                    display_name = EXCLUDED.display_name,
                    tier = EXCLUDED.tier,
                    roles = EXCLUDED.roles,
                    permissions = EXCLUDED.permissions,
                    quota_config = EXCLUDED.quota_config,
                    status = EXCLUDED.status,
                    metadata = EXCLUDED.metadata,
                    updated_at = NOW()
            """,
                user.get("user_id"),
                user.get("username"),
                user.get("email"),
                user.get("display_name"),
                user.get("tenant_id", "default"),
                user.get("tier", "normal"),
                user.get("roles", ["user"]),
                user.get("permissions", []),
                json.dumps(user.get("quota_config", {})),
                user.get("status", "active"),
                json.dumps(user.get("metadata", {})),
            )

    async def get_user(self, user_id: str) -> Optional[Dict[str, Any]]:
        """获取用户"""
        if not self._pool:
            return None
        async with self._pool.acquire() as conn:
            row = await conn.fetchrow(
                "SELECT * FROM users WHERE user_id = $1", user_id
            )
            return self._row_to_dict(row) if row else None

    async def list_users(
        self,
        tenant_id: Optional[str] = None,
        status: str = "active",
        limit: int = 100
    ) -> List[Dict[str, Any]]:
        """获取用户列表"""
        if not self._pool:
            return []
        
        query = "SELECT * FROM users WHERE 1=1"
        params = []
        param_idx = 1
        
        if tenant_id:
            query += f" AND tenant_id = ${param_idx}"
            params.append(tenant_id)
            param_idx += 1
        
        if status:
            query += f" AND status = ${param_idx}"
            params.append(status)
            param_idx += 1
        
        query += f" ORDER BY created_at DESC LIMIT ${param_idx}"
        params.append(limit)
        
        async with self._pool.acquire() as conn:
            rows = await conn.fetch(query, *params)
            return [self._row_to_dict(row) for row in rows]

    async def update_user_last_active(self, user_id: str) -> None:
        """更新用户最后活跃时间"""
        if not self._pool:
            return
        async with self._pool.acquire() as conn:
            await conn.execute(
                "UPDATE users SET last_active_at = NOW() WHERE user_id = $1",
                user_id
            )

    # =========================================================================
    # 租户表 (tenants)
    # =========================================================================

    async def save_tenant(self, tenant: Dict[str, Any]) -> None:
        """保存或更新租户"""
        if not self._pool:
            return
        async with self._pool.acquire() as conn:
            await conn.execute("""
                INSERT INTO tenants (
                    tenant_id, name, description, tier, quota_config,
                    rate_limit, allowed_services, status, metadata
                ) VALUES ($1, $2, $3, $4, $5, $6, $7, $8, $9)
                ON CONFLICT (tenant_id) DO UPDATE SET
                    name = EXCLUDED.name,
                    description = EXCLUDED.description,
                    tier = EXCLUDED.tier,
                    quota_config = EXCLUDED.quota_config,
                    rate_limit = EXCLUDED.rate_limit,
                    allowed_services = EXCLUDED.allowed_services,
                    status = EXCLUDED.status,
                    metadata = EXCLUDED.metadata,
                    updated_at = NOW()
            """,
                tenant.get("tenant_id"),
                tenant.get("name"),
                tenant.get("description"),
                tenant.get("tier", "normal"),
                json.dumps(tenant.get("quota_config", {})),
                json.dumps(tenant.get("rate_limit")) if tenant.get("rate_limit") else None,
                tenant.get("allowed_services", []),
                tenant.get("status", "active"),
                json.dumps(tenant.get("metadata", {})),
            )

    async def get_tenant(self, tenant_id: str) -> Optional[Dict[str, Any]]:
        """获取租户"""
        if not self._pool:
            return None
        async with self._pool.acquire() as conn:
            row = await conn.fetchrow(
                "SELECT * FROM tenants WHERE tenant_id = $1", tenant_id
            )
            return self._row_to_dict(row) if row else None

    async def list_tenants(self, status: str = "active") -> List[Dict[str, Any]]:
        """获取租户列表"""
        if not self._pool:
            return []
        async with self._pool.acquire() as conn:
            rows = await conn.fetch(
                "SELECT * FROM tenants WHERE status = $1 ORDER BY created_at DESC",
                status
            )
            return [self._row_to_dict(row) for row in rows]

    # =========================================================================
    # 限流配置表 (rate_limit_config)
    # =========================================================================

    async def save_rate_limit(
        self,
        scope: str,
        scope_id: str,
        requests: int,
        window_seconds: int,
        burst: int = 0,
        strategy: str = "sliding_window",
        priority: int = 0
    ) -> None:
        """保存限流配置"""
        if not self._pool:
            return
        async with self._pool.acquire() as conn:
            await conn.execute("""
                INSERT INTO rate_limit_config (
                    scope, scope_id, requests, window_seconds, burst, strategy, priority
                ) VALUES ($1, $2, $3, $4, $5, $6, $7)
                ON CONFLICT (scope, scope_id) DO UPDATE SET
                    requests = EXCLUDED.requests,
                    window_seconds = EXCLUDED.window_seconds,
                    burst = EXCLUDED.burst,
                    strategy = EXCLUDED.strategy,
                    priority = EXCLUDED.priority,
                    updated_at = NOW()
            """, scope, scope_id or "", requests, window_seconds, burst, strategy, priority)

    async def get_rate_limits(
        self,
        scope: Optional[str] = None,
        enabled: bool = True
    ) -> List[Dict[str, Any]]:
        """获取限流配置"""
        if not self._pool:
            return []
        
        query = """
            SELECT id, scope, scope_id, requests, window_seconds, 
                   burst, strategy, enabled, priority, created_at, updated_at
            FROM rate_limit_config WHERE 1=1
        """
        params = []
        param_idx = 1
        
        if scope:
            query += f" AND scope = ${param_idx}"
            params.append(scope)
            param_idx += 1
        
        if enabled is not None:
            query += f" AND enabled = ${param_idx}"
            params.append(enabled)
            param_idx += 1
        
        query += " ORDER BY priority DESC, scope"
        
        async with self._pool.acquire() as conn:
            rows = await conn.fetch(query, *params)
            return [self._row_to_dict(row) for row in rows]

    async def delete_rate_limit(self, scope: str, scope_id: str) -> bool:
        """删除限流配置"""
        if not self._pool:
            return False
        async with self._pool.acquire() as conn:
            result = await conn.execute(
                "DELETE FROM rate_limit_config WHERE scope = $1 AND scope_id = $2",
                scope, scope_id or ""
            )
            return result == "DELETE 1"

    # =========================================================================
    # RBAC 角色表 (rbac_roles)
    # =========================================================================

    async def get_rbac_roles(self) -> List[Dict[str, Any]]:
        """获取所有角色"""
        if not self._pool:
            return []
        async with self._pool.acquire() as conn:
            rows = await conn.fetch(
                "SELECT * FROM rbac_roles ORDER BY is_system DESC, role_name"
            )
            return [self._row_to_dict(row) for row in rows]

    async def get_role_permissions(self, role_name: str) -> List[str]:
        """获取角色权限"""
        if not self._pool:
            return []
        async with self._pool.acquire() as conn:
            row = await conn.fetchrow(
                "SELECT permissions FROM rbac_roles WHERE role_name = $1",
                role_name
            )
            return row["permissions"] if row else []

    async def save_role(
        self,
        role_name: str,
        permissions: List[str],
        description: str = None,
        is_system: bool = False
    ) -> None:
        """保存角色"""
        if not self._pool:
            return
        async with self._pool.acquire() as conn:
            await conn.execute("""
                INSERT INTO rbac_roles (role_name, permissions, description, is_system)
                VALUES ($1, $2, $3, $4)
                ON CONFLICT (role_name) DO UPDATE SET
                    permissions = EXCLUDED.permissions,
                    description = EXCLUDED.description,
                    updated_at = NOW()
            """, role_name, permissions, description, is_system)

    # =========================================================================
    # 审计日志表 (audit_logs)
    # =========================================================================

    async def log_audit(
        self,
        event_type: str,
        action: str,
        status: str,
        user_id: str = None,
        tenant_id: str = None,
        ip_address: str = None,
        user_agent: str = None,
        resource_type: str = None,
        resource_id: str = None,
        request_summary: Dict = None,
        response_summary: Dict = None,
        error_message: str = None,
        duration_ms: int = None
    ) -> None:
        """记录审计日志"""
        if not self._pool:
            return
        async with self._pool.acquire() as conn:
            await conn.execute("""
                INSERT INTO audit_logs (
                    event_type, user_id, tenant_id, ip_address, user_agent,
                    resource_type, resource_id, action, request_summary,
                    response_summary, status, error_message, duration_ms
                ) VALUES ($1, $2, $3, $4, $5, $6, $7, $8, $9, $10, $11, $12, $13)
            """,
                event_type, user_id, tenant_id, ip_address, user_agent,
                resource_type, resource_id, action,
                json.dumps(request_summary) if request_summary else None,
                json.dumps(response_summary) if response_summary else None,
                status, error_message, duration_ms
            )

    async def query_audit_logs(
        self,
        event_type: Optional[str] = None,
        user_id: Optional[str] = None,
        tenant_id: Optional[str] = None,
        start_time: Optional[datetime] = None,
        end_time: Optional[datetime] = None,
        limit: int = 100,
        offset: int = 0
    ) -> List[Dict[str, Any]]:
        """查询审计日志"""
        if not self._pool:
            return []
        
        query = "SELECT * FROM audit_logs WHERE 1=1"
        params = []
        param_idx = 1
        
        if event_type:
            query += f" AND event_type = ${param_idx}"
            params.append(event_type)
            param_idx += 1
        
        if user_id:
            query += f" AND user_id = ${param_idx}"
            params.append(user_id)
            param_idx += 1
        
        if tenant_id:
            query += f" AND tenant_id = ${param_idx}"
            params.append(tenant_id)
            param_idx += 1
        
        if start_time:
            query += f" AND created_at >= ${param_idx}"
            params.append(start_time)
            param_idx += 1
        
        if end_time:
            query += f" AND created_at <= ${param_idx}"
            params.append(end_time)
            param_idx += 1
        
        query += f" ORDER BY created_at DESC LIMIT ${param_idx} OFFSET ${param_idx + 1}"
        params.extend([limit, offset])
        
        async with self._pool.acquire() as conn:
            rows = await conn.fetch(query, *params)
            return [self._row_to_dict(row) for row in rows]

    # =========================================================================
    # 服务健康记录表 (service_health_records)
    # =========================================================================

    async def record_health_check(
        self,
        service_id: str,
        status: str,
        response_time_ms: int = None,
        details: Dict = None,
        error_message: str = None
    ) -> None:
        """记录健康检查结果"""
        if not self._pool:
            return
        async with self._pool.acquire() as conn:
            await conn.execute("""
                INSERT INTO service_health_records (
                    service_id, status, response_time_ms, details, error_message
                ) VALUES ($1, $2, $3, $4, $5)
            """,
                service_id, status, response_time_ms,
                json.dumps(details or {}), error_message
            )

    async def get_health_history(
        self,
        service_id: str,
        limit: int = 100
    ) -> List[Dict[str, Any]]:
        """获取服务健康历史"""
        if not self._pool:
            return []
        async with self._pool.acquire() as conn:
            rows = await conn.fetch("""
                SELECT * FROM service_health_records 
                WHERE service_id = $1 
                ORDER BY checked_at DESC 
                LIMIT $2
            """, service_id, limit)
            return [self._row_to_dict(row) for row in rows]

    # =========================================================================
    # 使用统计表 (usage_statistics)
    # =========================================================================

    async def update_usage_stats(
        self,
        dimension: str,
        dimension_id: str,
        period_type: str,
        period_start: datetime,
        request_count: int = 1,
        success_count: int = 0,
        error_count: int = 0,
        input_tokens: int = 0,
        output_tokens: int = 0,
        response_time_ms: int = None
    ) -> None:
        """更新使用统计"""
        if not self._pool:
            return
        async with self._pool.acquire() as conn:
            await conn.execute("""
                INSERT INTO usage_statistics (
                    dimension, dimension_id, period_type, period_start,
                    request_count, success_count, error_count,
                    input_tokens, output_tokens,
                    avg_response_time_ms, max_response_time_ms, min_response_time_ms
                ) VALUES ($1, $2, $3, $4, $5, $6, $7, $8, $9, $10, $10, $10)
                ON CONFLICT (dimension, dimension_id, period_type, period_start) DO UPDATE SET
                    request_count = usage_statistics.request_count + EXCLUDED.request_count,
                    success_count = usage_statistics.success_count + EXCLUDED.success_count,
                    error_count = usage_statistics.error_count + EXCLUDED.error_count,
                    input_tokens = usage_statistics.input_tokens + EXCLUDED.input_tokens,
                    output_tokens = usage_statistics.output_tokens + EXCLUDED.output_tokens,
                    avg_response_time_ms = CASE 
                        WHEN $10 IS NOT NULL THEN 
                            (COALESCE(usage_statistics.avg_response_time_ms, 0) * usage_statistics.request_count + $10) 
                            / (usage_statistics.request_count + 1)
                        ELSE usage_statistics.avg_response_time_ms
                    END,
                    max_response_time_ms = GREATEST(COALESCE(usage_statistics.max_response_time_ms, 0), COALESCE($10, 0)),
                    min_response_time_ms = LEAST(COALESCE(usage_statistics.min_response_time_ms, 999999999), COALESCE($10, 999999999)),
                    updated_at = NOW()
            """,
                dimension, dimension_id, period_type, period_start,
                request_count, success_count, error_count,
                input_tokens, output_tokens, response_time_ms
            )

    async def get_usage_stats(
        self,
        dimension: str,
        dimension_id: str,
        period_type: str,
        start_time: datetime,
        end_time: datetime
    ) -> List[Dict[str, Any]]:
        """获取使用统计"""
        if not self._pool:
            return []
        async with self._pool.acquire() as conn:
            rows = await conn.fetch("""
                SELECT * FROM usage_statistics 
                WHERE dimension = $1 AND dimension_id = $2 AND period_type = $3
                AND period_start >= $4 AND period_start <= $5
                ORDER BY period_start
            """, dimension, dimension_id, period_type, start_time, end_time)
            return [self._row_to_dict(row) for row in rows]

    # =========================================================================
    # Security event daily aggregates (auth failures, rate limits)
    # =========================================================================

    async def record_security_event(
        self,
        tenant_id: str,
        user_id: Optional[str],
        service_id: Optional[str],
        event_type: str,
        event_date: Optional[date] = None,
    ) -> None:
        """Record a security event into daily aggregates."""
        if not self._pool:
            return
        if event_date is None:
            event_date = datetime.utcnow().date()

        async with self._pool.acquire() as conn:
            await conn.execute(
                """
                INSERT INTO security_event_daily_aggregates (
                    tenant_id, user_id, service_id, event_type, date, event_count
                ) VALUES (
                    $1, $2, $3, $4, $5, 1
                )
                ON CONFLICT (tenant_id, COALESCE(user_id, ''), COALESCE(service_id, ''), event_type, date)
                DO UPDATE SET
                    event_count = security_event_daily_aggregates.event_count + 1,
                    updated_at = CURRENT_TIMESTAMP
                """,
                tenant_id,
                user_id or None,
                service_id or None,
                event_type,
                event_date,
            )

    async def get_security_event_breakdown(
        self,
        tenant_id: str,
        dimension: str,
        event_type: str,
        start_date: date,
        end_date: date,
        limit: int = 20,
    ) -> List[Dict[str, Any]]:
        """Get security event breakdown by dimension."""
        if not self._pool:
            return []

        dimension_column = {
            "user": "user_id",
            "service": "service_id",
        }.get(dimension, "user_id")

        async with self._pool.acquire() as conn:
            rows = await conn.fetch(
                f"""
                SELECT
                    {dimension_column} as dimension_value,
                    SUM(event_count) as total_events
                FROM security_event_daily_aggregates
                WHERE tenant_id = $1
                  AND date >= $2
                  AND date <= $3
                  AND event_type = $4
                  AND {dimension_column} IS NOT NULL
                GROUP BY {dimension_column}
                ORDER BY total_events DESC
                LIMIT $5
                """,
                tenant_id,
                start_date,
                end_date,
                event_type,
                limit,
            )
            return [self._row_to_dict(row) for row in rows]

    async def get_security_event_timeseries(
        self,
        tenant_id: str,
        event_type: str,
        start_date: date,
        end_date: date,
        user_id: Optional[str] = None,
        service_id: Optional[str] = None,
    ) -> List[Dict[str, Any]]:
        """Get daily time series for security events."""
        if not self._pool:
            return []

        async with self._pool.acquire() as conn:
            query = """
                SELECT
                    date,
                    SUM(event_count) as total_events
                FROM security_event_daily_aggregates
                WHERE tenant_id = $1
                  AND date >= $2
                  AND date <= $3
                  AND event_type = $4
            """
            params: List[Any] = [tenant_id, start_date, end_date, event_type]

            if user_id:
                query += " AND user_id = $" + str(len(params) + 1)
                params.append(user_id)
            if service_id:
                query += " AND service_id = $" + str(len(params) + 1)
                params.append(service_id)

            query += " GROUP BY date ORDER BY date"

            rows = await conn.fetch(query, *params)
            return [self._row_to_dict(row) for row in rows]

    async def get_security_event_last_ingested_at(
        self,
        tenant_id: str,
        event_type: str,
        start_date: date,
        end_date: date,
    ) -> Optional[datetime]:
        """Get last ingestion time for security event aggregates."""
        if not self._pool:
            return None
        async with self._pool.acquire() as conn:
            row = await conn.fetchrow(
                """
                SELECT MAX(updated_at) AS last_ingested
                FROM security_event_daily_aggregates
                WHERE tenant_id = $1
                  AND event_type = $2
                  AND date >= $3
                  AND date <= $4
                """,
                tenant_id,
                event_type,
                start_date,
                end_date,
            )
            return row["last_ingested"] if row else None

    async def get_usage_last_ingested_at(
        self,
        tenant_id: str,
        start_date: Optional[date] = None,
        end_date: Optional[date] = None,
        granularity: str = "day",
    ) -> Optional[datetime]:
        """Get last ingestion time for usage aggregates."""
        if not self._pool:
            return None

        table = "usage_hourly_aggregates" if granularity == "hour" else "usage_daily_aggregates"
        query = f"""
            SELECT MAX(updated_at) AS last_ingested
            FROM {table}
            WHERE tenant_id = $1
        """
        params: List[Any] = [tenant_id]

        if start_date:
            query += f" AND date >= ${len(params) + 1}"
            params.append(start_date)
        if end_date:
            query += f" AND date <= ${len(params) + 1}"
            params.append(end_date)

        async with self._pool.acquire() as conn:
            row = await conn.fetchrow(query, *params)
            return row["last_ingested"] if row else None

    # =========================================================================
    # LangGraph Thread 映射表 (langgraph_threads)
    # =========================================================================

    async def save_langgraph_thread(
        self,
        thread_id: str,
        user_id: str,
        tenant_id: str = "",
        assistant_id: str = None,
        metadata: Dict = None,
        is_anonymous: bool = False,
        expires_at: datetime = None
    ) -> None:
        """保存 LangGraph Thread 映射"""
        if not self._pool:
            return
        async with self._pool.acquire() as conn:
            await conn.execute("""
                INSERT INTO langgraph_threads (
                    thread_id, user_id, tenant_id, assistant_id, 
                    metadata, is_anonymous, expires_at
                ) VALUES ($1, $2, $3, $4, $5, $6, $7)
                ON CONFLICT (thread_id) DO UPDATE SET
                    assistant_id = EXCLUDED.assistant_id,
                    metadata = EXCLUDED.metadata,
                    expires_at = EXCLUDED.expires_at,
                    last_accessed_at = NOW(),
                    updated_at = NOW()
            """,
                thread_id, user_id, tenant_id or "", assistant_id,
                json.dumps(metadata or {}), is_anonymous, expires_at
            )

    async def get_langgraph_thread(self, thread_id: str) -> Optional[Dict[str, Any]]:
        """获取 LangGraph Thread"""
        if not self._pool:
            return None
        async with self._pool.acquire() as conn:
            row = await conn.fetchrow(
                "SELECT * FROM langgraph_threads WHERE thread_id = $1 AND status = 'active'",
                thread_id
            )
            if row:
                await conn.execute(
                    "UPDATE langgraph_threads SET last_accessed_at = NOW() WHERE thread_id = $1",
                    thread_id
                )
            return self._row_to_dict(row) if row else None

    async def list_user_threads(
        self,
        user_id: str,
        tenant_id: Optional[str] = None,
        limit: int = 100
    ) -> List[Dict[str, Any]]:
        """获取用户的 LangGraph Threads"""
        if not self._pool:
            return []
        
        query = "SELECT * FROM langgraph_threads WHERE user_id = $1 AND status = 'active'"
        params = [user_id]
        param_idx = 2
        
        if tenant_id:
            query += f" AND tenant_id = ${param_idx}"
            params.append(tenant_id)
            param_idx += 1
        
        query += f" ORDER BY last_accessed_at DESC NULLS LAST LIMIT ${param_idx}"
        params.append(limit)
        
        async with self._pool.acquire() as conn:
            rows = await conn.fetch(query, *params)
            return [self._row_to_dict(row) for row in rows]

    # =========================================================================
    # 语义缓存表 (semantic_cache)
    # =========================================================================

    async def save_cache(
        self,
        service_id: str,
        input_hash: str,
        output_text: str,
        input_text: str = None,
        output_data: Dict = None,
        metadata: Dict = None,
        expires_at: datetime = None
    ) -> None:
        """保存语义缓存"""
        if not self._pool:
            return
        async with self._pool.acquire() as conn:
            await conn.execute("""
                INSERT INTO semantic_cache (
                    service_id, input_hash, input_text, output_text, 
                    output_data, metadata, expires_at
                ) VALUES ($1, $2, $3, $4, $5, $6, $7)
                ON CONFLICT (service_id, input_hash) DO UPDATE SET
                    output_text = EXCLUDED.output_text,
                    output_data = EXCLUDED.output_data,
                    metadata = EXCLUDED.metadata,
                    expires_at = EXCLUDED.expires_at,
                    hit_count = semantic_cache.hit_count + 1,
                    last_hit_at = NOW()
            """,
                service_id, input_hash, input_text, output_text,
                json.dumps(output_data) if output_data else None,
                json.dumps(metadata or {}), expires_at
            )

    async def get_cache(
        self,
        service_id: str,
        input_hash: str
    ) -> Optional[Dict[str, Any]]:
        """获取语义缓存"""
        if not self._pool:
            return None
        async with self._pool.acquire() as conn:
            row = await conn.fetchrow("""
                SELECT * FROM semantic_cache 
                WHERE service_id = $1 AND input_hash = $2
                AND (expires_at IS NULL OR expires_at > NOW())
            """, service_id, input_hash)
            if row:
                # 更新命中统计
                await conn.execute("""
                    UPDATE semantic_cache SET 
                        hit_count = hit_count + 1, 
                        last_hit_at = NOW() 
                    WHERE service_id = $1 AND input_hash = $2
                """, service_id, input_hash)
            return self._row_to_dict(row) if row else None

    async def cleanup_expired_cache(self) -> int:
        """清理过期缓存"""
        if not self._pool:
            return 0
        async with self._pool.acquire() as conn:
            result = await conn.execute(
                "DELETE FROM semantic_cache WHERE expires_at IS NOT NULL AND expires_at < NOW()"
            )
            if result.startswith("DELETE "):
                return int(result.split()[-1])
            return 0

    # =========================================================================
    # 鉴权配置表 (auth_config)
    # =========================================================================

    async def get_auth_config(self, config_type: str) -> Optional[Dict[str, Any]]:
        """获取鉴权配置"""
        if not self._pool:
            return None
        async with self._pool.acquire() as conn:
            row = await conn.fetchrow(
                "SELECT * FROM auth_config WHERE config_type = $1 AND enabled = TRUE",
                config_type
            )
            return self._row_to_dict(row) if row else None

    async def save_auth_config(
        self,
        config_type: str,
        config: Dict[str, Any],
        enabled: bool = True
    ) -> None:
        """保存鉴权配置"""
        if not self._pool:
            return
        async with self._pool.acquire() as conn:
            await conn.execute("""
                INSERT INTO auth_config (config_type, config, enabled)
                VALUES ($1, $2, $3)
                ON CONFLICT (config_type) DO UPDATE SET
                    config = EXCLUDED.config,
                    enabled = EXCLUDED.enabled,
                    updated_at = NOW()
            """, config_type, json.dumps(config), enabled)

    # =========================================================================
    # Confluence 集成表
    # =========================================================================

    async def save_confluence_connection(self, connection: Dict[str, Any]) -> None:
        """保存或更新 Confluence 连接配置"""
        if not self._pool:
            return
        async with self._pool.acquire() as conn:
            await conn.execute("""
                INSERT INTO confluence_connections (
                    connection_id, tenant_id, name, domain, email, api_token,
                    sync_mode, polling_interval_minutes, status,
                    last_sync_at, last_error, created_by, owner_id
                ) VALUES ($1, $2, $3, $4, $5, $6, $7, $8, $9, $10, $11, $12, $13)
                ON CONFLICT (connection_id) DO UPDATE SET
                    name = EXCLUDED.name,
                    domain = EXCLUDED.domain,
                    email = EXCLUDED.email,
                    api_token = EXCLUDED.api_token,
                    sync_mode = EXCLUDED.sync_mode,
                    polling_interval_minutes = EXCLUDED.polling_interval_minutes,
                    status = EXCLUDED.status,
                    last_sync_at = EXCLUDED.last_sync_at,
                    last_error = EXCLUDED.last_error,
                    updated_at = NOW()
            """,
                connection.get("connection_id"),
                connection.get("tenant_id", ""),
                connection.get("name"),
                connection.get("domain"),
                connection.get("email"),
                connection.get("api_token"),
                connection.get("sync_mode", "manual"),
                connection.get("polling_interval_minutes", 60),
                connection.get("status", "active"),
                connection.get("last_sync_at"),
                connection.get("last_error"),
                connection.get("created_by"),
                # owner_id: 优先使用显式设置的 owner_id，否则回退到 created_by
                connection.get("owner_id") or connection.get("created_by"),
            )

    async def get_confluence_connection(self, connection_id: str) -> Optional[Dict[str, Any]]:
        """获取 Confluence 连接配置"""
        if not self._pool:
            return None
        async with self._pool.acquire() as conn:
            row = await conn.fetchrow(
                "SELECT * FROM confluence_connections WHERE connection_id = $1",
                connection_id
            )
            return self._row_to_dict(row) if row else None

    async def list_confluence_connections(
        self,
        tenant_id: Optional[str] = None,
        status: Optional[str] = None,
        limit: int = 100,
    ) -> List[Dict[str, Any]]:
        """列出 Confluence 连接"""
        if not self._pool:
            return []

        query = "SELECT * FROM confluence_connections WHERE 1=1"
        params: List[Any] = []
        param_idx = 1

        if tenant_id:
            query += f" AND tenant_id = ${param_idx}"
            params.append(tenant_id)
            param_idx += 1

        if status:
            query += f" AND status = ${param_idx}"
            params.append(status)
            param_idx += 1

        query += f" ORDER BY created_at DESC LIMIT ${param_idx}"
        params.append(limit)

        async with self._pool.acquire() as conn:
            rows = await conn.fetch(query, *params)
            return [self._row_to_dict(row) for row in rows]

    async def get_confluence_connections_with_polling(self) -> List[Dict[str, Any]]:
        """获取启用轮询的 Confluence 连接"""
        if not self._pool:
            return []
        async with self._pool.acquire() as conn:
            rows = await conn.fetch("""
                SELECT * FROM confluence_connections
                WHERE sync_mode = 'polling' AND status = 'active'
                ORDER BY created_at
            """)
            return [self._row_to_dict(row) for row in rows]

    async def update_confluence_connection_status(
        self,
        connection_id: str,
        status: str,
        last_sync_at: Optional[datetime] = None,
        last_error: Optional[str] = None,
    ) -> None:
        """更新 Confluence 连接状态"""
        if not self._pool:
            return

        updates = ["status = $1", "updated_at = NOW()"]
        params: List[Any] = [status]
        param_idx = 2

        if last_sync_at:
            updates.append(f"last_sync_at = ${param_idx}")
            params.append(last_sync_at)
            param_idx += 1

        if last_error is not None:
            updates.append(f"last_error = ${param_idx}")
            params.append(last_error if last_error else None)
            param_idx += 1

        params.append(connection_id)
        query = f"UPDATE confluence_connections SET {', '.join(updates)} WHERE connection_id = ${param_idx}"

        async with self._pool.acquire() as conn:
            await conn.execute(query, *params)

    async def delete_confluence_connection(self, connection_id: str) -> bool:
        """删除 Confluence 连接（级联删除绑定）"""
        if not self._pool:
            return False
        async with self._pool.acquire() as conn:
            result = await conn.execute(
                "DELETE FROM confluence_connections WHERE connection_id = $1",
                connection_id
            )
            if result.startswith("DELETE "):
                return int(result.split()[-1]) > 0
            return False

    async def update_confluence_connection(
        self,
        connection_id: str,
        updates: Dict[str, Any],
    ) -> None:
        """更新 Confluence 连接配置"""
        if not self._pool or not updates:
            return

        set_clauses = []
        params: List[Any] = []
        param_idx = 1

        allowed_fields = {
            "name", "domain", "email", "api_token", "sync_mode",
            "polling_interval_minutes", "status", "last_sync_at", "last_error"
        }

        for key, value in updates.items():
            if key in allowed_fields:
                set_clauses.append(f"{key} = ${param_idx}")
                params.append(value)
                param_idx += 1

        if not set_clauses:
            return

        set_clauses.append("updated_at = NOW()")
        params.append(connection_id)

        query = f"UPDATE confluence_connections SET {', '.join(set_clauses)} WHERE connection_id = ${param_idx}"

        async with self._pool.acquire() as conn:
            await conn.execute(query, *params)

    async def find_confluence_connection_by_domain(
        self,
        domain: str,
        tenant_id: Optional[str] = None,
    ) -> Optional[Dict[str, Any]]:
        """根据域名查找连接"""
        if not self._pool:
            return None

        query = "SELECT * FROM confluence_connections WHERE domain = $1"
        params: List[Any] = [domain]

        if tenant_id:
            query += " AND tenant_id = $2"
            params.append(tenant_id)

        query += " AND status = 'active' ORDER BY created_at DESC LIMIT 1"

        async with self._pool.acquire() as conn:
            row = await conn.fetchrow(query, *params)
            return self._row_to_dict(row) if row else None

    # =========================================================================
    # Confluence Space Binding 表
    # =========================================================================

    async def save_confluence_binding(self, binding: Dict[str, Any]) -> Optional[Dict[str, Any]]:
        """
        保存或更新 Confluence 空间绑定

        使用 RETURNING 子句在单个事务中完成保存和返回，确保原子性。

        Returns:
            保存后的绑定数据，如果失败返回 None
        """
        if not self._pool:
            return None
        async with self._pool.acquire() as conn:
            row = await conn.fetchrow("""
                INSERT INTO confluence_space_bindings (
                    binding_id, connection_id, tenant_id, dataset_id, space_key, space_id,
                    space_name, root_page_id, root_page_title,
                    include_patterns, exclude_patterns, max_depth,
                    include_attachments, include_comments, status,
                    last_sync_at, synced_page_count, total_page_count,
                    last_error, created_by, owner_id
                ) VALUES ($1, $2, $3, $4, $5, $6, $7, $8, $9, $10, $11, $12, $13, $14, $15, $16, $17, $18, $19, $20, $21)
                ON CONFLICT (binding_id) DO UPDATE SET
                    space_id = EXCLUDED.space_id,
                    space_name = EXCLUDED.space_name,
                    root_page_id = EXCLUDED.root_page_id,
                    root_page_title = EXCLUDED.root_page_title,
                    include_patterns = EXCLUDED.include_patterns,
                    exclude_patterns = EXCLUDED.exclude_patterns,
                    max_depth = EXCLUDED.max_depth,
                    include_attachments = EXCLUDED.include_attachments,
                    include_comments = EXCLUDED.include_comments,
                    status = EXCLUDED.status,
                    last_sync_at = EXCLUDED.last_sync_at,
                    synced_page_count = EXCLUDED.synced_page_count,
                    total_page_count = EXCLUDED.total_page_count,
                    last_error = EXCLUDED.last_error,
                    updated_at = NOW()
                RETURNING *
            """,
                binding.get("binding_id"),
                binding.get("connection_id"),
                binding.get("tenant_id"),
                binding.get("dataset_id"),
                binding.get("space_key"),
                binding.get("space_id"),
                binding.get("space_name"),
                binding.get("root_page_id"),
                binding.get("root_page_title"),
                json.dumps(binding.get("include_patterns", [])),
                json.dumps(binding.get("exclude_patterns", [])),
                binding.get("max_depth", 10),
                binding.get("include_attachments", False),
                binding.get("include_comments", False),
                binding.get("status", "pending"),
                binding.get("last_sync_at"),
                binding.get("synced_page_count", 0),
                binding.get("total_page_count", 0),
                binding.get("last_error"),
                binding.get("created_by"),
                # owner_id: 优先使用显式设置的 owner_id，否则回退到 created_by
                binding.get("owner_id") or binding.get("created_by"),
            )
            return self._row_to_dict(row) if row else None

    async def get_confluence_binding(self, binding_id: str) -> Optional[Dict[str, Any]]:
        """获取 Confluence 空间绑定"""
        if not self._pool:
            return None
        async with self._pool.acquire() as conn:
            row = await conn.fetchrow(
                "SELECT * FROM confluence_space_bindings WHERE binding_id = $1",
                binding_id
            )
            return self._row_to_dict(row) if row else None

    async def get_confluence_bindings_by_connection(
        self, connection_id: str
    ) -> List[Dict[str, Any]]:
        """获取连接下的所有空间绑定"""
        if not self._pool:
            return []
        async with self._pool.acquire() as conn:
            rows = await conn.fetch("""
                SELECT * FROM confluence_space_bindings
                WHERE connection_id = $1
                ORDER BY created_at
            """, connection_id)
            return [self._row_to_dict(row) for row in rows]

    async def get_confluence_bindings_by_dataset(
        self, dataset_id: str
    ) -> List[Dict[str, Any]]:
        """获取数据集关联的所有空间绑定"""
        if not self._pool:
            return []
        async with self._pool.acquire() as conn:
            rows = await conn.fetch("""
                SELECT * FROM confluence_space_bindings
                WHERE dataset_id = $1
                ORDER BY created_at
            """, dataset_id)
            return [self._row_to_dict(row) for row in rows]

    async def update_confluence_binding_status(
        self,
        binding_id: str,
        status: str,
        synced_page_count: Optional[int] = None,
        total_page_count: Optional[int] = None,
        last_sync_at: Optional[datetime] = None,
        last_error: Optional[str] = None,
    ) -> None:
        """更新 Confluence 空间绑定状态"""
        if not self._pool:
            return

        updates = ["status = $1", "updated_at = NOW()"]
        params: List[Any] = [status]
        param_idx = 2

        if synced_page_count is not None:
            updates.append(f"synced_page_count = ${param_idx}")
            params.append(synced_page_count)
            param_idx += 1

        if total_page_count is not None:
            updates.append(f"total_page_count = ${param_idx}")
            params.append(total_page_count)
            param_idx += 1

        if last_sync_at:
            updates.append(f"last_sync_at = ${param_idx}")
            params.append(last_sync_at)
            param_idx += 1

        if last_error is not None:
            updates.append(f"last_error = ${param_idx}")
            params.append(last_error if last_error else None)
            param_idx += 1

        params.append(binding_id)
        query = f"UPDATE confluence_space_bindings SET {', '.join(updates)} WHERE binding_id = ${param_idx}"

        async with self._pool.acquire() as conn:
            await conn.execute(query, *params)

    async def delete_confluence_binding(self, binding_id: str) -> bool:
        """删除 Confluence 空间绑定（级联删除页面记录）"""
        if not self._pool:
            return False
        async with self._pool.acquire() as conn:
            result = await conn.execute(
                "DELETE FROM confluence_space_bindings WHERE binding_id = $1",
                binding_id
            )
            if result.startswith("DELETE "):
                return int(result.split()[-1]) > 0
            return False

    async def update_confluence_binding(
        self,
        binding_id: str,
        updates: Dict[str, Any],
    ) -> None:
        """更新 Confluence 空间绑定"""
        if not self._pool or not updates:
            return

        set_clauses = []
        params: List[Any] = []
        param_idx = 1

        allowed_fields = {
            "space_id", "space_name", "include_patterns", "exclude_patterns",
            "max_depth", "include_attachments", "include_comments", "status",
            "last_sync_at", "synced_page_count", "total_page_count", "last_error",
            "root_page_id", "root_page_title",
            "sync_mode", "polling_interval_minutes", "last_incremental_sync_at",  # binding 级别同步配置
            "sync_enabled", "next_sync_at",  # 调度器相关
        }

        # JSON 字段需要序列化
        json_fields = {"include_patterns", "exclude_patterns"}

        for key, value in updates.items():
            if key in allowed_fields:
                set_clauses.append(f"{key} = ${param_idx}")
                # JSON 字段需要序列化为字符串
                if key in json_fields and isinstance(value, (list, dict)):
                    params.append(json.dumps(value))
                else:
                    params.append(value)
                param_idx += 1

        if not set_clauses:
            return

        set_clauses.append("updated_at = NOW()")
        params.append(binding_id)

        query = f"UPDATE confluence_space_bindings SET {', '.join(set_clauses)} WHERE binding_id = ${param_idx}"

        async with self._pool.acquire() as conn:
            await conn.execute(query, *params)

    async def list_confluence_bindings(
        self,
        connection_id: Optional[str] = None,
        tenant_id: Optional[str] = None,
        dataset_id: Optional[str] = None,
        status: Optional[str] = None,
        limit: int = 100,
    ) -> List[Dict[str, Any]]:
        """列出 Confluence 空间绑定"""
        if not self._pool:
            return []

        query = "SELECT * FROM confluence_space_bindings WHERE 1=1"
        params: List[Any] = []
        param_idx = 1

        if connection_id:
            query += f" AND connection_id = ${param_idx}"
            params.append(connection_id)
            param_idx += 1

        if tenant_id:
            query += f" AND tenant_id = ${param_idx}"
            params.append(tenant_id)
            param_idx += 1

        if dataset_id:
            query += f" AND dataset_id = ${param_idx}"
            params.append(dataset_id)
            param_idx += 1

        if status:
            query += f" AND status = ${param_idx}"
            params.append(status)
            param_idx += 1

        query += f" ORDER BY created_at DESC LIMIT ${param_idx}"
        params.append(limit)

        async with self._pool.acquire() as conn:
            rows = await conn.fetch(query, *params)
            return [self._row_to_dict(row) for row in rows]

    # =========================================================================
    # Confluence Page 表
    # =========================================================================

    async def save_confluence_page(self, page: Dict[str, Any]) -> None:
        """保存或更新 Confluence 页面记录"""
        if not self._pool:
            return
        async with self._pool.acquire() as conn:
            await conn.execute("""
                INSERT INTO confluence_pages (
                    id, binding_id, document_id, page_id, space_key, title,
                    version, content_hash, parent_page_id, depth, status,
                    last_synced_at, confluence_updated_at, error, labels, web_url, author
                ) VALUES ($1, $2, $3, $4, $5, $6, $7, $8, $9, $10, $11, $12, $13, $14, $15, $16, $17)
                ON CONFLICT (id) DO UPDATE SET
                    document_id = EXCLUDED.document_id,
                    title = EXCLUDED.title,
                    version = EXCLUDED.version,
                    content_hash = EXCLUDED.content_hash,
                    status = EXCLUDED.status,
                    last_synced_at = EXCLUDED.last_synced_at,
                    confluence_updated_at = EXCLUDED.confluence_updated_at,
                    error = EXCLUDED.error,
                    labels = EXCLUDED.labels,
                    web_url = EXCLUDED.web_url,
                    updated_at = NOW()
            """,
                page.get("id"),
                page.get("binding_id"),
                page.get("document_id"),
                page.get("page_id"),
                page.get("space_key"),
                page.get("title"),
                page.get("version", 1),
                page.get("content_hash"),
                page.get("parent_page_id"),
                page.get("depth", 0),
                page.get("status", "pending"),
                page.get("last_synced_at"),
                page.get("confluence_updated_at"),
                page.get("error"),
                page.get("labels", []),
                page.get("web_url"),
                page.get("author"),
            )

    async def get_confluence_page(
        self, page_record_id: str
    ) -> Optional[Dict[str, Any]]:
        """通过记录 ID 获取 Confluence 页面记录"""
        if not self._pool:
            return None
        async with self._pool.acquire() as conn:
            row = await conn.fetchrow(
                "SELECT * FROM confluence_pages WHERE id = $1",
                page_record_id
            )
            return self._row_to_dict(row) if row else None

    async def get_confluence_page_by_binding_and_page(
        self, binding_id: str, page_id: str
    ) -> Optional[Dict[str, Any]]:
        """通过绑定 ID 和页面 ID 获取 Confluence 页面记录"""
        if not self._pool:
            return None
        async with self._pool.acquire() as conn:
            row = await conn.fetchrow("""
                SELECT * FROM confluence_pages
                WHERE binding_id = $1 AND page_id = $2
            """, binding_id, page_id)
            return self._row_to_dict(row) if row else None

    async def get_confluence_page_by_document(
        self, document_id: str
    ) -> Optional[Dict[str, Any]]:
        """根据文档 ID 获取 Confluence 页面记录"""
        if not self._pool:
            return None
        async with self._pool.acquire() as conn:
            row = await conn.fetchrow(
                "SELECT * FROM confluence_pages WHERE document_id = $1",
                document_id
            )
            return self._row_to_dict(row) if row else None

    async def list_confluence_pages(
        self,
        binding_id: str,
        status: Optional[str] = None,
        limit: int = 100,
        offset: int = 0,
    ) -> List[Dict[str, Any]]:
        """
        列出 Confluence 页面记录

        返回结果包含关联文档的处理状态:
        - document_status: 文档的实际处理状态 (uploaded/parsing/embedding/completed/failed)
        - document_progress: 文档处理进度 (0-100)
        - effective_status: 计算后的有效状态，检测是否需要重新同步
          当页面标记为 'synced' 但关联文档不存在或没有任何 segment 时，
          返回 'needs_resync' 表示需要重新同步
        """
        if not self._pool:
            return []

        # JOIN documents 表获取关联文档的处理状态
        # 计算 effective_status: 检测 synced 状态但实际无数据的情况
        query = """
            SELECT
                cp.*,
                d.status AS document_status,
                d.progress AS document_progress,
                CASE
                    WHEN cp.status = 'synced'
                         AND cp.document_id IS NOT NULL
                         AND (d.document_id IS NULL OR COALESCE(d.segment_count, 0) = 0)
                    THEN 'needs_resync'
                    ELSE cp.status
                END AS effective_status
            FROM confluence_pages cp
            LEFT JOIN documents d ON cp.document_id = d.document_id
            WHERE cp.binding_id = $1
        """
        params: List[Any] = [binding_id]
        param_idx = 2

        if status:
            query += f" AND cp.status = ${param_idx}"
            params.append(status)
            param_idx += 1

        query += f" ORDER BY cp.title ASC LIMIT ${param_idx} OFFSET ${param_idx + 1}"
        params.extend([limit, offset])

        async with self._pool.acquire() as conn:
            rows = await conn.fetch(query, *params)
            return [self._row_to_dict(row) for row in rows]

    async def update_confluence_page_status(
        self,
        id: str,
        status: str,
        document_id: Optional[str] = None,
        content_hash: Optional[str] = None,
        version: Optional[int] = None,
        last_synced_at: Optional[datetime] = None,
        error: Optional[str] = None,
    ) -> None:
        """更新 Confluence 页面状态"""
        if not self._pool:
            return

        updates = ["status = $1", "updated_at = NOW()"]
        params: List[Any] = [status]
        param_idx = 2

        if document_id is not None:
            updates.append(f"document_id = ${param_idx}")
            params.append(document_id)
            param_idx += 1

        if content_hash is not None:
            updates.append(f"content_hash = ${param_idx}")
            params.append(content_hash)
            param_idx += 1

        if version is not None:
            updates.append(f"version = ${param_idx}")
            params.append(version)
            param_idx += 1

        if last_synced_at:
            updates.append(f"last_synced_at = ${param_idx}")
            params.append(last_synced_at)
            param_idx += 1

        if error is not None:
            updates.append(f"error = ${param_idx}")
            params.append(error if error else None)
            param_idx += 1

        params.append(id)
        query = f"UPDATE confluence_pages SET {', '.join(updates)} WHERE id = ${param_idx}"

        async with self._pool.acquire() as conn:
            await conn.execute(query, *params)

    async def update_confluence_page_image_count(
        self,
        binding_id: str,
        page_id: str,
        image_count: int,
    ) -> None:
        """更新 Confluence 页面的图片数量

        Args:
            binding_id: 绑定ID
            page_id: Confluence页面ID
            image_count: 图片数量（必须 >= 0）
        """
        if not self._pool:
            return
        # Ensure non-negative image count
        if image_count < 0:
            image_count = 0
        async with self._pool.acquire() as conn:
            await conn.execute(
                """
                UPDATE confluence_pages
                SET image_count = $1, images_synced_at = NOW()
                WHERE binding_id = $2 AND page_id = $3
                """,
                image_count,
                binding_id,
                page_id,
            )

    async def delete_confluence_page(self, id: str) -> bool:
        """删除 Confluence 页面记录"""
        if not self._pool:
            return False
        async with self._pool.acquire() as conn:
            result = await conn.execute(
                "DELETE FROM confluence_pages WHERE id = $1", id
            )
            if result.startswith("DELETE "):
                return int(result.split()[-1]) > 0
            return False

    async def delete_confluence_pages_by_binding(self, binding_id: str) -> int:
        """删除绑定下的所有页面记录"""
        if not self._pool:
            return 0
        async with self._pool.acquire() as conn:
            result = await conn.execute(
                "DELETE FROM confluence_pages WHERE binding_id = $1",
                binding_id
            )
            if result.startswith("DELETE "):
                return int(result.split()[-1])
            return 0

    async def get_confluence_page_by_page_id(
        self,
        binding_id: str,
        page_id: str,
    ) -> Optional[Dict[str, Any]]:
        """根据 Confluence page_id 获取页面记录"""
        if not self._pool:
            return None
        async with self._pool.acquire() as conn:
            row = await conn.fetchrow("""
                SELECT * FROM confluence_pages
                WHERE binding_id = $1 AND page_id = $2
            """, binding_id, page_id)
            return self._row_to_dict(row) if row else None

    async def update_confluence_page_sync_config(
        self,
        page_id: str,
        updates: Dict[str, Any],
    ) -> Optional[Dict[str, Any]]:
        """更新页面级同步配置"""
        if not self._pool:
            return None

        allowed_fields = {
            "sync_mode", "polling_interval_minutes", "sync_enabled",
            "next_sync_at", "sync_priority",
        }

        set_clauses = ["updated_at = NOW()"]
        params: List[Any] = []
        param_idx = 1

        for key, value in updates.items():
            if key in allowed_fields:
                set_clauses.append(f"{key} = ${param_idx}")
                params.append(value)
                param_idx += 1

        if len(set_clauses) == 1:  # Only updated_at
            return None

        params.append(page_id)
        query = f"""
            UPDATE confluence_pages
            SET {', '.join(set_clauses)}
            WHERE id = ${param_idx}
            RETURNING *
        """

        async with self._pool.acquire() as conn:
            row = await conn.fetchrow(query, *params)
            return self._row_to_dict(row) if row else None

    async def get_bindings_due_for_sync(self, limit: int = 100) -> List[Dict[str, Any]]:
        """获取需要同步的绑定列表（next_sync_at <= now）"""
        if not self._pool:
            return []
        async with self._pool.acquire() as conn:
            rows = await conn.fetch("""
                SELECT * FROM confluence_space_bindings
                WHERE sync_enabled = TRUE
                  AND sync_mode = 'polling'
                  AND next_sync_at IS NOT NULL
                  AND next_sync_at <= NOW()
                ORDER BY next_sync_at ASC
                LIMIT $1
            """, limit)
            return [self._row_to_dict(row) for row in rows]

    async def get_pages_due_for_sync(self, limit: int = 100) -> List[Dict[str, Any]]:
        """获取需要同步的页面列表（有独立配置且 next_sync_at <= now）"""
        if not self._pool:
            return []
        async with self._pool.acquire() as conn:
            rows = await conn.fetch("""
                SELECT * FROM confluence_pages
                WHERE sync_enabled = TRUE
                  AND sync_mode = 'polling'
                  AND next_sync_at IS NOT NULL
                  AND next_sync_at <= NOW()
                ORDER BY sync_priority DESC, next_sync_at ASC
                LIMIT $1
            """, limit)
            return [self._row_to_dict(row) for row in rows]

    async def get_all_polling_pages(self, limit: int = 500) -> List[Dict[str, Any]]:
        """
        获取所有启用轮询的页面（用于调度器初始化）

        与 get_pages_due_for_sync 不同，此方法返回所有启用轮询的页面，
        无论 next_sync_at 是否已到期。调度器会根据 next_sync_at 决定何时执行。

        Returns:
            所有 sync_enabled=TRUE AND sync_mode='polling' 的页面
        """
        if not self._pool:
            return []
        async with self._pool.acquire() as conn:
            rows = await conn.fetch("""
                SELECT * FROM confluence_pages
                WHERE sync_enabled = TRUE
                  AND sync_mode = 'polling'
                ORDER BY sync_priority DESC, next_sync_at ASC NULLS FIRST
                LIMIT $1
            """, limit)
            return [self._row_to_dict(row) for row in rows]

    async def schedule_next_sync(
        self,
        table: str,
        id_field: str,
        id_value: str,
        interval_minutes: int,
    ) -> None:
        """设置下次同步时间

        Args:
            table: 表名（仅支持白名单中的表）
            id_field: ID 字段名（仅支持白名单中的字段）
            id_value: ID 值
            interval_minutes: 间隔分钟数

        Raises:
            ValueError: 如果表名或字段名不在白名单中
        """
        if not self._pool:
            return

        # 白名单验证，防止 SQL 注入
        allowed_tables = {"confluence_space_bindings", "confluence_pages"}
        allowed_id_fields = {"binding_id", "id"}

        if table not in allowed_tables:
            raise ValueError(f"Invalid table name: {table}")
        if id_field not in allowed_id_fields:
            raise ValueError(f"Invalid id field: {id_field}")

        # 验证 interval_minutes 是有效的整数
        interval_minutes = int(interval_minutes)
        if interval_minutes < 0 or interval_minutes > 10080:  # 最大 7 天
            raise ValueError(f"Invalid interval_minutes: {interval_minutes}")

        async with self._pool.acquire() as conn:
            # 使用参数化的 interval 值
            await conn.execute(f"""
                UPDATE {table}
                SET next_sync_at = NOW() + $1 * INTERVAL '1 minute',
                    updated_at = NOW()
                WHERE {id_field} = $2
            """, interval_minutes, id_value)

    async def upsert_confluence_page(
        self,
        binding_id: str,
        page_id: str,
        document_id: Optional[str] = None,
        space_key: str = "",
        title: str = "",
        version: int = 1,
        content_hash: Optional[str] = None,
        parent_page_id: Optional[str] = None,
        depth: int = 0,
        status: str = "synced",
        labels: Optional[List[str]] = None,
        web_url: Optional[str] = None,
        author: Optional[str] = None,
        confluence_updated_at: Optional[str] = None,
    ) -> None:
        """插入或更新 Confluence 页面记录"""
        if not self._pool:
            return

        record_id = f"{binding_id}:{page_id}"

        # 将 ISO 字符串转换为 datetime 对象
        updated_at_dt = None
        if confluence_updated_at:
            if isinstance(confluence_updated_at, str):
                # 处理 ISO 格式: '2025-08-12T04:04:43.266Z'
                updated_at_dt = datetime.fromisoformat(
                    confluence_updated_at.replace("Z", "+00:00")
                )
            else:
                updated_at_dt = confluence_updated_at

        async with self._pool.acquire() as conn:
            await conn.execute("""
                INSERT INTO confluence_pages (
                    id, binding_id, document_id, page_id, space_key, title,
                    version, content_hash, parent_page_id, depth, status,
                    last_synced_at, confluence_updated_at, labels, web_url, author
                ) VALUES ($1, $2, $3, $4, $5, $6, $7, $8, $9, $10, $11, NOW(), $12, $13, $14, $15)
                ON CONFLICT (id) DO UPDATE SET
                    document_id = EXCLUDED.document_id,
                    title = EXCLUDED.title,
                    version = EXCLUDED.version,
                    content_hash = EXCLUDED.content_hash,
                    status = EXCLUDED.status,
                    last_synced_at = NOW(),
                    confluence_updated_at = EXCLUDED.confluence_updated_at,
                    labels = EXCLUDED.labels,
                    web_url = EXCLUDED.web_url,
                    updated_at = NOW()
            """,
                record_id,
                binding_id,
                document_id,
                page_id,
                space_key,
                title,
                version,
                content_hash,
                parent_page_id,
                depth,
                status,
                updated_at_dt,
                json.dumps(labels or []),  # JSONB 列需要 JSON 字符串
                web_url,
                author,
            )

    async def delete_confluence_page_by_page_id(
        self,
        binding_id: str,
        page_id: str,
    ) -> bool:
        """根据 page_id 删除页面记录"""
        if not self._pool:
            return False
        async with self._pool.acquire() as conn:
            result = await conn.execute("""
                DELETE FROM confluence_pages
                WHERE binding_id = $1 AND page_id = $2
            """, binding_id, page_id)
            if result.startswith("DELETE "):
                return int(result.split()[-1]) > 0
            return False

    # =========================================================================
    # Confluence Sync Task 表
    # =========================================================================

    async def save_confluence_sync_task(self, task: Dict[str, Any]) -> None:
        """保存或更新 Confluence 同步任务"""
        if not self._pool:
            return
        async with self._pool.acquire() as conn:
            await conn.execute("""
                INSERT INTO confluence_sync_tasks (
                    task_id, binding_id, page_id, task_type, priority,
                    status, retry_count, max_retries, progress,
                    total_items, processed_items, error, result,
                    started_at, completed_at, owner_id
                ) VALUES ($1, $2, $3, $4, $5, $6, $7, $8, $9, $10, $11, $12, $13, $14, $15, $16)
                ON CONFLICT (task_id) DO UPDATE SET
                    status = EXCLUDED.status,
                    retry_count = EXCLUDED.retry_count,
                    progress = EXCLUDED.progress,
                    total_items = EXCLUDED.total_items,
                    processed_items = EXCLUDED.processed_items,
                    error = EXCLUDED.error,
                    result = EXCLUDED.result,
                    started_at = EXCLUDED.started_at,
                    completed_at = EXCLUDED.completed_at,
                    updated_at = NOW()
            """,
                task.get("task_id"),
                task.get("binding_id"),
                task.get("page_id"),
                task.get("task_type"),
                task.get("priority", 0),
                task.get("status", "pending"),
                task.get("retry_count", 0),
                task.get("max_retries", 3),
                task.get("progress", 0.0),
                task.get("total_items", 0),
                task.get("processed_items", 0),
                task.get("error"),
                json.dumps(task.get("result")) if task.get("result") else None,
                task.get("started_at"),
                task.get("completed_at"),
                task.get("owner_id"),  # ACL: owner_id for access control
            )

    async def get_confluence_sync_task(self, task_id: str) -> Optional[Dict[str, Any]]:
        """获取 Confluence 同步任务"""
        if not self._pool:
            return None
        async with self._pool.acquire() as conn:
            row = await conn.fetchrow(
                "SELECT * FROM confluence_sync_tasks WHERE task_id = $1",
                task_id
            )
            return self._row_to_dict(row) if row else None

    async def list_confluence_sync_tasks(
        self,
        binding_id: Optional[str] = None,
        status: Optional[str] = None,
        limit: int = 100,
    ) -> List[Dict[str, Any]]:
        """列出 Confluence 同步任务"""
        if not self._pool:
            return []

        query = "SELECT * FROM confluence_sync_tasks WHERE 1=1"
        params: List[Any] = []
        param_idx = 1

        if binding_id:
            query += f" AND binding_id = ${param_idx}"
            params.append(binding_id)
            param_idx += 1

        if status:
            query += f" AND status = ${param_idx}"
            params.append(status)
            param_idx += 1

        query += f" ORDER BY priority DESC, created_at ASC LIMIT ${param_idx}"
        params.append(limit)

        async with self._pool.acquire() as conn:
            rows = await conn.fetch(query, *params)
            return [self._row_to_dict(row) for row in rows]

    async def update_confluence_sync_task_status(
        self,
        task_id: str,
        status: str,
        progress: Optional[float] = None,
        processed_items: Optional[int] = None,
        error: Optional[str] = None,
        result: Optional[Dict] = None,
    ) -> None:
        """更新 Confluence 同步任务状态"""
        if not self._pool:
            return

        updates = ["status = $1", "updated_at = NOW()"]
        params: List[Any] = [status]
        param_idx = 2

        if progress is not None:
            updates.append(f"progress = ${param_idx}")
            params.append(progress)
            param_idx += 1

        if processed_items is not None:
            updates.append(f"processed_items = ${param_idx}")
            params.append(processed_items)
            param_idx += 1

        if error is not None:
            updates.append(f"error = ${param_idx}")
            params.append(error if error else None)
            param_idx += 1

        if result is not None:
            updates.append(f"result = ${param_idx}")
            params.append(json.dumps(result))
            param_idx += 1

        if status == "processing":
            updates.append(f"started_at = COALESCE(started_at, ${param_idx})")
            params.append(datetime.utcnow())
            param_idx += 1
        elif status in ("completed", "failed"):
            updates.append(f"completed_at = ${param_idx}")
            params.append(datetime.utcnow())
            param_idx += 1

        params.append(task_id)
        query = f"UPDATE confluence_sync_tasks SET {', '.join(updates)} WHERE task_id = ${param_idx}"

        async with self._pool.acquire() as conn:
            await conn.execute(query, *params)

    async def get_pending_confluence_sync_tasks(self, limit: int = 10) -> List[Dict[str, Any]]:
        """获取待处理的 Confluence 同步任务"""
        if not self._pool:
            return []
        async with self._pool.acquire() as conn:
            rows = await conn.fetch("""
                SELECT * FROM confluence_sync_tasks
                WHERE status = 'pending'
                ORDER BY priority DESC, created_at ASC
                LIMIT $1
            """, limit)
            return [self._row_to_dict(row) for row in rows]

    async def create_confluence_sync_task(
        self,
        task_id: str,
        binding_id: Optional[str] = None,
        page_id: Optional[str] = None,
        task_type: str = "full_sync",
        priority: int = 0,
        owner_id: Optional[str] = None,
    ) -> None:
        """创建 Confluence 同步任务"""
        if not self._pool:
            return

        async with self._pool.acquire() as conn:
            await conn.execute("""
                INSERT INTO confluence_sync_tasks (
                    task_id, binding_id, page_id, task_type, priority,
                    status, retry_count, max_retries, progress,
                    total_items, processed_items, owner_id
                ) VALUES ($1, $2, $3, $4, $5, 'pending', 0, 3, 0, 0, 0, $6)
            """,
                task_id,
                binding_id,
                page_id,
                task_type,
                priority,
                owner_id,  # ACL: owner_id for access control
            )

    async def update_confluence_sync_task(
        self,
        task_id: str,
        updates: Dict[str, Any],
    ) -> None:
        """更新 Confluence 同步任务"""
        if not self._pool or not updates:
            return

        set_clauses = []
        params: List[Any] = []
        param_idx = 1

        allowed_fields = {
            "status", "retry_count", "progress", "total_items",
            "processed_items", "error", "result", "started_at", "completed_at"
        }

        for key, value in updates.items():
            if key in allowed_fields:
                if key == "result" and isinstance(value, dict):
                    set_clauses.append(f"{key} = ${param_idx}")
                    params.append(json.dumps(value))
                else:
                    set_clauses.append(f"{key} = ${param_idx}")
                    params.append(value)
                param_idx += 1

        if not set_clauses:
            return

        set_clauses.append("updated_at = NOW()")
        params.append(task_id)

        query = f"UPDATE confluence_sync_tasks SET {', '.join(set_clauses)} WHERE task_id = ${param_idx}"

        async with self._pool.acquire() as conn:
            await conn.execute(query, *params)

    # =========================================================================
    # Document Confluence 扩展字段
    # =========================================================================

    async def update_document_confluence_fields(
        self,
        document_id: str,
        confluence_page_id: Optional[str] = None,
        confluence_binding_id: Optional[str] = None,
        confluence_version: Optional[int] = None,
        confluence_web_url: Optional[str] = None,
    ) -> None:
        """更新文档的 Confluence 关联字段"""
        if not self._pool:
            return

        updates = ["updated_at = NOW()"]
        params: List[Any] = []
        param_idx = 1

        if confluence_page_id is not None:
            updates.append(f"confluence_page_id = ${param_idx}")
            params.append(confluence_page_id)
            param_idx += 1

        if confluence_binding_id is not None:
            updates.append(f"confluence_binding_id = ${param_idx}")
            params.append(confluence_binding_id)
            param_idx += 1

        if confluence_version is not None:
            updates.append(f"confluence_version = ${param_idx}")
            params.append(confluence_version)
            param_idx += 1

        if confluence_web_url is not None:
            updates.append(f"confluence_web_url = ${param_idx}")
            params.append(confluence_web_url)
            param_idx += 1

        if len(updates) == 1:  # 只有 updated_at
            return

        params.append(document_id)
        query = f"UPDATE documents SET {', '.join(updates)} WHERE document_id = ${param_idx}"

        async with self._pool.acquire() as conn:
            await conn.execute(query, *params)

    async def get_documents_by_confluence_binding(
        self,
        binding_id: str,
        limit: int = 100,
        offset: int = 0,
    ) -> List[Dict[str, Any]]:
        """获取 Confluence 绑定关联的文档"""
        if not self._pool:
            return []
        async with self._pool.acquire() as conn:
            rows = await conn.fetch("""
                SELECT * FROM documents
                WHERE confluence_binding_id = $1
                ORDER BY created_at DESC
                LIMIT $2 OFFSET $3
            """, binding_id, limit, offset)
            return [self._row_to_dict(row) for row in rows]

    # =========================================================================
    # 用户认证增强方法 (Account Management)
    # =========================================================================

    async def get_user_by_email(self, email: str) -> Optional[Dict[str, Any]]:
        """通过邮箱获取用户"""
        if not self._pool:
            return None
        async with self._pool.acquire() as conn:
            row = await conn.fetchrow(
                "SELECT * FROM users WHERE LOWER(email) = LOWER($1)", email
            )
            return self._row_to_dict(row) if row else None

    async def save_user_with_password(self, user: Dict[str, Any]) -> None:
        """保存或更新用户（包含密码字段）"""
        if not self._pool:
            return
        async with self._pool.acquire() as conn:
            await conn.execute("""
                INSERT INTO users (
                    user_id, username, email, display_name, department, tenant_id,
                    tier, roles, permissions, quota_config, status,
                    password_hash, force_password_change, email_verified,
                    created_by, metadata
                ) VALUES ($1, $2, $3, $4, $5, $6, $7, $8, $9, $10, $11, $12, $13, $14, $15, $16)
                ON CONFLICT (user_id) DO UPDATE SET
                    username = EXCLUDED.username,
                    email = EXCLUDED.email,
                    display_name = EXCLUDED.display_name,
                    department = EXCLUDED.department,
                    tier = EXCLUDED.tier,
                    roles = EXCLUDED.roles,
                    permissions = EXCLUDED.permissions,
                    quota_config = EXCLUDED.quota_config,
                    status = EXCLUDED.status,
                    updated_at = NOW()
            """,
                user.get("user_id"),
                user.get("username") or user.get("email", "").split("@")[0],
                user.get("email"),
                user.get("display_name"),
                user.get("department"),
                user.get("tenant_id", "default"),
                user.get("tier", "normal"),
                user.get("roles", ["user"]),
                user.get("permissions", []),
                json.dumps(user.get("quota_config", {})),
                user.get("status", "active"),
                user.get("password_hash"),
                user.get("force_password_change", True),
                user.get("email_verified", False),
                user.get("created_by"),
                json.dumps(user.get("metadata", {})),
            )

    async def update_user_password(self, user_id: str, password_hash: str) -> None:
        """更新用户密码并清除强制修改标记"""
        if not self._pool:
            return
        async with self._pool.acquire() as conn:
            await conn.execute("""
                UPDATE users SET
                    password_hash = $1,
                    force_password_change = FALSE,
                    password_changed_at = NOW(),
                    updated_at = NOW()
                WHERE user_id = $2
            """, password_hash, user_id)

    async def reset_user_password(self, user_id: str, password_hash: str) -> None:
        """重置用户密码为默认值，需强制修改"""
        if not self._pool:
            return
        async with self._pool.acquire() as conn:
            await conn.execute("""
                UPDATE users SET
                    password_hash = $1,
                    force_password_change = TRUE,
                    password_changed_at = NULL,
                    login_attempts = 0,
                    locked_until = NULL,
                    updated_at = NOW()
                WHERE user_id = $2
            """, password_hash, user_id)

    async def increment_login_attempts(self, user_id: str) -> None:
        """增加登录失败计数"""
        if not self._pool:
            return
        async with self._pool.acquire() as conn:
            await conn.execute(
                "UPDATE users SET login_attempts = COALESCE(login_attempts, 0) + 1 WHERE user_id = $1",
                user_id
            )

    async def reset_login_attempts(self, user_id: str) -> None:
        """重置登录失败计数"""
        if not self._pool:
            return
        async with self._pool.acquire() as conn:
            await conn.execute(
                "UPDATE users SET login_attempts = 0, locked_until = NULL WHERE user_id = $1",
                user_id
            )

    async def lock_user_account(self, user_id: str, minutes: int = 30) -> None:
        """锁定用户账户"""
        if not self._pool:
            return
        async with self._pool.acquire() as conn:
            await conn.execute("""
                UPDATE users SET
                    locked_until = NOW() + INTERVAL '%s minutes',
                    updated_at = NOW()
                WHERE user_id = $1
            """ % minutes, user_id)

    async def update_last_login(self, user_id: str, ip_address: str) -> None:
        """更新最后登录信息"""
        if not self._pool:
            return
        async with self._pool.acquire() as conn:
            await conn.execute("""
                UPDATE users SET
                    last_login_at = NOW(),
                    last_login_ip = $1,
                    last_active_at = NOW()
                WHERE user_id = $2
            """, ip_address, user_id)

    async def log_login_audit(
        self,
        user_id: Optional[str],
        email: Optional[str],
        action: str,
        ip_address: str,
        user_agent: str,
        details: Dict[str, Any]
    ) -> None:
        """记录登录审计日志"""
        if not self._pool:
            return
        async with self._pool.acquire() as conn:
            await conn.execute("""
                INSERT INTO login_audit (user_id, email, action, ip_address, user_agent, details)
                VALUES ($1, $2, $3, $4, $5, $6)
            """, user_id, email, action, ip_address, user_agent, json.dumps(details))

    async def get_user_permissions(self, user_id: str) -> List[str]:
        """获取用户的所有权限（角色权限 + 额外权限）"""
        if not self._pool:
            return []
        cached = await self._get_cached_permissions(user_id)
        if cached is not None:
            return cached
        async with self._pool.acquire() as conn:
            permissions_set: set = set()

            # 1. 从 role_permissions 表获取角色权限
            try:
                rows = await conn.fetch("""
                    SELECT DISTINCT rp.permission_code
                    FROM user_roles ur
                    JOIN role_permissions rp ON ur.role_name = rp.role_name
                    WHERE ur.user_id = $1
                      AND (ur.expires_at IS NULL OR ur.expires_at > NOW())
                """, user_id)
                for row in rows:
                    permissions_set.add(row['permission_code'])
            except Exception:
                pass

            # 2. 如果 role_permissions 为空，回退到 rbac_roles 的 permissions 数组
            if not permissions_set:
                try:
                    rows = await conn.fetch("""
                        SELECT DISTINCT unnest(rr.permissions) as permission_code
                        FROM user_roles ur
                        JOIN rbac_roles rr ON ur.role_name = rr.role_name
                        WHERE ur.user_id = $1
                          AND (ur.expires_at IS NULL OR ur.expires_at > NOW())
                    """, user_id)
                    for row in rows:
                        permissions_set.add(row['permission_code'])
                except Exception:
                    pass

            # 3. 回退到用户自身的 permissions 字段/roles 映射
            if not permissions_set:
                user_row = await conn.fetchrow(
                    "SELECT roles, permissions FROM users WHERE user_id = $1", user_id
                )
                if user_row:
                    permissions_set.update(user_row["permissions"] or [])
                    roles = user_row["roles"] or []
                    if roles:
                        try:
                            role_rows = await conn.fetch(
                                "SELECT permissions FROM rbac_roles WHERE role_name = ANY($1)",
                                roles,
                            )
                            for role_row in role_rows:
                                role_perms = role_row["permissions"] or []
                                permissions_set.update(role_perms)
                        except Exception:
                            pass

            # 4. 获取用户额外权限（直接分配）
            try:
                extra_rows = await conn.fetch("""
                    SELECT permission_code
                    FROM user_permissions
                    WHERE user_id = $1
                      AND (expires_at IS NULL OR expires_at > NOW())
                """, user_id)
                for row in extra_rows:
                    permissions_set.add(row['permission_code'])
            except Exception:
                # user_permissions 表可能不存在
                pass

            permissions = list(permissions_set)
            await self._set_cached_permissions(user_id, permissions)
            return permissions

    async def list_users_paginated(
        self,
        status: Optional[str] = None,
        search: Optional[str] = None,
        tenant_id: Optional[str] = None,
        limit: int = 20,
        offset: int = 0
    ) -> tuple:
        """分页获取用户列表"""
        if not self._pool:
            return [], 0

        query = "SELECT * FROM users WHERE 1=1"
        count_query = "SELECT COUNT(*) FROM users WHERE 1=1"
        params = []
        param_idx = 1

        if status:
            query += f" AND status = ${param_idx}"
            count_query += f" AND status = ${param_idx}"
            params.append(status)
            param_idx += 1

        if tenant_id:
            query += f" AND tenant_id = ${param_idx}"
            count_query += f" AND tenant_id = ${param_idx}"
            params.append(tenant_id)
            param_idx += 1

        if search:
            query += f" AND (email ILIKE ${param_idx} OR display_name ILIKE ${param_idx} OR username ILIKE ${param_idx})"
            count_query += f" AND (email ILIKE ${param_idx} OR display_name ILIKE ${param_idx} OR username ILIKE ${param_idx})"
            params.append(f"%{search}%")
            param_idx += 1

        async with self._pool.acquire() as conn:
            total = await conn.fetchval(count_query, *params)

            query += f" ORDER BY created_at DESC LIMIT ${param_idx} OFFSET ${param_idx + 1}"
            params.extend([limit, offset])

            rows = await conn.fetch(query, *params)
            users = [self._row_to_dict(row) for row in rows]

            return users, total or 0

    async def update_user(self, user_id: str, updates: Dict[str, Any]) -> None:
        """更新用户字段"""
        if not self._pool or not updates:
            return

        allowed_fields = {
            "display_name", "username", "department", "tier", "roles", "permissions",
            "quota_config", "status", "metadata", "email_verified"
        }
        filtered = {k: v for k, v in updates.items() if k in allowed_fields}
        if not filtered:
            return

        set_clauses = ["updated_at = NOW()"]
        params = []
        param_idx = 1

        for key, value in filtered.items():
            if key in ("quota_config", "metadata") and isinstance(value, dict):
                set_clauses.append(f"{key} = ${param_idx}")
                params.append(json.dumps(value))
            else:
                set_clauses.append(f"{key} = ${param_idx}")
                params.append(value)
            param_idx += 1

        params.append(user_id)
        query = f"UPDATE users SET {', '.join(set_clauses)} WHERE user_id = ${param_idx}"

        async with self._pool.acquire() as conn:
            await conn.execute(query, *params)

    async def delete_user(self, user_id: str) -> bool:
        """删除用户"""
        if not self._pool:
            return False
        async with self._pool.acquire() as conn:
            # 先删除 user_roles
            await conn.execute("DELETE FROM user_roles WHERE user_id = $1", user_id)
            # 再删除用户
            result = await conn.execute("DELETE FROM users WHERE user_id = $1", user_id)
            return result == "DELETE 1"

    async def assign_user_role(
        self, user_id: str, role_name: str, granted_by: str
    ) -> None:
        """为用户分配角色"""
        if not self._pool:
            return
        async with self._pool.acquire() as conn:
            await conn.execute("""
                INSERT INTO user_roles (user_id, role_name, granted_by)
                VALUES ($1, $2, $3)
                ON CONFLICT (user_id, role_name) DO UPDATE SET
                    granted_at = NOW(),
                    granted_by = EXCLUDED.granted_by
            """, user_id, role_name, granted_by)
        await self._invalidate_permission_cache(user_id)

    async def remove_user_role(self, user_id: str, role_name: str) -> bool:
        """移除用户角色"""
        if not self._pool:
            return False
        async with self._pool.acquire() as conn:
            result = await conn.execute(
                "DELETE FROM user_roles WHERE user_id = $1 AND role_name = $2",
                user_id, role_name
            )
        await self._invalidate_permission_cache(user_id)
        return result == "DELETE 1"

    async def update_user_roles(
        self, user_id: str, roles: List[str], granted_by: str
    ) -> None:
        """更新用户的所有角色"""
        if not self._pool:
            return
        async with self._pool.acquire() as conn:
            # 先删除现有角色
            await conn.execute("DELETE FROM user_roles WHERE user_id = $1", user_id)
            # 插入新角色
            for role in roles:
                await conn.execute("""
                    INSERT INTO user_roles (user_id, role_name, granted_by)
                    VALUES ($1, $2, $3)
                """, user_id, role, granted_by)
            # 同时更新 users 表的 roles 字段
            await conn.execute(
                "UPDATE users SET roles = $1, updated_at = NOW() WHERE user_id = $2",
                roles, user_id
            )
        await self._invalidate_permission_cache(user_id)

    async def get_user_roles(self, user_id: str) -> List[str]:
        """获取用户的所有角色"""
        if not self._pool:
            return []
        async with self._pool.acquire() as conn:
            rows = await conn.fetch("""
                SELECT role_name FROM user_roles
                WHERE user_id = $1
                  AND (expires_at IS NULL OR expires_at > NOW())
                ORDER BY granted_at
            """, user_id)
            return [row['role_name'] for row in rows]

    # =========================================================================
    # 角色和权限管理方法
    # =========================================================================

    async def list_roles(self) -> List[Dict[str, Any]]:
        """获取所有角色"""
        if not self._pool:
            return []
        async with self._pool.acquire() as conn:
            rows = await conn.fetch(
                "SELECT * FROM rbac_roles ORDER BY is_system DESC, role_name"
            )
            return [self._row_to_dict(row) for row in rows]

    async def get_role(self, role_name: str) -> Optional[Dict[str, Any]]:
        """获取角色详情"""
        if not self._pool:
            return None
        async with self._pool.acquire() as conn:
            row = await conn.fetchrow(
                "SELECT * FROM rbac_roles WHERE role_name = $1", role_name
            )
            return self._row_to_dict(row) if row else None

    async def create_role(
        self, role_name: str, description: Optional[str], permissions: List[str]
    ) -> None:
        """创建新角色"""
        if not self._pool:
            return
        async with self._pool.acquire() as conn:
            await conn.execute("""
                INSERT INTO rbac_roles (role_name, description, permissions, is_system)
                VALUES ($1, $2, $3, FALSE)
            """, role_name, description, permissions)

            # 同时插入 role_permissions
            for perm in permissions:
                await conn.execute("""
                    INSERT INTO role_permissions (role_name, permission_code)
                    VALUES ($1, $2)
                    ON CONFLICT DO NOTHING
                """, role_name, perm)
        await self._invalidate_permission_cache()

    async def update_role(
        self, role_name: str, description: Optional[str], permissions: Optional[List[str]]
    ) -> None:
        """更新角色"""
        if not self._pool:
            return
        async with self._pool.acquire() as conn:
            if description is not None and permissions is not None:
                await conn.execute("""
                    UPDATE rbac_roles SET
                        description = $1,
                        permissions = $2,
                        updated_at = NOW()
                    WHERE role_name = $3
                """, description, permissions, role_name)
            elif description is not None:
                await conn.execute("""
                    UPDATE rbac_roles SET
                        description = $1,
                        updated_at = NOW()
                    WHERE role_name = $2
                """, description, role_name)
            elif permissions is not None:
                await conn.execute("""
                    UPDATE rbac_roles SET
                        permissions = $1,
                        updated_at = NOW()
                    WHERE role_name = $2
                """, permissions, role_name)

            # 如果更新了权限，同步 role_permissions 表
            if permissions is not None:
                await conn.execute(
                    "DELETE FROM role_permissions WHERE role_name = $1", role_name
                )
                for perm in permissions:
                    await conn.execute("""
                        INSERT INTO role_permissions (role_name, permission_code)
                        VALUES ($1, $2)
                        ON CONFLICT DO NOTHING
                    """, role_name, perm)
        await self._invalidate_permission_cache()

    async def delete_role(self, role_name: str) -> bool:
        """删除角色"""
        if not self._pool:
            return False
        async with self._pool.acquire() as conn:
            # 先删除 role_permissions
            await conn.execute(
                "DELETE FROM role_permissions WHERE role_name = $1", role_name
            )
            # 删除 user_roles 中的引用
            await conn.execute(
                "DELETE FROM user_roles WHERE role_name = $1", role_name
            )
            # 删除角色
            result = await conn.execute(
                "DELETE FROM rbac_roles WHERE role_name = $1 AND is_system = FALSE",
                role_name
            )
            await self._invalidate_permission_cache()
            return result == "DELETE 1"

    async def list_permissions(self, category: Optional[str] = None) -> List[Dict[str, Any]]:
        """获取所有权限定义"""
        if not self._pool:
            return []
        async with self._pool.acquire() as conn:
            if category:
                rows = await conn.fetch(
                    "SELECT * FROM permissions WHERE category = $1 ORDER BY permission_code",
                    category
                )
            else:
                rows = await conn.fetch(
                    "SELECT * FROM permissions ORDER BY category, permission_code"
                )
            return [self._row_to_dict(row) for row in rows]

    async def get_role_user_count(self, role_name: str) -> int:
        """获取角色的用户数量"""
        if not self._pool:
            return 0
        async with self._pool.acquire() as conn:
            count = await conn.fetchval(
                "SELECT COUNT(*) FROM user_roles WHERE role_name = $1",
                role_name
            )
            return count or 0

    async def get_users_by_role(self, role_name: str) -> List[Dict[str, Any]]:
        """获取拥有指定角色的所有用户"""
        if not self._pool:
            return []
        async with self._pool.acquire() as conn:
            rows = await conn.fetch("""
                SELECT u.user_id, u.email, u.display_name, u.status, ur.granted_at
                FROM users u
                JOIN user_roles ur ON u.user_id = ur.user_id
                WHERE ur.role_name = $1
                ORDER BY ur.granted_at DESC
            """, role_name)
            return [self._row_to_dict(row) for row in rows]

    # =========================================================================
    # 用户额外权限管理 (User Extra Permissions)
    # =========================================================================

    async def get_user_extra_permissions(self, user_id: str) -> List[Dict[str, Any]]:
        """获取用户的额外权限（直接分配，非角色）"""
        if not self._pool:
            return []
        async with self._pool.acquire() as conn:
            try:
                rows = await conn.fetch("""
                    SELECT permission_code, granted_by, granted_at, expires_at, note
                    FROM user_permissions
                    WHERE user_id = $1
                      AND (expires_at IS NULL OR expires_at > NOW())
                    ORDER BY granted_at DESC
                """, user_id)
                return [self._row_to_dict(row) for row in rows]
            except Exception:
                # Table might not exist yet
                return []

    async def add_user_extra_permission(
        self,
        user_id: str,
        permission_code: str,
        granted_by: str,
        note: Optional[str] = None,
        expires_at: Optional[datetime] = None
    ) -> bool:
        """给用户添加额外权限"""
        if not self._pool:
            return False
        await self._invalidate_permission_cache(user_id)
        async with self._pool.acquire() as conn:
            try:
                await conn.execute("""
                    INSERT INTO user_permissions (user_id, permission_code, granted_by, note, expires_at)
                    VALUES ($1, $2, $3, $4, $5)
                    ON CONFLICT (user_id, permission_code) DO UPDATE SET
                        granted_by = EXCLUDED.granted_by,
                        granted_at = NOW(),
                        note = EXCLUDED.note,
                        expires_at = EXCLUDED.expires_at
                """, user_id, permission_code, granted_by, note, expires_at)
                return True
            except Exception:
                return False

    async def remove_user_extra_permission(self, user_id: str, permission_code: str) -> bool:
        """移除用户的额外权限"""
        if not self._pool:
            return False
        await self._invalidate_permission_cache(user_id)
        async with self._pool.acquire() as conn:
            try:
                result = await conn.execute("""
                    DELETE FROM user_permissions
                    WHERE user_id = $1 AND permission_code = $2
                """, user_id, permission_code)
                return "DELETE" in result
            except Exception:
                return False

    async def update_user_extra_permissions(
        self,
        user_id: str,
        permissions: List[str],
        granted_by: str
    ) -> None:
        """更新用户的额外权限（替换所有）"""
        if not self._pool:
            return
        await self._invalidate_permission_cache(user_id)
        async with self._pool.acquire() as conn:
            async with conn.transaction():
                # 删除现有的额外权限
                await conn.execute(
                    "DELETE FROM user_permissions WHERE user_id = $1",
                    user_id
                )
                # 添加新的额外权限
                for perm in permissions:
                    await conn.execute("""
                        INSERT INTO user_permissions (user_id, permission_code, granted_by)
                        VALUES ($1, $2, $3)
                    """, user_id, perm, granted_by)

    # =========================================================================
    # 辅助方法
    # =========================================================================

    def _row_to_dict(self, row) -> Dict[str, Any]:
        """将数据库行转换为字典，正确处理 JSON 和 datetime 字段"""
        if not row:
            return {}
        result = dict(row)

        # JSON 字段列表 - 需要解析为 Python 对象（字典类型）
        json_dict_fields = {
            "metadata", "embedding_config", "index_config", "result", "config"
        }

        # JSON 字段列表 - 需要解析为 Python 对象（列表类型）
        json_list_fields = {
            "roles", "keywords", "include_patterns", "exclude_patterns",
            "labels", "events", "history"
        }

        for key, value in result.items():
            # 处理 datetime 类型
            if isinstance(value, datetime):
                result[key] = value.isoformat()
            # 处理 JSON 字典字段
            elif key in json_dict_fields and value is not None:
                if isinstance(value, str):
                    try:
                        result[key] = json.loads(value)
                    except (json.JSONDecodeError, TypeError):
                        result[key] = {}
                elif not isinstance(value, dict):
                    result[key] = {}
            # 处理 JSON 列表字段
            elif key in json_list_fields and value is not None:
                if isinstance(value, str):
                    try:
                        result[key] = json.loads(value)
                    except (json.JSONDecodeError, TypeError):
                        result[key] = []
                elif not isinstance(value, list):
                    result[key] = []
        return result
