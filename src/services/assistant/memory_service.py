"""
Memory Service for Agent.

Provides structured long-term memory (User Level) and persistent session memory (Session Level).
Implements the 3-layer memory architecture:
1. Working Memory (in-context, handled by AssistantService)
2. Session Memory (persistent key-value for current task/session)
3. User Memory (long-term preferences and facts)
"""

from __future__ import annotations

import json
from datetime import datetime, timezone
from typing import Any, Dict, List, Optional
from uuid import uuid4

from ...core.observability.logging import get_logger
from ...persistence.database import DatabaseStorage

logger = get_logger(__name__)


class MemoryService:
    def __init__(self, database: DatabaseStorage):
        self.database = database

    # =========================================================================
    # User Memory (Long-term)
    # =========================================================================

    async def set_user_memory(
        self,
        user_id: str,
        key: str,
        value: Any,
        tenant_id: str = "default",
        metadata: Optional[Dict[str, Any]] = None,
    ) -> bool:
        """
        Set a user memory key-value pair.
        Upserts if key already exists.
        """
        query = """
            INSERT INTO user_memory (tenant_id, user_id, key, value, metadata, updated_at)
            VALUES ($1, $2, $3, $4, $5, NOW())
            ON CONFLICT (user_id, key)
            DO UPDATE SET
                value = EXCLUDED.value,
                metadata = COALESCE(EXCLUDED.metadata, user_memory.metadata),
                updated_at = NOW();
        """
        try:
            # Ensure value is JSON serializable
            if not isinstance(value, (dict, list, str, int, float, bool, type(None))):
                value = str(value)
            
            # Use json.dumps for the JSONB column if the driver requires string input for JSONB
            # asyncpg usually handles dict/list -> JSONB automatically, but let's be safe if using simple query
            # Assuming DatabaseStorage handles binding correctly.
            
            await self.database.execute(
                query,
                tenant_id,
                user_id,
                key,
                json.dumps(value) if isinstance(value, (dict, list)) else value, # Depending on DB wrapper implementation
                json.dumps(metadata) if metadata else None
            )
            return True
        except Exception as e:
            logger.error(f"Failed to set user memory {user_id}:{key}: {e}")
            return False

    async def get_user_memory(
        self,
        user_id: str,
        key: str,
    ) -> Optional[Any]:
        """
        Get a specific user memory value.
        Updates access count and last_accessed_at.
        """
        # First get the value
        query = """
            SELECT value FROM user_memory
            WHERE user_id = $1 AND key = $2
        """
        result = await self.database.fetch_one(query, user_id, key)
        
        if result:
            # Async update access stats (fire and forget pattern ideally, but here simple await)
            update_query = """
                UPDATE user_memory
                SET access_count = access_count + 1, last_accessed_at = NOW()
                WHERE user_id = $1 AND key = $2
            """
            await self.database.execute(update_query, user_id, key)
            
            value = result.get("value")
            # If the driver returns JSON string, parse it. If it returns dict, use it.
            if isinstance(value, str):
                try:
                    return json.loads(value)
                except:
                    return value
            return value
            
        return None

    async def list_user_memories(
        self,
        user_id: str,
        limit: int = 100,
    ) -> Dict[str, Any]:
        """
        List all memories for a user.
        Returns a dictionary of key-value pairs.
        """
        query = """
            SELECT key, value FROM user_memory
            WHERE user_id = $1
            ORDER BY last_accessed_at DESC NULLS LAST, created_at DESC
            LIMIT $2
        """
        rows = await self.database.fetch_all(query, user_id, limit)
        
        memories = {}
        for row in rows:
            val = row.get("value")
            if isinstance(val, str):
                try:
                    val = json.loads(val)
                except:
                    pass
            memories[row.get("key")] = val
            
        return memories

    async def delete_user_memory(self, user_id: str, key: str) -> bool:
        """Delete a user memory item."""
        query = "DELETE FROM user_memory WHERE user_id = $1 AND key = $2"
        await self.database.execute(query, user_id, key)
        return True

    # =========================================================================
    # Session Memory (Context/Task Level)
    # =========================================================================

    async def set_session_memory(
        self,
        session_id: str,
        key: str,
        value: Any,
        tenant_id: str = "default",
        metadata: Optional[Dict[str, Any]] = None,
    ) -> bool:
        """Set session memory."""
        query = """
            INSERT INTO session_memory (tenant_id, session_id, key, value, metadata, updated_at)
            VALUES ($1, $2, $3, $4, $5, NOW())
            ON CONFLICT (session_id, key)
            DO UPDATE SET
                value = EXCLUDED.value,
                metadata = COALESCE(EXCLUDED.metadata, session_memory.metadata),
                updated_at = NOW();
        """
        try:
            if not isinstance(value, (dict, list, str, int, float, bool, type(None))):
                value = str(value)
                
            await self.database.execute(
                query,
                tenant_id,
                session_id,
                key,
                json.dumps(value) if isinstance(value, (dict, list)) else value,
                json.dumps(metadata) if metadata else None
            )
            return True
        except Exception as e:
            logger.error(f"Failed to set session memory {session_id}:{key}: {e}")
            return False

    async def get_session_memory(self, session_id: str, key: str) -> Optional[Any]:
        """Get session memory value."""
        query = "SELECT value FROM session_memory WHERE session_id = $1 AND key = $2"
        result = await self.database.fetch_one(query, session_id, key)
        
        if result:
            value = result.get("value")
            if isinstance(value, str):
                try:
                    return json.loads(value)
                except:
                    return value
            return value
        return None

    async def list_session_memories(self, session_id: str) -> Dict[str, Any]:
        """List all session memories."""
        query = "SELECT key, value FROM session_memory WHERE session_id = $1"
        rows = await self.database.fetch_all(query, session_id)
        
        memories = {}
        for row in rows:
            val = row.get("value")
            if isinstance(val, str):
                try:
                    val = json.loads(val)
                except:
                    pass
            memories[row.get("key")] = val
            
        return memories

    async def delete_session_memory(self, session_id: str, key: str) -> bool:
        """Delete session memory item."""
        query = "DELETE FROM session_memory WHERE session_id = $1 AND key = $2"
        await self.database.execute(query, session_id, key)
        return True
