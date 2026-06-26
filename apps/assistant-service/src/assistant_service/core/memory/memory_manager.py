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
    manager = MemoryManager(
        db=database, tenant_id="tenant1", user_id="u1", session_id="s1"
    )

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
import re
from abc import ABC, abstractmethod
from datetime import datetime
from enum import Enum
from typing import Any, Protocol, runtime_checkable


class MemoryPolicyError(ValueError):
    """Raised when a memory operation violates the configured profile policy."""


class MemoryProfile(str, Enum):
    """Supported user-visible memory profiles."""

    OFF = "off"
    BASIC = "basic"
    HYBRID = "hybrid"


class MemoryType(str, Enum):
    """Explicit memory taxonomy used by NGA-03."""

    PROCEDURAL = "procedural"
    SITUATIONAL = "situational"
    SEMANTIC = "semantic"


_EMAIL_RE = re.compile(r"[A-Za-z0-9._%+-]+@[A-Za-z0-9.-]+\.[A-Za-z]{2,}")
_PHONE_RE = re.compile(r"(?<!\w)(?:\+?\d[\d\s().-]{7,}\d)(?!\w)")
_PROMPT_INJECTION_RE = re.compile(
    r"(?i)\b(?:"
    r"ignore\s+(?:all\s+)?(?:previous|prior)\s+instructions|"
    r"reveal\s+(?:the\s+)?system\s+prompt|"
    r"show\s+(?:the\s+)?system\s+prompt|"
    r"developer\s+message|"
    r"system\s+prompt"
    r")\b"
)


def _normalize_profile(profile: MemoryProfile | str | None) -> MemoryProfile:
    if isinstance(profile, MemoryProfile):
        return profile
    try:
        return MemoryProfile(str(profile or MemoryProfile.HYBRID.value).lower())
    except ValueError as exc:
        raise MemoryPolicyError(f"Unsupported memory profile: {profile}") from exc


def _normalize_memory_type(memory_type: MemoryType | str | None) -> MemoryType:
    if isinstance(memory_type, MemoryType):
        return memory_type
    try:
        return MemoryType(str(memory_type or MemoryType.SEMANTIC.value).lower())
    except ValueError as exc:
        raise MemoryPolicyError(f"Unsupported memory type: {memory_type}") from exc


def _sanitize_text(value: str) -> tuple[str, dict[str, bool]]:
    redacted = _EMAIL_RE.sub("[redacted-email]", value)
    redacted = _PHONE_RE.sub("[redacted-phone]", redacted)
    pii_filtered = redacted != value

    filtered = _PROMPT_INJECTION_RE.sub("[filtered-prompt-injection]", redacted)
    return filtered, {
        "pii_filtered": pii_filtered,
        "prompt_injection_filtered": filtered != redacted,
    }


def sanitize_memory_value(value: Any) -> tuple[Any, dict[str, bool]]:
    """Redact PII and neutralize prompt-control text in memory values."""
    if isinstance(value, str):
        return _sanitize_text(value)

    if isinstance(value, list):
        sanitized_items = []
        pii_filtered = False
        prompt_filtered = False
        for item in value:
            sanitized_item, flags = sanitize_memory_value(item)
            sanitized_items.append(sanitized_item)
            pii_filtered = pii_filtered or flags["pii_filtered"]
            prompt_filtered = prompt_filtered or flags["prompt_injection_filtered"]
        return sanitized_items, {
            "pii_filtered": pii_filtered,
            "prompt_injection_filtered": prompt_filtered,
        }

    if isinstance(value, dict):
        sanitized_dict: dict[Any, Any] = {}
        pii_filtered = False
        prompt_filtered = False
        for key, item in value.items():
            sanitized_item, flags = sanitize_memory_value(item)
            sanitized_dict[key] = sanitized_item
            pii_filtered = pii_filtered or flags["pii_filtered"]
            prompt_filtered = prompt_filtered or flags["prompt_injection_filtered"]
        return sanitized_dict, {
            "pii_filtered": pii_filtered,
            "prompt_injection_filtered": prompt_filtered,
        }

    return value, {
        "pii_filtered": False,
        "prompt_injection_filtered": False,
    }


