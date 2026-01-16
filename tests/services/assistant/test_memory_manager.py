"""
Memory Manager Tests

Tests for the three-layer memory system:
- WorkingMemoryLayer: In-memory storage and search
- SessionMemoryLayer: Database-backed session memory
- LongTermMemoryLayer: User-level persistent memory
- MemoryManager: Unified interface across all layers
"""

from __future__ import annotations

import pytest
from unittest.mock import AsyncMock, MagicMock, patch
from typing import Any, Dict, List, Optional

from src.services.assistant.memory.memory_manager import (
    MemoryLayer,
    WorkingMemoryLayer,
    SessionMemoryLayer,
    LongTermMemoryLayer,
    MemoryManager,
)


# =============================================================================
# WorkingMemoryLayer Tests
# =============================================================================


class TestWorkingMemoryLayerStore:
    """Test WorkingMemoryLayer store operations."""

    @pytest.mark.asyncio
    async def test_store_simple_value(self):
        """Test storing a simple string value."""
        layer = WorkingMemoryLayer()
        await layer.store("key1", "value1")

        assert "key1" in layer._storage
        assert layer._storage["key1"] == "value1"

    @pytest.mark.asyncio
    async def test_store_complex_value(self):
        """Test storing complex data structures."""
        layer = WorkingMemoryLayer()
        complex_value = {"nested": {"data": [1, 2, 3]}, "status": "active"}
        await layer.store("task_state", complex_value)

        assert layer._storage["task_state"] == complex_value

    @pytest.mark.asyncio
    async def test_store_with_metadata(self):
        """Test storing with metadata."""
        layer = WorkingMemoryLayer()
        metadata = {"source": "user_input", "priority": "high"}
        await layer.store("key1", "value1", metadata=metadata)

        assert layer._metadata["key1"] == metadata

    @pytest.mark.asyncio
    async def test_store_overwrites_existing(self):
        """Test that storing with existing key overwrites the value."""
        layer = WorkingMemoryLayer()
        await layer.store("key1", "value1")
        await layer.store("key1", "value2")

        assert layer._storage["key1"] == "value2"

    @pytest.mark.asyncio
    async def test_store_records_timestamp(self):
        """Test that store records a timestamp."""
        layer = WorkingMemoryLayer()
        await layer.store("key1", "value1")

        assert "key1" in layer._timestamps
        assert layer._timestamps["key1"] is not None


class TestWorkingMemoryLayerRetrieve:
    """Test WorkingMemoryLayer retrieve operations."""

    @pytest.mark.asyncio
    async def test_retrieve_existing_key(self):
        """Test retrieving an existing key."""
        layer = WorkingMemoryLayer()
        await layer.store("key1", "value1")

        result = await layer.retrieve("key1")
        assert result == "value1"

    @pytest.mark.asyncio
    async def test_retrieve_nonexistent_key(self):
        """Test retrieving a nonexistent key returns None."""
        layer = WorkingMemoryLayer()

        result = await layer.retrieve("nonexistent")
        assert result is None

    @pytest.mark.asyncio
    async def test_retrieve_complex_value(self):
        """Test retrieving complex data structures."""
        layer = WorkingMemoryLayer()
        complex_value = {"tasks": ["task1", "task2"], "count": 2}
        await layer.store("state", complex_value)

        result = await layer.retrieve("state")
        assert result == complex_value


