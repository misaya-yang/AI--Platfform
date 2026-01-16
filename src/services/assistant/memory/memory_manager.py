"""
Three-Layer Memory System for Enterprise AI Assistant.

This module implements a hierarchical memory architecture with three distinct layers:
1. WorkingMemoryLayer - In-memory, fastest, for current session task state
2. SessionMemoryLayer - Database-backed, persists across reconnections
3. LongTermMemoryLayer - User-level persistent memory for preferences and patterns

The MemoryManager provides a unified interface for storing and retrieving
information across all layers, with automatic layer ordering for recall operations.

Architecture:
    +-------------------+
    | Working Memory    |  <- Fastest, in-memory, cleared on session end
    +-------------------+
    | Session Memory    |  <- Database-backed, cleared on session delete
    +-------------------+
    | Long-term Memory  |  <- User-level, persistent across sessions
    +-------------------+

Usage:
    ```python
    manager = MemoryManager(db=database, session_id="s1", user_id="u1")

    # Store in working memory (default)
    await manager.remember("current_task", {"step": 1, "status": "processing"})

    # Store in session memory
    await manager.remember("search_results", results, layer="session")

    # Store in long-term memory
    await manager.remember("preferred_format", "markdown", layer="long_term")

    # Recall checks layers in order: working -> session -> long_term
    value = await manager.recall("current_task")

    # Search across all layers
    results = await manager.search_all("task")
    ```
"""

from __future__ import annotations

import json
from abc import ABC, abstractmethod
from datetime import datetime
from typing import Any, Dict, List, Optional, Protocol, runtime_checkable


@runtime_checkable
class MemoryDatabase(Protocol):
    """
    Protocol defining the database interface for memory operations.

    This protocol defines the methods that will be implemented in Task 3.4
    for session and user memory persistence.

    Note: The database implementation will handle tenant_id internally by
    looking up the tenant from session_id or user_id. The caller does not
    need to provide tenant_id explicitly.
    """

    async def store_session_memory(
        self, session_id: str, key: str, value: Any, metadata: Optional[dict]
    ) -> None: ...

    async def get_session_memory(
        self, session_id: str, key: str
    ) -> Optional[Any]: ...

    async def search_session_memory(
        self, session_id: str, query: str, limit: int
    ) -> List[Dict[str, Any]]: ...

    async def delete_session_memory(
        self, session_id: str, key: str
    ) -> bool: ...

    async def clear_session_memory(self, session_id: str) -> None: ...

    async def store_user_memory(
        self, user_id: str, key: str, value: Any, metadata: Optional[dict]
    ) -> None: ...

    async def get_user_memory(
        self, user_id: str, key: str
    ) -> Optional[Any]: ...

    async def search_user_memory(
        self, user_id: str, query: str, limit: int
    ) -> List[Dict[str, Any]]: ...

    async def delete_user_memory(
        self, user_id: str, key: str
    ) -> bool: ...

    async def get_frequently_accessed_user_memory(
        self, user_id: str, limit: int
    ) -> List[Dict[str, Any]]: ...


class MemoryLayer(ABC):
    """
    Abstract base class for memory layers.

    Each memory layer implements storage, retrieval, and search operations
    with different persistence characteristics and performance profiles.
    """

    @abstractmethod
    async def store(self, key: str, value: Any, metadata: Optional[dict] = None) -> None:
        """
        Store a value with the given key.

        Args:
            key: Unique identifier for the stored value
            value: The value to store (any JSON-serializable type)
            metadata: Optional metadata about the stored value
        """
        pass

    @abstractmethod
    async def retrieve(self, key: str) -> Optional[Any]:
        """
        Retrieve a value by key.

        Args:
            key: The key to look up

        Returns:
            The stored value if found, None otherwise
        """
        pass

    @abstractmethod
    async def search(self, query: str, limit: int = 10) -> List[Dict[str, Any]]:
        """
        Search for entries matching the query.

        Args:
            query: Search query string
            limit: Maximum number of results to return

        Returns:
            List of matching entries with keys, values, and metadata
        """
        pass


