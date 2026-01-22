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
        tenant_id: str,
        user_id: str,
        key: str,
        value: Any,
        metadata: Optional[Dict[str, Any]] = None,
    ) -> bool:
        """
        Set a user memory key-value pair.
        Upserts if key already exists.

        Args:
            tenant_id: Tenant ID for multi-tenant isolation (required)
            user_id: User ID
            key: Memory key
            value: Value to store
            metadata: Optional metadata
        """
        query = """
            INSERT INTO user_memory (tenant_id, user_id, key, value, metadata, updated_at)
            VALUES ($1, $2, $3, $4, $5, NOW())
            ON CONFLICT (tenant_id, user_id, key)
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
        tenant_id: str,
        user_id: str,
        key: str,
    ) -> Optional[Any]:
        """
        Get a specific user memory value.
        Updates access count and last_accessed_at.

        Args:
            tenant_id: Tenant ID for multi-tenant isolation (required)
            user_id: User ID
            key: Memory key
        """
        # First get the value with tenant isolation
        query = """
            SELECT value FROM user_memory
            WHERE tenant_id = $1 AND user_id = $2 AND key = $3
        """
        result = await self.database.fetchrow(query, tenant_id, user_id, key)

        if result:
            # Async update access stats (fire and forget pattern ideally, but here simple await)
            update_query = """
                UPDATE user_memory
                SET access_count = access_count + 1, last_accessed_at = NOW()
                WHERE tenant_id = $1 AND user_id = $2 AND key = $3
            """
            await self.database.execute(update_query, tenant_id, user_id, key)
            
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
        tenant_id: str,
        user_id: str,
        limit: int = 100,
    ) -> Dict[str, Any]:
        """
        List all memories for a user.
        Returns a dictionary of key-value pairs.

        Args:
            tenant_id: Tenant ID for multi-tenant isolation (required)
            user_id: User ID
            limit: Maximum number of memories to return
        """
        query = """
            SELECT key, value FROM user_memory
            WHERE tenant_id = $1 AND user_id = $2
            ORDER BY last_accessed_at DESC NULLS LAST, created_at DESC
            LIMIT $3
        """
        rows = await self.database.fetch(query, tenant_id, user_id, limit)
        
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

    async def delete_user_memory(self, tenant_id: str, user_id: str, key: str) -> bool:
        """
        Delete a user memory item.

        Args:
            tenant_id: Tenant ID for multi-tenant isolation (required)
            user_id: User ID
            key: Memory key
        """
        query = "DELETE FROM user_memory WHERE tenant_id = $1 AND user_id = $2 AND key = $3"
        await self.database.execute(query, tenant_id, user_id, key)
        return True

    # =========================================================================
    # Session Memory (Context/Task Level)
    # =========================================================================

    async def set_session_memory(
        self,
        tenant_id: str,
        session_id: str,
        key: str,
        value: Any,
        metadata: Optional[Dict[str, Any]] = None,
    ) -> bool:
        """
        Set session memory.

        Args:
            tenant_id: Tenant ID for multi-tenant isolation (required)
            session_id: Session ID
            key: Memory key
            value: Value to store
            metadata: Optional metadata
        """
        query = """
            INSERT INTO session_memory (tenant_id, session_id, key, value, metadata, updated_at)
            VALUES ($1, $2, $3, $4, $5, NOW())
            ON CONFLICT (tenant_id, session_id, key)
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

    async def get_session_memory(self, tenant_id: str, session_id: str, key: str) -> Optional[Any]:
        """
        Get session memory value.

        Args:
            tenant_id: Tenant ID for multi-tenant isolation (required)
            session_id: Session ID
            key: Memory key
        """
        query = "SELECT value FROM session_memory WHERE tenant_id = $1 AND session_id = $2 AND key = $3"
        result = await self.database.fetchrow(query, tenant_id, session_id, key)
        
        if result:
            value = result.get("value")
            if isinstance(value, str):
                try:
                    return json.loads(value)
                except:
                    return value
            return value
        return None

    async def list_session_memories(self, tenant_id: str, session_id: str) -> Dict[str, Any]:
        """
        List all session memories.

        Args:
            tenant_id: Tenant ID for multi-tenant isolation (required)
            session_id: Session ID
        """
        query = "SELECT key, value FROM session_memory WHERE tenant_id = $1 AND session_id = $2"
        rows = await self.database.fetch(query, tenant_id, session_id)
        
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

    async def delete_session_memory(self, tenant_id: str, session_id: str, key: str) -> bool:
        """
        Delete session memory item.

        Args:
            tenant_id: Tenant ID for multi-tenant isolation (required)
            session_id: Session ID
            key: Memory key
        """
        query = "DELETE FROM session_memory WHERE tenant_id = $1 AND session_id = $2 AND key = $3"
        await self.database.execute(query, tenant_id, session_id, key)
        return True

    async def get_session_context(
        self,
        tenant_id: str,
        session_id: str,
    ) -> Optional[Dict[str, Any]]:
        """
        Get the full session context including all session memories.

        Returns a dictionary with:
        - conversation_summary: Previous conversation summary if compressed
        - task_state: Current task state
        - recent_context: Any other relevant session data

        Args:
            tenant_id: Tenant ID for multi-tenant isolation (required)
            session_id: The session ID

        Returns:
            Dictionary of session context, or None if no session data
        """
        # Get all session memories with tenant isolation
        all_memories = await self.list_session_memories(tenant_id, session_id)

        if not all_memories:
            return None

        # Structure the context
        context: Dict[str, Any] = {
            "conversation_summary": all_memories.get("conversation_summary"),
            "task_state": all_memories.get("task_state"),
            "working_memory": all_memories.get("working_memory"),
            "last_response": all_memories.get("last_response"),
            "compressed_context": all_memories.get("compressed_context"),
        }

        # Filter out None values
        return {k: v for k, v in context.items() if v is not None} or None

    async def get_user_preferences(
        self,
        tenant_id: str,
        user_id: str,
    ) -> Dict[str, Any]:
        """
        Get user preferences from long-term memory.

        Returns default preferences if none are stored.

        Args:
            tenant_id: Tenant ID for multi-tenant isolation (required)
            user_id: The user ID

        Returns:
            Dictionary of user preferences
        """
        DEFAULT_PREFERENCES = {
            "language": "zh-CN",
            "response_style": "professional",
            "preferred_tools": [],
            "default_datasets": [],
        }

        prefs = await self.get_user_memory(tenant_id=tenant_id, user_id=user_id, key="preferences")
        if prefs is None:
            return DEFAULT_PREFERENCES.copy()

        # Merge with defaults to ensure all keys exist
        merged = DEFAULT_PREFERENCES.copy()
        if isinstance(prefs, dict):
            merged.update(prefs)
        return merged

    async def get_long_term_context(
        self,
        tenant_id: str,
        user_id: str,
        limit: int = 20,
    ) -> Dict[str, Any]:
        """
        Get user's long-term memory context.

        Includes preferences and frequently accessed memories.

        Args:
            tenant_id: Tenant ID for multi-tenant isolation (required)
            user_id: The user ID
            limit: Maximum number of memories to retrieve

        Returns:
            Dictionary with preferences and learned patterns
        """
        # Get preferences with tenant isolation
        preferences = await self.get_user_preferences(tenant_id, user_id)

        # Get frequently accessed memories with tenant isolation
        query = """
            SELECT key, value, metadata, access_count
            FROM user_memory
            WHERE tenant_id = $1 AND user_id = $2
            ORDER BY access_count DESC, last_accessed_at DESC NULLS LAST
            LIMIT $3
        """
        rows = await self.database.fetch(query, tenant_id, user_id, limit)

        frequent_memories = []
        for row in rows:
            key = row.get("key")
            if key == "preferences":
                continue  # Already included

            val = row.get("value")
            if isinstance(val, str):
                try:
                    val = json.loads(val)
                except Exception:
                    pass

            frequent_memories.append({
                "key": key,
                "value": val,
                "access_count": row.get("access_count", 0),
            })

        return {
            "preferences": preferences,
            "frequent_memories": frequent_memories[:10],  # Top 10
        }