class TestWorkingMemoryLayerSearch:
    """Test WorkingMemoryLayer search operations."""

    @pytest.mark.asyncio
    async def test_search_matches_key(self):
        """Test search matches key names."""
        layer = WorkingMemoryLayer()
        await layer.store("task_progress", "50%")
        await layer.store("user_name", "Alice")

        results = await layer.search("task")
        assert len(results) == 1
        assert results[0]["key"] == "task_progress"

    @pytest.mark.asyncio
    async def test_search_matches_value(self):
        """Test search matches value content."""
        layer = WorkingMemoryLayer()
        await layer.store("key1", "The quick brown fox")
        await layer.store("key2", "Lazy dog")

        results = await layer.search("quick")
        assert len(results) == 1
        assert results[0]["key"] == "key1"

    @pytest.mark.asyncio
    async def test_search_case_insensitive(self):
        """Test search is case insensitive."""
        layer = WorkingMemoryLayer()
        await layer.store("Task_Status", "COMPLETED")

        results = await layer.search("task")
        assert len(results) == 1
        assert results[0]["key"] == "Task_Status"

    @pytest.mark.asyncio
    async def test_search_respects_limit(self):
        """Test search respects the limit parameter."""
        layer = WorkingMemoryLayer()
        for i in range(10):
            await layer.store(f"task_{i}", f"Task {i} data")

        results = await layer.search("task", limit=3)
        assert len(results) == 3

    @pytest.mark.asyncio
    async def test_search_returns_metadata(self):
        """Test search results include metadata."""
        layer = WorkingMemoryLayer()
        await layer.store("key1", "value1", metadata={"type": "test"})

        results = await layer.search("key1")
        assert results[0]["metadata"] == {"type": "test"}

    @pytest.mark.asyncio
    async def test_search_no_matches(self):
        """Test search returns empty list when no matches."""
        layer = WorkingMemoryLayer()
        await layer.store("key1", "value1")

        results = await layer.search("nonexistent")
        assert results == []

    @pytest.mark.asyncio
    async def test_search_json_value(self):
        """Test search can match in JSON-serialized complex values."""
        layer = WorkingMemoryLayer()
        await layer.store("data", {"status": "processing", "id": 123})

        results = await layer.search("processing")
        assert len(results) == 1
        assert results[0]["key"] == "data"


class TestWorkingMemoryLayerClear:
    """Test WorkingMemoryLayer clear operations."""

    @pytest.mark.asyncio
    async def test_clear_removes_all_data(self):
        """Test clear removes all stored data."""
        layer = WorkingMemoryLayer()
        await layer.store("key1", "value1")
        await layer.store("key2", "value2")

        layer.clear()

        assert len(layer._storage) == 0
        assert len(layer._metadata) == 0
        assert len(layer._timestamps) == 0

    def test_keys_returns_all_keys(self):
        """Test keys method returns all stored keys."""
        layer = WorkingMemoryLayer()
        layer._storage = {"a": 1, "b": 2, "c": 3}

        keys = layer.keys()
        assert set(keys) == {"a", "b", "c"}

    def test_len_returns_count(self):
        """Test __len__ returns item count."""
        layer = WorkingMemoryLayer()
        layer._storage = {"a": 1, "b": 2}

        assert len(layer) == 2


# =============================================================================
# SessionMemoryLayer Tests
# =============================================================================


class TestSessionMemoryLayerStore:
    """Test SessionMemoryLayer store operations."""

    @pytest.mark.asyncio
    async def test_store_calls_db_method(self):
        """Test store calls the correct database method."""
        mock_db = MagicMock()
        mock_db.store_session_memory = AsyncMock()

        layer = SessionMemoryLayer(db=mock_db, session_id="session_123")
        await layer.store("key1", {"data": "value"}, metadata={"source": "test"})

        mock_db.store_session_memory.assert_called_once_with(
            session_id="session_123",
            key="key1",
            value={"data": "value"},
            metadata={"source": "test"},
        )

    @pytest.mark.asyncio
    async def test_store_without_metadata(self):
        """Test store works without metadata."""
        mock_db = MagicMock()
        mock_db.store_session_memory = AsyncMock()

        layer = SessionMemoryLayer(db=mock_db, session_id="s1")
        await layer.store("key1", "value1")

        mock_db.store_session_memory.assert_called_once_with(
            session_id="s1",
            key="key1",
            value="value1",
            metadata=None,
        )