class WorkingMemoryLayer(MemoryLayer):
    """
    In-memory storage for current session task state.

    This is the fastest memory layer, storing data entirely in memory.
    Data is cleared when the session ends or when explicitly cleared.

    Attributes:
        _storage: Internal dictionary storing key-value pairs
        _metadata: Internal dictionary storing metadata for each key
    """

    def __init__(self) -> None:
        """Initialize an empty working memory layer."""
        self._storage: Dict[str, Any] = {}
        self._metadata: Dict[str, dict] = {}
        self._timestamps: Dict[str, datetime] = {}

    async def store(self, key: str, value: Any, metadata: Optional[dict] = None) -> None:
        """
        Store a value in working memory.

        Args:
            key: Unique identifier for the stored value
            value: The value to store
            metadata: Optional metadata about the stored value
        """
        self._storage[key] = value
        self._metadata[key] = metadata or {}
        self._timestamps[key] = datetime.now()

    async def retrieve(self, key: str) -> Optional[Any]:
        """
        Retrieve a value from working memory.

        Args:
            key: The key to look up

        Returns:
            The stored value if found, None otherwise
        """
        return self._storage.get(key)

    async def search(self, query: str, limit: int = 10) -> List[Dict[str, Any]]:
        """
        Search working memory using simple keyword matching.

        Matches are found if the query appears in the key or in the
        string representation of the value.

        Args:
            query: Search query string
            limit: Maximum number of results to return

        Returns:
            List of matching entries with key, value, and metadata
        """
        results: List[Dict[str, Any]] = []
        query_lower = query.lower()

        for key, value in self._storage.items():
            # Convert value to string for matching
            value_str = json.dumps(value) if not isinstance(value, str) else value

            # Check if query matches key or value (not both to avoid duplicates)
            if query_lower in key.lower():
                results.append({
                    "key": key,
                    "value": value,
                    "metadata": self._metadata.get(key, {}),
                    "timestamp": self._timestamps.get(key),
                })
            elif query_lower in value_str.lower():
                results.append({
                    "key": key,
                    "value": value,
                    "metadata": self._metadata.get(key, {}),
                    "timestamp": self._timestamps.get(key),
                })

            # Check limit after both conditions
            if len(results) >= limit:
                break

        return results

    def clear(self) -> None:
        """Clear all working memory data."""
        self._storage.clear()
        self._metadata.clear()
        self._timestamps.clear()

    def keys(self) -> List[str]:
        """Return all keys in working memory."""
        return list(self._storage.keys())

    def __len__(self) -> int:
        """Return the number of items in working memory."""
        return len(self._storage)


class SessionMemoryLayer(MemoryLayer):
    """
    Database-backed memory for session-specific data.

    This layer persists data across reconnections within the same session.
    Data is cleared when the session is deleted.

    Attributes:
        db: Database instance for persistence
        session_id: The session ID this memory belongs to
    """

    def __init__(self, db: MemoryDatabase, session_id: str) -> None:
        """
        Initialize session memory layer.

        Args:
            db: Database instance for persistence
            session_id: The session ID this memory belongs to
        """
        self.db = db
        self.session_id = session_id

    async def store(self, key: str, value: Any, metadata: Optional[dict] = None) -> None:
        """
        Store a value in session memory (database-backed).

        Args:
            key: Unique identifier for the stored value
            value: The value to store (will be JSON-serialized)
            metadata: Optional metadata about the stored value
        """
        await self.db.store_session_memory(
            session_id=self.session_id,
            key=key,
            value=value,
            metadata=metadata,
        )

    async def retrieve(self, key: str) -> Optional[Any]:
        """
        Retrieve a value from session memory.

        Args:
            key: The key to look up

        Returns:
            The stored value if found, None otherwise
        """
        return await self.db.get_session_memory(
            session_id=self.session_id,
            key=key,
        )

    async def search(self, query: str, limit: int = 10) -> List[Dict[str, Any]]:
        """
        Search session memory.

        Args:
            query: Search query string
            limit: Maximum number of results to return

        Returns:
            List of matching entries with key, value, and metadata
        """
        return await self.db.search_session_memory(
            session_id=self.session_id,
            query=query,
            limit=limit,
        )

    async def delete(self, key: str) -> bool:
        """
        Delete a specific key from session memory.

        Args:
            key: The key to delete

        Returns:
            True if deleted, False if key not found
        """
        return await self.db.delete_session_memory(
            session_id=self.session_id,
            key=key,
        )

    async def clear(self) -> None:
        """Clear all session memory for this session."""
        await self.db.clear_session_memory(session_id=self.session_id)


