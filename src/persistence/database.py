"""
PostgreSQL 数据库存储层

提供与 database/schema.sql 表结构对应的完整 CRUD 操作
"""
from __future__ import annotations

import json
from datetime import datetime
from typing import Any, Dict, List, Optional

try:
    import asyncpg
    HAS_ASYNCPG = True
except ImportError:
    HAS_ASYNCPG = False
    asyncpg = None


class DatabaseStorage:
    """PostgreSQL 数据库存储"""

    def __init__(self, dsn: Optional[str] = None, enabled: bool = False):
        self.dsn = dsn
        self.enabled = enabled and HAS_ASYNCPG and dsn
        self._pool: Optional[Any] = None

    async def connect(self) -> None:
        """建立数据库连接池"""
        if not self.enabled:
            return
        if not HAS_ASYNCPG:
            raise RuntimeError("asyncpg is not installed. Run: pip install asyncpg")
        self._pool = await asyncpg.create_pool(self.dsn, min_size=2, max_size=10)

    async def close(self) -> None:
        """关闭连接池"""
        if self._pool:
            await self._pool.close()
            self._pool = None

    async def execute_schema(self, schema_path: str) -> None:
        """执行 SQL 建表脚本"""
        if not self._pool:
            return
        with open(schema_path, 'r', encoding='utf-8') as f:
            sql = f.read()
        async with self._pool.acquire() as conn:
            await conn.execute(sql)

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
    # 辅助方法
    # =========================================================================

    def _row_to_dict(self, row) -> Dict[str, Any]:
        """将数据库行转换为字典"""
        if not row:
            return {}
        result = dict(row)
        # 处理 datetime 类型
        for key, value in result.items():
            if isinstance(value, datetime):
                result[key] = value.isoformat()
        return result