class TestSessionMemoryLayerRetrieve:
    """Test SessionMemoryLayer retrieve operations."""

    @pytest.mark.asyncio
    async def test_retrieve_calls_db_method(self):
        """Test retrieve calls the correct database method."""
        mock_db = MagicMock()
        mock_db.get_session_memory = AsyncMock(return_value={"data": "value"})

        layer = SessionMemoryLayer(db=mock_db, session_id="session_123")
        result = await layer.retrieve("key1")

        mock_db.get_session_memory.assert_called_once_with(
            session_id="session_123",
            key="key1",
        )
        assert result == {"data": "value"}

    @pytest.mark.asyncio
    async def test_retrieve_returns_none_when_not_found(self):
        """Test retrieve returns None when key not found."""
        mock_db = MagicMock()
        mock_db.get_session_memory = AsyncMock(return_value=None)

        layer = SessionMemoryLayer(db=mock_db, session_id="s1")
        result = await layer.retrieve("nonexistent")

        assert result is None


class TestSessionMemoryLayerSearch:
    """Test SessionMemoryLayer search operations."""

    @pytest.mark.asyncio
    async def test_search_calls_db_method(self):
        """Test search calls the correct database method."""
        mock_db = MagicMock()
        mock_db.search_session_memory = AsyncMock(
            return_value=[{"key": "task_1", "value": "data"}]
        )

        layer = SessionMemoryLayer(db=mock_db, session_id="s1")
        results = await layer.search("task", limit=5)

        mock_db.search_session_memory.assert_called_once_with(
            session_id="s1",
            query="task",
            limit=5,
        )
        assert len(results) == 1


class TestSessionMemoryLayerDelete:
    """Test SessionMemoryLayer delete operations."""

    @pytest.mark.asyncio
    async def test_delete_calls_db_method(self):
        """Test delete calls the correct database method."""
        mock_db = MagicMock()
        mock_db.delete_session_memory = AsyncMock(return_value=True)

        layer = SessionMemoryLayer(db=mock_db, session_id="s1")
        result = await layer.delete("key1")

        mock_db.delete_session_memory.assert_called_once_with(
            session_id="s1",
            key="key1",
        )
        assert result is True

    @pytest.mark.asyncio
    async def test_clear_calls_db_method(self):
        """Test clear calls the correct database method."""
        mock_db = MagicMock()
        mock_db.clear_session_memory = AsyncMock()

        layer = SessionMemoryLayer(db=mock_db, session_id="s1")
        await layer.clear()

        mock_db.clear_session_memory.assert_called_once_with(session_id="s1")


# =============================================================================
# LongTermMemoryLayer Tests
# =============================================================================


class TestLongTermMemoryLayerStore:
    """Test LongTermMemoryLayer store operations."""

    @pytest.mark.asyncio
    async def test_store_calls_db_method(self):
        """Test store calls the correct database method."""
        mock_db = MagicMock()
        mock_db.store_user_memory = AsyncMock()

        layer = LongTermMemoryLayer(db=mock_db, user_id="user_123")
        await layer.store("preference", {"theme": "dark"}, metadata={"type": "pref"})

        mock_db.store_user_memory.assert_called_once_with(
            user_id="user_123",
            key="preference",
            value={"theme": "dark"},
            metadata={"type": "pref"},
        )


class TestLongTermMemoryLayerRetrieve:
    """Test LongTermMemoryLayer retrieve operations."""

    @pytest.mark.asyncio
    async def test_retrieve_calls_db_method(self):
        """Test retrieve calls the correct database method."""
        mock_db = MagicMock()
        mock_db.get_user_memory = AsyncMock(return_value={"theme": "dark"})

        layer = LongTermMemoryLayer(db=mock_db, user_id="user_123")
        result = await layer.retrieve("preference")

        mock_db.get_user_memory.assert_called_once_with(
            user_id="user_123",
            key="preference",
        )
        assert result == {"theme": "dark"}


class TestLongTermMemoryLayerSearch:
    """Test LongTermMemoryLayer search operations."""

    @pytest.mark.asyncio
    async def test_search_calls_db_method(self):
        """Test search calls the correct database method."""
        mock_db = MagicMock()
        mock_db.search_user_memory = AsyncMock(
            return_value=[{"key": "pref_1", "value": "dark"}]
        )

        layer = LongTermMemoryLayer(db=mock_db, user_id="u1")
        results = await layer.search("pref", limit=5)

        mock_db.search_user_memory.assert_called_once_with(
            user_id="u1",
            query="pref",
            limit=5,
        )
        assert len(results) == 1