class LongTermMemoryLayer(MemoryLayer):
    """
    User-level persistent memory for preferences and patterns.

    This layer stores information that persists across sessions,
    including user preferences, learned patterns, and frequently
    used resources.

    Attributes:
        db: Database instance for persistence
        user_id: The user ID this memory belongs to
    """

    # Default user preferences
    DEFAULT_PREFERENCES: Dict[str, Any] = {
        "language": "zh-CN",
        "response_style": "professional",
        "preferred_tools": [],
        "default_datasets": [],
    }

    def __init__(self, db: MemoryDatabase, user_id: str) -> None:
        """
        Initialize long-term memory layer.

        Args:
            db: Database instance for persistence
            user_id: The user ID this memory belongs to
        """
        self.db = db
        self.user_id = user_id

    async def store(self, key: str, value: Any, metadata: Optional[dict] = None) -> None:
        """
        Store a value in long-term memory (database-backed).

        Args:
            key: Unique identifier for the stored value
            value: The value to store (will be JSON-serialized)
            metadata: Optional metadata about the stored value
        """
        await self.db.store_user_memory(
            user_id=self.user_id,
            key=key,
            value=value,
            metadata=metadata,
        )

    async def retrieve(self, key: str) -> Optional[Any]:
        """
        Retrieve a value from long-term memory.

        Also increments the access count for the retrieved key.

        Args:
            key: The key to look up

        Returns:
            The stored value if found, None otherwise
        """
        return await self.db.get_user_memory(
            user_id=self.user_id,
            key=key,
        )

    async def search(self, query: str, limit: int = 10) -> List[Dict[str, Any]]:
        """
        Search long-term memory.

        Args:
            query: Search query string
            limit: Maximum number of results to return

        Returns:
            List of matching entries with key, value, and metadata
        """
        return await self.db.search_user_memory(
            user_id=self.user_id,
            query=query,
            limit=limit,
        )

    async def delete(self, key: str) -> bool:
        """
        Delete a specific key from long-term memory.

        Args:
            key: The key to delete

        Returns:
            True if deleted, False if key not found
        """
        return await self.db.delete_user_memory(
            user_id=self.user_id,
            key=key,
        )

    async def get_preferences(self) -> Dict[str, Any]:
        """
        Get user preferences from long-term memory.

        Returns:
            User preferences dictionary, or defaults if not set
        """
        preferences = await self.retrieve("user_preferences")
        if preferences is None:
            return self.DEFAULT_PREFERENCES.copy()
        # Merge with defaults to ensure all keys exist
        merged = self.DEFAULT_PREFERENCES.copy()
        merged.update(preferences)
        return merged

    async def update_preferences(self, updates: Dict[str, Any]) -> Dict[str, Any]:
        """
        Update user preferences.

        Args:
            updates: Dictionary of preference updates to apply

        Returns:
            The updated preferences dictionary
        """
        current = await self.get_preferences()
        current.update(updates)
        await self.store("user_preferences", current, metadata={"type": "preferences"})
        return current

    async def get_frequently_accessed(self, limit: int = 10) -> List[Dict[str, Any]]:
        """
        Get the most frequently accessed memory entries.

        Args:
            limit: Maximum number of results to return

        Returns:
            List of frequently accessed entries sorted by access count
        """
        return await self.db.get_frequently_accessed_user_memory(
            user_id=self.user_id,
            limit=limit,
        )