@runtime_checkable
class MemoryDatabase(Protocol):
    """
    Protocol defining the database interface for memory operations.

    This protocol defines the methods that will be implemented in Task 3.4
    for session and user memory persistence.

    Multi-tenancy: All methods require tenant_id as the first parameter
    to ensure proper data isolation between tenants. The caller must
    provide the tenant_id explicitly for all database operations.
    """

    async def store_session_memory(
        self, tenant_id: str, session_id: str, key: str, value: Any, metadata: dict | None
    ) -> None: ...

    async def get_session_memory(self, tenant_id: str, session_id: str, key: str) -> Any | None: ...

    async def search_session_memory(
        self, tenant_id: str, session_id: str, query: str, limit: int
    ) -> list[dict[str, Any]]: ...

    async def delete_session_memory(self, tenant_id: str, session_id: str, key: str) -> bool: ...

    async def clear_session_memory(self, tenant_id: str, session_id: str) -> None: ...

    async def store_user_memory(
        self, tenant_id: str, user_id: str, key: str, value: Any, metadata: dict | None
    ) -> None: ...

    async def get_user_memory(self, tenant_id: str, user_id: str, key: str) -> Any | None: ...

    async def search_user_memory(
        self, tenant_id: str, user_id: str, query: str, limit: int
    ) -> list[dict[str, Any]]: ...

    async def delete_user_memory(self, tenant_id: str, user_id: str, key: str) -> bool: ...

    async def get_frequently_accessed_user_memory(
        self, tenant_id: str, user_id: str, limit: int
    ) -> list[dict[str, Any]]: ...