class TestLongTermMemoryLayerPreferences:
    """Test LongTermMemoryLayer preferences methods."""

    @pytest.mark.asyncio
    async def test_get_preferences_returns_stored_value(self):
        """Test get_preferences returns stored preferences."""
        mock_db = MagicMock()
        stored_prefs = {
            "language": "en-US",
            "response_style": "casual",
            "preferred_tools": ["search"],
            "default_datasets": ["docs"],
        }
        mock_db.get_user_memory = AsyncMock(return_value=stored_prefs)

        layer = LongTermMemoryLayer(db=mock_db, user_id="u1")
        result = await layer.get_preferences()

        assert result["language"] == "en-US"
        assert result["response_style"] == "casual"

    @pytest.mark.asyncio
    async def test_get_preferences_returns_defaults_when_not_set(self):
        """Test get_preferences returns defaults when no preferences stored."""
        mock_db = MagicMock()
        mock_db.get_user_memory = AsyncMock(return_value=None)

        layer = LongTermMemoryLayer(db=mock_db, user_id="u1")
        result = await layer.get_preferences()

        assert result == LongTermMemoryLayer.DEFAULT_PREFERENCES

    @pytest.mark.asyncio
    async def test_get_preferences_merges_with_defaults(self):
        """Test get_preferences merges stored with defaults for missing keys."""
        mock_db = MagicMock()
        # Only has some preferences set
        mock_db.get_user_memory = AsyncMock(return_value={"language": "fr-FR"})

        layer = LongTermMemoryLayer(db=mock_db, user_id="u1")
        result = await layer.get_preferences()

        # Should have the stored language
        assert result["language"] == "fr-FR"
        # Should have defaults for missing keys
        assert result["response_style"] == "professional"
        assert result["preferred_tools"] == []
        assert result["default_datasets"] == []

    @pytest.mark.asyncio
    async def test_update_preferences(self):
        """Test update_preferences updates and stores preferences."""
        mock_db = MagicMock()
        mock_db.get_user_memory = AsyncMock(return_value=None)
        mock_db.store_user_memory = AsyncMock()

        layer = LongTermMemoryLayer(db=mock_db, user_id="u1")
        result = await layer.update_preferences({"language": "ja-JP"})

        # Should return updated preferences
        assert result["language"] == "ja-JP"

        # Should call store with correct parameters
        mock_db.store_user_memory.assert_called_once()
        call_args = mock_db.store_user_memory.call_args
        assert call_args.kwargs["user_id"] == "u1"
        assert call_args.kwargs["key"] == "user_preferences"
        assert call_args.kwargs["value"]["language"] == "ja-JP"

    @pytest.mark.asyncio
    async def test_default_preferences_values(self):
        """Test that default preferences have expected values."""
        defaults = LongTermMemoryLayer.DEFAULT_PREFERENCES

        assert defaults["language"] == "zh-CN"
        assert defaults["response_style"] == "professional"
        assert defaults["preferred_tools"] == []
        assert defaults["default_datasets"] == []


class TestLongTermMemoryLayerDelete:
    """Test LongTermMemoryLayer delete operations."""

    @pytest.mark.asyncio
    async def test_delete_calls_db_method(self):
        """Test delete calls the correct database method."""
        mock_db = MagicMock()
        mock_db.delete_user_memory = AsyncMock(return_value=True)

        layer = LongTermMemoryLayer(db=mock_db, user_id="u1")
        result = await layer.delete("key1")

        mock_db.delete_user_memory.assert_called_once_with(
            user_id="u1",
            key="key1",
        )
        assert result is True

    @pytest.mark.asyncio
    async def test_get_frequently_accessed_calls_db_method(self):
        """Test get_frequently_accessed calls the correct database method."""
        mock_db = MagicMock()
        mock_db.get_frequently_accessed_user_memory = AsyncMock(
            return_value=[{"key": "common_pref", "access_count": 50}]
        )

        layer = LongTermMemoryLayer(db=mock_db, user_id="u1")
        results = await layer.get_frequently_accessed(limit=5)

        mock_db.get_frequently_accessed_user_memory.assert_called_once_with(
            user_id="u1",
            limit=5,
        )
        assert len(results) == 1