class MemoryManager:
    """
    Unified interface for the three-layer memory system.

    Provides methods to store and retrieve information across all memory layers
    with automatic layer ordering for recall operations.

    Layers (in order of access priority):
    1. Working Memory - Fastest, in-memory, for current task state
    2. Session Memory - Database-backed, persists across reconnections
    3. Long-term Memory - User-level, persistent across sessions

    Attributes:
        working: WorkingMemoryLayer instance
        session: SessionMemoryLayer instance
        long_term: LongTermMemoryLayer instance
    """

    def __init__(self, db: MemoryDatabase, session_id: str, user_id: str) -> None:
        """
        Initialize the memory manager with all three layers.

        Args:
            db: Database instance for persistent storage
            session_id: The current session ID
            user_id: The current user ID
        """
        self.working = WorkingMemoryLayer()
        self.session = SessionMemoryLayer(db=db, session_id=session_id)
        self.long_term = LongTermMemoryLayer(db=db, user_id=user_id)

        self._db = db
        self._session_id = session_id
        self._user_id = user_id

    async def remember(
        self,
        key: str,
        value: Any,
        layer: str = "working",
        metadata: Optional[dict] = None,
    ) -> None:
        """
        Store a value in the specified memory layer.

        Args:
            key: Unique identifier for the stored value
            value: The value to store
            layer: Which layer to store in ("working", "session", or "long_term")
            metadata: Optional metadata about the stored value

        Raises:
            ValueError: If an invalid layer name is provided
        """
        if layer == "working":
            await self.working.store(key, value, metadata)
        elif layer == "session":
            await self.session.store(key, value, metadata)
        elif layer == "long_term":
            await self.long_term.store(key, value, metadata)
        else:
            raise ValueError(
                f"Invalid memory layer: {layer}. "
                f"Must be 'working', 'session', or 'long_term'."
            )

    async def recall(self, key: str) -> Optional[Any]:
        """
        Retrieve a value, checking layers in priority order.

        Searches layers in order: working -> session -> long_term.
        Returns the first match found, or None if not found in any layer.

        Args:
            key: The key to look up

        Returns:
            The stored value if found in any layer, None otherwise
        """
        # Check working memory first (fastest)
        value = await self.working.retrieve(key)
        if value is not None:
            return value

        # Check session memory next
        value = await self.session.retrieve(key)
        if value is not None:
            return value

        # Finally check long-term memory
        value = await self.long_term.retrieve(key)
        return value

    async def search_all(
        self,
        query: str,
        limit: int = 10,
    ) -> List[Dict[str, Any]]:
        """
        Search across all memory layers.

        Results are annotated with their source layer and combined.
        Working memory results come first, followed by session, then long-term.

        Args:
            query: Search query string
            limit: Maximum total number of results to return

        Returns:
            List of matching entries with layer annotation:
            [{"key": ..., "value": ..., "metadata": ..., "layer": "working"}, ...]
        """
        results: List[Dict[str, Any]] = []

        # Search working memory
        working_results = await self.working.search(query, limit=limit)
        for result in working_results:
            result["layer"] = "working"
            results.append(result)

        # Search session memory
        remaining = limit - len(results)
        if remaining > 0:
            session_results = await self.session.search(query, limit=remaining)
            for result in session_results:
                result["layer"] = "session"
                results.append(result)

        # Search long-term memory
        remaining = limit - len(results)
        if remaining > 0:
            long_term_results = await self.long_term.search(query, limit=remaining)
            for result in long_term_results:
                result["layer"] = "long_term"
                results.append(result)

        return results[:limit]

    async def get_user_preferences(self) -> Dict[str, Any]:
        """
        Get user preferences from long-term memory.

        Convenience method that delegates to the long-term memory layer.

        Returns:
            User preferences dictionary
        """
        return await self.long_term.get_preferences()

    async def update_user_preferences(self, updates: Dict[str, Any]) -> Dict[str, Any]:
        """
        Update user preferences in long-term memory.

        Convenience method that delegates to the long-term memory layer.

        Args:
            updates: Dictionary of preference updates to apply

        Returns:
            The updated preferences dictionary
        """
        return await self.long_term.update_preferences(updates)

    def clear_working_memory(self) -> None:
        """Clear all working memory data."""
        self.working.clear()

    async def clear_session_memory(self) -> None:
        """Clear all session memory data."""
        await self.session.clear()

    @property
    def session_id(self) -> str:
        """Get the current session ID."""
        return self._session_id

    @property
    def user_id(self) -> str:
        """Get the current user ID."""
        return self._user_id