class MemoryLayer(ABC):
    """
    Abstract base class for memory layers.

    Each memory layer implements storage, retrieval, and search operations
    with different persistence characteristics and performance profiles.
    """

    @abstractmethod
    async def store(self, key: str, value: Any, metadata: dict | None = None) -> None:
        """
        Store a value with the given key.

        Args:
            key: Unique identifier for the stored value
            value: The value to store (any JSON-serializable type)
            metadata: Optional metadata about the stored value
        """
        pass

    @abstractmethod
    async def retrieve(self, key: str) -> Any | None:
        """
        Retrieve a value by key.

        Args:
            key: The key to look up

        Returns:
            The stored value if found, None otherwise
        """
        pass

    @abstractmethod
    async def search(self, query: str, limit: int = 10) -> list[dict[str, Any]]:
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
        self._storage: dict[str, Any] = {}
        self._metadata: dict[str, dict] = {}
        self._timestamps: dict[str, datetime] = {}

    async def store(self, key: str, value: Any, metadata: dict | None = None) -> None:
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

    async def retrieve(self, key: str) -> Any | None:
        """
        Retrieve a value from working memory.

        Args:
            key: The key to look up

        Returns:
            The stored value if found, None otherwise
        """
        return self._storage.get(key)

    async def search(self, query: str, limit: int = 10) -> list[dict[str, Any]]:
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
        results: list[dict[str, Any]] = []
        query_lower = query.lower()

        for key, value in self._storage.items():
            # Convert value to string for matching
            value_str = json.dumps(value) if not isinstance(value, str) else value

            # Check if query matches key or value (not both to avoid duplicates)
            if query_lower in key.lower() or query_lower in value_str.lower():
                results.append(
                    {
                        "key": key,
                        "value": value,
                        "metadata": self._metadata.get(key, {}),
                        "timestamp": self._timestamps.get(key),
                    }
                )

            # Check limit after both conditions
            if len(results) >= limit:
                break

        return results

    def clear(self) -> None:
        """Clear all working memory data."""
        self._storage.clear()
        self._metadata.clear()
        self._timestamps.clear()

    def keys(self) -> list[str]:
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
        tenant_id: The tenant ID for multi-tenancy isolation
        session_id: The session ID this memory belongs to
    """

    def __init__(self, db: MemoryDatabase, tenant_id: str, session_id: str) -> None:
        """
        Initialize session memory layer.

        Args:
            db: Database instance for persistence
            tenant_id: The tenant ID for multi-tenancy isolation
            session_id: The session ID this memory belongs to
        """
        self.db = db
        self.tenant_id = tenant_id
        self.session_id = session_id

    async def store(self, key: str, value: Any, metadata: dict | None = None) -> None:
        """
        Store a value in session memory (database-backed).

        Args:
            key: Unique identifier for the stored value
            value: The value to store (will be JSON-serialized)
            metadata: Optional metadata about the stored value
        """
        await self.db.store_session_memory(
            tenant_id=self.tenant_id,
            session_id=self.session_id,
            key=key,
            value=value,
            metadata=metadata,
        )

    async def retrieve(self, key: str) -> Any | None:
        """
        Retrieve a value from session memory.

        Args:
            key: The key to look up

        Returns:
            The stored value if found, None otherwise
        """
        return await self.db.get_session_memory(
            tenant_id=self.tenant_id,
            session_id=self.session_id,
            key=key,
        )

    async def search(self, query: str, limit: int = 10) -> list[dict[str, Any]]:
        """
        Search session memory.

        Args:
            query: Search query string
            limit: Maximum number of results to return

        Returns:
            List of matching entries with key, value, and metadata
        """
        return await self.db.search_session_memory(
            tenant_id=self.tenant_id,
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
            tenant_id=self.tenant_id,
            session_id=self.session_id,
            key=key,
        )

    async def clear(self) -> None:
        """Clear all session memory for this session."""
        await self.db.clear_session_memory(
            tenant_id=self.tenant_id,
            session_id=self.session_id,
        )


class LongTermMemoryLayer(MemoryLayer):
    """
    User-level persistent memory for preferences and patterns.

    This layer stores information that persists across sessions,
    including user preferences, learned patterns, and frequently
    used resources.

    Attributes:
        db: Database instance for persistence
        tenant_id: The tenant ID for multi-tenancy isolation
        user_id: The user ID this memory belongs to
    """

    # Default user preferences
    DEFAULT_PREFERENCES: dict[str, Any] = {
        "language": "zh-CN",
        "response_style": "professional",
        "preferred_tools": [],
        "default_datasets": [],
    }

    def __init__(self, db: MemoryDatabase, tenant_id: str, user_id: str) -> None:
        """
        Initialize long-term memory layer.

        Args:
            db: Database instance for persistence
            tenant_id: The tenant ID for multi-tenancy isolation
            user_id: The user ID this memory belongs to
        """
        self.db = db
        self.tenant_id = tenant_id
        self.user_id = user_id

    async def store(self, key: str, value: Any, metadata: dict | None = None) -> None:
        """
        Store a value in long-term memory (database-backed).

        Args:
            key: Unique identifier for the stored value
            value: The value to store (will be JSON-serialized)
            metadata: Optional metadata about the stored value
        """
        await self.db.store_user_memory(
            tenant_id=self.tenant_id,
            user_id=self.user_id,
            key=key,
            value=value,
            metadata=metadata,
        )

    async def retrieve(self, key: str) -> Any | None:
        """
        Retrieve a value from long-term memory.

        Also increments the access count for the retrieved key.

        Args:
            key: The key to look up

        Returns:
            The stored value if found, None otherwise
        """
        return await self.db.get_user_memory(
            tenant_id=self.tenant_id,
            user_id=self.user_id,
            key=key,
        )

    async def search(self, query: str, limit: int = 10) -> list[dict[str, Any]]:
        """
        Search long-term memory.

        Args:
            query: Search query string
            limit: Maximum number of results to return

        Returns:
            List of matching entries with key, value, and metadata
        """
        return await self.db.search_user_memory(
            tenant_id=self.tenant_id,
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
            tenant_id=self.tenant_id,
            user_id=self.user_id,
            key=key,
        )

    async def get_preferences(self) -> dict[str, Any]:
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

    async def update_preferences(self, updates: dict[str, Any]) -> dict[str, Any]:
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

    async def get_frequently_accessed(self, limit: int = 10) -> list[dict[str, Any]]:
        """
        Get the most frequently accessed memory entries.

        Args:
            limit: Maximum number of results to return

        Returns:
            List of frequently accessed entries sorted by access count
        """
        return await self.db.get_frequently_accessed_user_memory(
            tenant_id=self.tenant_id,
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

    def __init__(
        self,
        db: MemoryDatabase,
        tenant_id: str,
        user_id: str,
        session_id: str,
        profile: MemoryProfile | str = MemoryProfile.HYBRID,
    ) -> None:
        """
        Initialize the memory manager with all three layers.

        Args:
            db: Database instance for persistent storage
            tenant_id: The tenant ID for multi-tenancy isolation
            user_id: The current user ID
            session_id: The current session ID
            profile: Memory profile controlling long-term writes and recall
        """
        self.working = WorkingMemoryLayer()
        self.session = SessionMemoryLayer(db=db, tenant_id=tenant_id, session_id=session_id)
        self.long_term = LongTermMemoryLayer(db=db, tenant_id=tenant_id, user_id=user_id)

        self._db = db
        self._tenant_id = tenant_id
        self._session_id = session_id
        self._user_id = user_id
        self._profile = _normalize_profile(profile)

    async def remember(
        self,
        key: str,
        value: Any,
        layer: str = "working",
        metadata: dict | None = None,
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
        sanitized_value, boundary_metadata = self._apply_boundary_policy(
            layer=layer,
            value=value,
            metadata=metadata,
            operation="write",
        )

        if layer == "working":
            await self.working.store(key, sanitized_value, boundary_metadata)
        elif layer == "session":
            await self.session.store(key, sanitized_value, boundary_metadata)
        elif layer == "long_term":
            await self.long_term.store(key, sanitized_value, boundary_metadata)
        else:
            raise ValueError(
                f"Invalid memory layer: {layer}. Must be 'working', 'session', or 'long_term'."
            )

    async def recall(self, key: str) -> Any | None:
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

        # Finally check long-term memory when the active profile allows durable recall.
        if not self._profile_allows_long_term_read():
            return None
        value = await self.long_term.retrieve(key)
        sanitized_value, _ = sanitize_memory_value(value)
        return sanitized_value

    async def search_all(
        self,
        query: str,
        limit: int = 10,
    ) -> list[dict[str, Any]]:
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
        results: list[dict[str, Any]] = []

        # Search working memory
        working_results = await self.working.search(query, limit=limit)
        for result in working_results:
            result["layer"] = "working"
            results.append(self._sanitize_search_result(result))

        # Search session memory
        remaining = limit - len(results)
        if remaining > 0:
            session_results = await self.session.search(query, limit=remaining)
            for result in session_results:
                result["layer"] = "session"
                results.append(self._sanitize_search_result(result))

        # Search long-term memory
        remaining = limit - len(results)
        if remaining > 0 and self._profile_allows_long_term_read():
            long_term_results = await self.long_term.search(query, limit=remaining)
            for result in long_term_results:
                result["layer"] = "long_term"
                results.append(self._sanitize_search_result(result))

        return results[:limit]

    async def get_user_preferences(self) -> dict[str, Any]:
        """
        Get user preferences from long-term memory.

        Convenience method that delegates to the long-term memory layer.

        Returns:
            User preferences dictionary
        """
        if not self._profile_allows_long_term_read():
            return {}
        return await self.long_term.get_preferences()

    async def update_user_preferences(self, updates: dict[str, Any]) -> dict[str, Any]:
        """
        Update user preferences in long-term memory.

        Convenience method that delegates to the long-term memory layer.

        Args:
            updates: Dictionary of preference updates to apply

        Returns:
            The updated preferences dictionary
        """
        if self._profile == MemoryProfile.OFF:
            raise MemoryPolicyError("Memory profile 'off' blocks long-term preference writes.")

        sanitized_updates, _ = sanitize_memory_value(updates)
        return await self.long_term.update_preferences(sanitized_updates)

    def clear_working_memory(self) -> None:
        """Clear all working memory data."""
        self.working.clear()

    async def clear_session_memory(self) -> None:
        """Clear all session memory data."""
        await self.session.clear()

    async def delete_memory(self, key: str, layer: str = "long_term") -> bool:
        """
        Delete memory from an explicit layer.

        Deletion is allowed even when a profile blocks recall, so users can purge
        durable memory after switching memory off.
        """
        if layer == "working":
            existed = key in self.working._storage
            if existed:
                self.working._storage.pop(key, None)
                self.working._metadata.pop(key, None)
                self.working._timestamps.pop(key, None)
            return existed
        if layer == "session":
            return await self.session.delete(key)
        if layer == "long_term":
            return await self.long_term.delete(key)
        raise ValueError(
            f"Invalid memory layer: {layer}. Must be 'working', 'session', or 'long_term'."
        )

    def inspect_memory_policy(self) -> dict[str, Any]:
        """Return storage/retrieval/delete boundaries without exposing values."""
        return {
            "profile": self._profile.value,
            "storage": {
                "working": {
                    "memory_type": MemoryType.SITUATIONAL.value,
                    "scope": {"tenant_id": self._tenant_id, "session_id": self._session_id},
                    "retention": "active_run_or_process",
                    "inspect": "keys_and_metadata_only",
                    "delete": "clear_working_memory_or_delete_memory",
                },
                "session": {
                    "memory_type": MemoryType.SITUATIONAL.value,
                    "scope": {"tenant_id": self._tenant_id, "session_id": self._session_id},
                    "retention": "until_session_deleted_or_cleared",
                    "inspect": "scoped_database_lookup",
                    "delete": "delete_memory(layer='session')",
                },
                "long_term": {
                    "memory_type": MemoryType.SEMANTIC.value,
                    "scope": {"tenant_id": self._tenant_id, "user_id": self._user_id},
                    "retention": "until_explicit_delete",
                    "inspect": "explicit_key_or_search_only",
                    "delete": "delete_memory(layer='long_term')",
                },
            },
            "profiles": {
                MemoryProfile.OFF.value: "session/working only; no long-term write or recall",
                MemoryProfile.BASIC.value: "semantic facts and preferences only",
                MemoryProfile.HYBRID.value: "semantic, situational, and proposed procedural memory",
            },
            "privacy": {
                "pii_filter": "email and phone redaction before store/recall",
                "prompt_boundary": "prompt-control text is treated as untrusted memory data",
            },
        }

    def _profile_allows_long_term_read(self) -> bool:
        return self._profile in {MemoryProfile.BASIC, MemoryProfile.HYBRID}

    def _apply_boundary_policy(
        self,
        *,
        layer: str,
        value: Any,
        metadata: dict | None,
        operation: str,
    ) -> tuple[Any, dict[str, Any]]:
        memory_type = self._memory_type_for_layer(layer, metadata)
        if operation == "write":
            self._validate_write_allowed(layer=layer, memory_type=memory_type)

        sanitized_value, privacy = sanitize_memory_value(value)
        boundary_metadata: dict[str, Any] = dict(metadata or {})
        boundary_metadata["memory_type"] = memory_type.value
        boundary_metadata["memory_profile"] = self._profile.value
        boundary_metadata["scope"] = self._scope_for_layer(layer)
        boundary_metadata["privacy"] = privacy
        boundary_metadata["trust"] = "untrusted_memory_data"
        if memory_type == MemoryType.PROCEDURAL:
            boundary_metadata.setdefault("review_status", "proposed")
        return sanitized_value, boundary_metadata

    def _validate_write_allowed(self, *, layer: str, memory_type: MemoryType) -> None:
        if layer == "long_term" and self._profile == MemoryProfile.OFF:
            raise MemoryPolicyError("Memory profile 'off' blocks long-term memory writes.")
        if self._profile == MemoryProfile.BASIC and memory_type != MemoryType.SEMANTIC:
            raise MemoryPolicyError("Memory profile 'basic' allows only semantic memory.")

    def _memory_type_for_layer(self, layer: str, metadata: dict | None) -> MemoryType:
        if metadata and metadata.get("memory_type"):
            return _normalize_memory_type(metadata.get("memory_type"))
        if layer in {"working", "session"}:
            return MemoryType.SITUATIONAL
        return MemoryType.SEMANTIC

    def _scope_for_layer(self, layer: str) -> dict[str, str]:
        if layer == "working":
            return {
                "tenant_id": self._tenant_id,
                "session_id": self._session_id,
                "layer": layer,
            }
        if layer == "session":
            return {
                "tenant_id": self._tenant_id,
                "session_id": self._session_id,
                "layer": layer,
            }
        if layer == "long_term":
            return {
                "tenant_id": self._tenant_id,
                "user_id": self._user_id,
                "layer": layer,
            }
        return {"tenant_id": self._tenant_id, "layer": layer}

    @staticmethod
    def _sanitize_search_result(result: dict[str, Any]) -> dict[str, Any]:
        sanitized = dict(result)
        value, privacy = sanitize_memory_value(sanitized.get("value"))
        sanitized["value"] = value
        metadata = dict(sanitized.get("metadata") or {})
        metadata.setdefault("privacy", privacy)
        metadata.setdefault("trust", "untrusted_memory_data")
        sanitized["metadata"] = metadata
        return sanitized

    @property
    def tenant_id(self) -> str:
        """Get the current tenant ID."""
        return self._tenant_id

    @property
    def session_id(self) -> str:
        """Get the current session ID."""
        return self._session_id

    @property
    def user_id(self) -> str:
        """Get the current user ID."""
        return self._user_id

    @property
    def profile(self) -> MemoryProfile:
        """Get the active memory profile."""
        return self._profile