# =============================================================================
# MemoryManager Tests
# =============================================================================


class TestMemoryManagerInit:
    """Test MemoryManager initialization."""

    def test_init_creates_all_layers(self):
        """Test initialization creates all three memory layers."""
        mock_db = MagicMock()

        manager = MemoryManager(db=mock_db, session_id="s1", user_id="u1")

        assert isinstance(manager.working, WorkingMemoryLayer)
        assert isinstance(manager.session, SessionMemoryLayer)
        assert isinstance(manager.long_term, LongTermMemoryLayer)

    def test_init_sets_properties(self):
        """Test initialization sets session_id and user_id properties."""
        mock_db = MagicMock()

        manager = MemoryManager(db=mock_db, session_id="s1", user_id="u1")

        assert manager.session_id == "s1"
        assert manager.user_id == "u1"


class TestMemoryManagerRemember:
    """Test MemoryManager remember operations."""

    @pytest.mark.asyncio
    async def test_remember_default_layer(self):
        """Test remember stores in working memory by default."""
        mock_db = MagicMock()
        manager = MemoryManager(db=mock_db, session_id="s1", user_id="u1")

        await manager.remember("key1", "value1")

        result = await manager.working.retrieve("key1")
        assert result == "value1"

    @pytest.mark.asyncio
    async def test_remember_working_layer(self):
        """Test remember with explicit working layer."""
        mock_db = MagicMock()
        manager = MemoryManager(db=mock_db, session_id="s1", user_id="u1")

        await manager.remember("key1", "value1", layer="working")

        result = await manager.working.retrieve("key1")
        assert result == "value1"

    @pytest.mark.asyncio
    async def test_remember_session_layer(self):
        """Test remember stores in session memory."""
        mock_db = MagicMock()
        mock_db.store_session_memory = AsyncMock()

        manager = MemoryManager(db=mock_db, session_id="s1", user_id="u1")
        await manager.remember("key1", "value1", layer="session")

        mock_db.store_session_memory.assert_called_once()

    @pytest.mark.asyncio
    async def test_remember_long_term_layer(self):
        """Test remember stores in long-term memory."""
        mock_db = MagicMock()
        mock_db.store_user_memory = AsyncMock()

        manager = MemoryManager(db=mock_db, session_id="s1", user_id="u1")
        await manager.remember("key1", "value1", layer="long_term")

        mock_db.store_user_memory.assert_called_once()

    @pytest.mark.asyncio
    async def test_remember_with_metadata(self):
        """Test remember passes metadata correctly."""
        mock_db = MagicMock()
        manager = MemoryManager(db=mock_db, session_id="s1", user_id="u1")

        await manager.remember("key1", "value1", metadata={"type": "test"})

        # Check metadata was stored
        assert manager.working._metadata["key1"] == {"type": "test"}

    @pytest.mark.asyncio
    async def test_remember_invalid_layer_raises_error(self):
        """Test remember raises ValueError for invalid layer."""
        mock_db = MagicMock()
        manager = MemoryManager(db=mock_db, session_id="s1", user_id="u1")

        with pytest.raises(ValueError) as exc_info:
            await manager.remember("key1", "value1", layer="invalid")

        assert "Invalid memory layer" in str(exc_info.value)


class TestMemoryManagerRecall:
    """Test MemoryManager recall operations."""

    @pytest.mark.asyncio
    async def test_recall_from_working_memory(self):
        """Test recall finds value in working memory first."""
        mock_db = MagicMock()
        mock_db.get_session_memory = AsyncMock(return_value=None)
        mock_db.get_user_memory = AsyncMock(return_value=None)

        manager = MemoryManager(db=mock_db, session_id="s1", user_id="u1")
        await manager.working.store("key1", "working_value")

        result = await manager.recall("key1")
        assert result == "working_value"

    @pytest.mark.asyncio
    async def test_recall_from_session_memory(self):
        """Test recall finds value in session memory when not in working."""
        mock_db = MagicMock()
        mock_db.get_session_memory = AsyncMock(return_value="session_value")
        mock_db.get_user_memory = AsyncMock(return_value=None)

        manager = MemoryManager(db=mock_db, session_id="s1", user_id="u1")

        result = await manager.recall("key1")
        assert result == "session_value"

    @pytest.mark.asyncio
    async def test_recall_from_long_term_memory(self):
        """Test recall finds value in long-term memory as last resort."""
        mock_db = MagicMock()
        mock_db.get_session_memory = AsyncMock(return_value=None)
        mock_db.get_user_memory = AsyncMock(return_value="long_term_value")

        manager = MemoryManager(db=mock_db, session_id="s1", user_id="u1")

        result = await manager.recall("key1")
        assert result == "long_term_value"

    @pytest.mark.asyncio
    async def test_recall_not_found_returns_none(self):
        """Test recall returns None when not found in any layer."""
        mock_db = MagicMock()
        mock_db.get_session_memory = AsyncMock(return_value=None)
        mock_db.get_user_memory = AsyncMock(return_value=None)

        manager = MemoryManager(db=mock_db, session_id="s1", user_id="u1")

        result = await manager.recall("nonexistent")
        assert result is None

    @pytest.mark.asyncio
    async def test_recall_priority_order(self):
        """Test recall respects layer priority (working > session > long_term)."""
        mock_db = MagicMock()
        mock_db.get_session_memory = AsyncMock(return_value="session_value")
        mock_db.get_user_memory = AsyncMock(return_value="long_term_value")

        manager = MemoryManager(db=mock_db, session_id="s1", user_id="u1")
        await manager.working.store("key1", "working_value")

        result = await manager.recall("key1")

        # Should return working memory value, not session or long-term
        assert result == "working_value"
        # Session and long-term should not be called since working had value
        mock_db.get_session_memory.assert_not_called()
        mock_db.get_user_memory.assert_not_called()


class TestMemoryManagerSearchAll:
    """Test MemoryManager search_all operations."""

    @pytest.mark.asyncio
    async def test_search_all_combines_results(self):
        """Test search_all combines results from all layers."""
        mock_db = MagicMock()
        mock_db.search_session_memory = AsyncMock(
            return_value=[{"key": "session_task", "value": "s_data"}]
        )
        mock_db.search_user_memory = AsyncMock(
            return_value=[{"key": "user_task", "value": "u_data"}]
        )

        manager = MemoryManager(db=mock_db, session_id="s1", user_id="u1")
        await manager.working.store("working_task", "w_data")

        results = await manager.search_all("task")

        assert len(results) == 3
        # Check layer annotations
        layers = [r["layer"] for r in results]
        assert "working" in layers
        assert "session" in layers
        assert "long_term" in layers

    @pytest.mark.asyncio
    async def test_search_all_annotates_layers(self):
        """Test search_all adds layer annotation to results."""
        mock_db = MagicMock()
        mock_db.search_session_memory = AsyncMock(return_value=[])
        mock_db.search_user_memory = AsyncMock(return_value=[])

        manager = MemoryManager(db=mock_db, session_id="s1", user_id="u1")
        await manager.working.store("key1", "value1")

        results = await manager.search_all("key1")

        assert results[0]["layer"] == "working"

    @pytest.mark.asyncio
    async def test_search_all_respects_limit(self):
        """Test search_all respects the overall limit."""
        mock_db = MagicMock()
        mock_db.search_session_memory = AsyncMock(
            return_value=[{"key": f"s_{i}", "value": f"v_{i}"} for i in range(5)]
        )
        mock_db.search_user_memory = AsyncMock(
            return_value=[{"key": f"u_{i}", "value": f"v_{i}"} for i in range(5)]
        )

        manager = MemoryManager(db=mock_db, session_id="s1", user_id="u1")
        for i in range(5):
            await manager.working.store(f"w_{i}", f"v_{i}")

        results = await manager.search_all("", limit=7)

        assert len(results) <= 7

    @pytest.mark.asyncio
    async def test_search_all_working_first(self):
        """Test search_all returns working memory results first."""
        mock_db = MagicMock()
        mock_db.search_session_memory = AsyncMock(
            return_value=[{"key": "session_key", "value": "session_val"}]
        )
        mock_db.search_user_memory = AsyncMock(
            return_value=[{"key": "user_key", "value": "user_val"}]
        )

        manager = MemoryManager(db=mock_db, session_id="s1", user_id="u1")
        await manager.working.store("working_key", "working_val")

        results = await manager.search_all("key")

        # First result should be from working memory
        assert results[0]["layer"] == "working"


class TestMemoryManagerPreferences:
    """Test MemoryManager preference convenience methods."""

    @pytest.mark.asyncio
    async def test_get_user_preferences(self):
        """Test get_user_preferences delegates to long_term layer."""
        mock_db = MagicMock()
        mock_db.get_user_memory = AsyncMock(return_value={"language": "en-US"})

        manager = MemoryManager(db=mock_db, session_id="s1", user_id="u1")
        result = await manager.get_user_preferences()

        assert result["language"] == "en-US"

    @pytest.mark.asyncio
    async def test_update_user_preferences(self):
        """Test update_user_preferences delegates to long_term layer."""
        mock_db = MagicMock()
        mock_db.get_user_memory = AsyncMock(return_value=None)
        mock_db.store_user_memory = AsyncMock()

        manager = MemoryManager(db=mock_db, session_id="s1", user_id="u1")
        result = await manager.update_user_preferences({"language": "de-DE"})

        assert result["language"] == "de-DE"
        mock_db.store_user_memory.assert_called_once()


class TestMemoryManagerClear:
    """Test MemoryManager clear operations."""

    def test_clear_working_memory(self):
        """Test clear_working_memory clears only working memory."""
        mock_db = MagicMock()
        manager = MemoryManager(db=mock_db, session_id="s1", user_id="u1")
        manager.working._storage = {"key1": "value1"}

        manager.clear_working_memory()

        assert len(manager.working._storage) == 0

    @pytest.mark.asyncio
    async def test_clear_session_memory(self):
        """Test clear_session_memory calls db method."""
        mock_db = MagicMock()
        mock_db.clear_session_memory = AsyncMock()

        manager = MemoryManager(db=mock_db, session_id="s1", user_id="u1")
        await manager.clear_session_memory()

        mock_db.clear_session_memory.assert_called_once_with(session_id="s1")


# =============================================================================
# Integration Tests
# =============================================================================


class TestMemoryManagerIntegration:
    """Integration tests for MemoryManager workflow."""

    @pytest.mark.asyncio
    async def test_typical_workflow(self):
        """Test a typical memory usage workflow."""
        mock_db = MagicMock()
        mock_db.store_session_memory = AsyncMock()
        mock_db.store_user_memory = AsyncMock()
        mock_db.get_session_memory = AsyncMock(return_value=None)
        mock_db.get_user_memory = AsyncMock(return_value=None)

        manager = MemoryManager(db=mock_db, session_id="s1", user_id="u1")

        # Store current task in working memory
        await manager.remember("current_task", {"step": 1, "status": "in_progress"})

        # Store search results in session memory
        await manager.remember(
            "search_results",
            [{"doc": "result1"}, {"doc": "result2"}],
            layer="session",
        )

        # Store user preference in long-term memory
        await manager.remember(
            "output_format_preference",
            "markdown",
            layer="long_term",
        )

        # Recall from working memory
        task = await manager.recall("current_task")
        assert task["step"] == 1

        # Verify db methods were called for persistent layers
        mock_db.store_session_memory.assert_called_once()
        mock_db.store_user_memory.assert_called_once()

    @pytest.mark.asyncio
    async def test_layer_fallback_workflow(self):
        """Test recall falls back through layers correctly."""
        mock_db = MagicMock()
        mock_db.get_session_memory = AsyncMock(return_value=None)
        mock_db.get_user_memory = AsyncMock(return_value="default_format")

        manager = MemoryManager(db=mock_db, session_id="s1", user_id="u1")

        # No value in working or session, should find in long-term
        result = await manager.recall("output_format")

        assert result == "default_format"
        mock_db.get_session_memory.assert_called_once()
        mock_db.get_user_memory.assert_called_once()
