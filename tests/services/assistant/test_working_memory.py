"""
Working Memory Tests

Tests for WorkingMemory class and related components:
- TaskItem and CollectedInfo dataclasses
- WorkingMemory task management
- Markdown rendering for context injection
- Serialization/deserialization
"""

from datetime import datetime

import pytest
from ai_gateway_core.tasks.task_manager import TaskManager
from assistant_service.core.runtime.memory.working_state import (
    LEGACY_WORKING_MEMORY_KEY,
    bounded_working_memory_context,
    persist_working_memory,
    restore_working_memory,
    working_memory_key,
)
from assistant_service.core.working_memory import (
    CollectedInfo,
    TaskItem,
    TaskStatus,
    WorkingMemory,
)


class _WorkingStateMemoryService:
    def __init__(self) -> None:
        self.payloads: dict[str, object] = {}
        self.calls: list[dict[str, object]] = []
        self.fail_keys: set[str] = set()

    async def get_session_memory(self, **kwargs):
        return self.payloads.get(str(kwargs["key"]))

    async def set_session_memory(self, **kwargs):
        self.calls.append(dict(kwargs))
        key = str(kwargs["key"])
        if key in self.fail_keys:
            return False
        self.payloads[key] = kwargs["value"]
        return True


@pytest.mark.asyncio
async def test_task_manager_rejects_cross_owner_session_reuse():
    """A cached session id remains bound to its original tenant and user."""
    manager = TaskManager()

    async with manager.session_context("shared", "tenant_a", "user_a") as session:
        session.working_memory.set_goal("tenant-a private goal")

    with pytest.raises(PermissionError, match="different tenant or user"):
        async with manager.session_context("shared", "tenant_b", "user_a"):
            pass

    with pytest.raises(PermissionError, match="different tenant or user"):
        async with manager.session_context("shared", "tenant_a", "user_b"):
            pass

    original = await manager.get_session("shared")
    assert original is not None
    assert original.tenant_id == "tenant_a"
    assert original.user_id == "user_a"
    assert await manager.get_session("shared", tenant_id="tenant_a", user_id="user_b") is None


def test_persisted_working_memory_restores_only_for_expected_session():
    memory = WorkingMemory(session_id="session-a")
    memory.set_goal("finish the report")
    memory.add_task("task-1", "collect evidence")

    restored = WorkingMemory.from_persisted_dict(
        memory.to_dict(),
        expected_session_id="session-a",
    )

    assert restored.to_dict() == memory.to_dict()


def test_settled_working_memory_is_hidden_and_can_be_archived() -> None:
    memory = WorkingMemory(session_id="session-a")
    memory.set_goal("finish the report")
    memory.add_task("task-1", "collect evidence")
    assert memory.has_active_job() is True
    assert bounded_working_memory_context(memory) is not None

    memory.update_task("task-1", TaskStatus.COMPLETED, result="done")
    assert memory.has_active_job() is False
    assert bounded_working_memory_context(memory) is None
    assert memory.archive_if_settled() is True
    assert memory.goal is None
    assert memory.tasks == []


def test_goal_only_working_memory_stays_active() -> None:
    memory = WorkingMemory(session_id="session-a")
    memory.set_goal("keep reviewing the contract")
    assert memory.has_active_job() is True
    assert memory.archive_if_settled() is False
    assert memory.goal == "keep reviewing the contract"


def test_persisted_working_memory_rejects_wrong_session_after_restore_helper() -> None:
    memory = WorkingMemory(session_id="session-a")
    memory.set_goal("finish the report")
    with pytest.raises(ValueError, match="session mismatch"):
        WorkingMemory.from_persisted_dict(
            memory.to_dict(),
            expected_session_id="session-b",
        )


def test_persisted_working_memory_rejects_unbounded_or_ambiguous_state():
    memory = WorkingMemory(session_id="session-a").to_dict()
    memory["tasks"] = [
        {
            "id": "duplicate",
            "description": "task",
            "status": "pending",
            "result": None,
            "error": None,
            "created_at": datetime.now().isoformat(),
            "completed_at": None,
        },
        {
            "id": "duplicate",
            "description": "other task",
            "status": "pending",
            "result": None,
            "error": None,
            "created_at": datetime.now().isoformat(),
            "completed_at": None,
        },
    ]

    with pytest.raises(ValueError, match="duplicate task ids"):
        WorkingMemory.from_persisted_dict(
            memory,
            expected_session_id="session-a",
        )

    memory["tasks"] = []
    memory["notes"] = ["x" * 1_001]
    with pytest.raises(ValueError, match="note is invalid"):
        WorkingMemory.from_persisted_dict(
            memory,
            expected_session_id="session-a",
        )


@pytest.mark.asyncio
async def test_working_memory_persistence_has_scoped_honest_receipts():
    service = _WorkingStateMemoryService()
    memory = WorkingMemory(session_id="session-a")
    memory.set_goal("resume safely")
    memory.add_task("task-1", "read back external state")

    assert await persist_working_memory(
        service,
        tenant_id="tenant-a",
        user_id="user-a",
        session_id="session-a",
        memory=memory,
    )
    assert working_memory_key(
        tenant_id="tenant-a",
        user_id="user-a",
        session_id="session-a",
    ).startswith("working_memory:")
    assert [call["key"] for call in service.calls] == [
        working_memory_key(
            tenant_id="tenant-a",
            user_id="user-a",
            session_id="session-a",
        ),
        LEGACY_WORKING_MEMORY_KEY,
    ]
    assert service.calls[0]["metadata"] == {
        "schema_version": "assistant-working-memory/v2",
        "scope": "tenant_user_session",
        "owner_scope": service.calls[0]["metadata"]["owner_scope"],
        "source": "assistant_working_memory",
    }
    assert service.payloads[LEGACY_WORKING_MEMORY_KEY] == memory.to_dict()
    restored = await restore_working_memory(
        service,
        tenant_id="tenant-a",
        user_id="user-a",
        session_id="session-a",
    )
    assert restored is not None
    assert restored.to_dict() == memory.to_dict()
    assert "read back external state" in str(bounded_working_memory_context(restored))
    assert (
        await restore_working_memory(
            service,
            tenant_id="tenant-a",
            user_id="user-b",
            session_id="session-a",
        )
        is None
    )

    service.fail_keys.add(
        working_memory_key(
            tenant_id="tenant-a",
            user_id="user-a",
            session_id="session-a",
        )
    )
    assert not await persist_working_memory(
        service,
        tenant_id="tenant-a",
        user_id="user-a",
        session_id="session-a",
        memory=memory,
    )


def test_working_memory_key_uses_collision_safe_length_delimited_scope():
    first = working_memory_key(
        tenant_id="tenant\0nested",
        user_id="user",
        session_id="session",
    )
    delimiter_collision = working_memory_key(
        tenant_id="tenant",
        user_id="nested\0user",
        session_id="session",
    )

    assert first != delimiter_collision


@pytest.mark.asyncio
async def test_working_memory_v2_priority_and_owner_proven_legacy_fallback():
    service = _WorkingStateMemoryService()
    legacy = WorkingMemory(session_id="session-a")
    legacy.set_goal("legacy private goal")
    service.payloads[LEGACY_WORKING_MEMORY_KEY] = legacy.to_dict()

    assert (
        await restore_working_memory(
            service,
            tenant_id="tenant-a",
            user_id="user-a",
            session_id="session-a",
        )
        is None
    )
    restored_legacy = await restore_working_memory(
        service,
        tenant_id="tenant-a",
        user_id="user-a",
        session_id="session-a",
        legacy_owner_verified=True,
    )
    assert restored_legacy is not None
    assert restored_legacy.goal == "legacy private goal"

    current = WorkingMemory(session_id="session-a")
    current.set_goal("v2 current goal")
    assert await persist_working_memory(
        service,
        tenant_id="tenant-a",
        user_id="user-a",
        session_id="session-a",
        memory=current,
    )
    # A stale legacy consumer row cannot override a valid v2 envelope.
    service.payloads[LEGACY_WORKING_MEMORY_KEY] = legacy.to_dict()
    restored_v2 = await restore_working_memory(
        service,
        tenant_id="tenant-a",
        user_id="user-a",
        session_id="session-a",
        legacy_owner_verified=True,
    )
    assert restored_v2 is not None
    assert restored_v2.goal == "v2 current goal"

    v2_key = working_memory_key(
        tenant_id="tenant-a",
        user_id="user-a",
        session_id="session-a",
    )
    assert isinstance(service.payloads[v2_key], dict)
    service.payloads[v2_key]["schema_version"] = "malformed"  # type: ignore[index]
    with pytest.raises(ValueError, match="schema is unsupported"):
        await restore_working_memory(
            service,
            tenant_id="tenant-a",
            user_id="user-a",
            session_id="session-a",
            legacy_owner_verified=True,
        )


@pytest.mark.asyncio
async def test_working_memory_dual_write_receipts_prevent_legacy_tearing():
    service = _WorkingStateMemoryService()
    v2_key = working_memory_key(
        tenant_id="tenant-a",
        user_id="user-a",
        session_id="session-a",
    )
    stale = WorkingMemory(session_id="session-a")
    stale.set_goal("stale")
    service.payloads[v2_key] = {"stale": True}
    service.payloads[LEGACY_WORKING_MEMORY_KEY] = stale.to_dict()

    updated = WorkingMemory(session_id="session-a")
    updated.set_goal("updated")
    service.fail_keys.add(v2_key)
    assert not await persist_working_memory(
        service,
        tenant_id="tenant-a",
        user_id="user-a",
        session_id="session-a",
        memory=updated,
    )
    assert [call["key"] for call in service.calls] == [v2_key]
    assert service.payloads[LEGACY_WORKING_MEMORY_KEY] == stale.to_dict()

    service.calls.clear()
    service.fail_keys = {LEGACY_WORKING_MEMORY_KEY}
    assert not await persist_working_memory(
        service,
        tenant_id="tenant-a",
        user_id="user-a",
        session_id="session-a",
        memory=updated,
    )
    assert [call["key"] for call in service.calls] == [
        v2_key,
        LEGACY_WORKING_MEMORY_KEY,
    ]
    restored = await restore_working_memory(
        service,
        tenant_id="tenant-a",
        user_id="user-a",
        session_id="session-a",
        legacy_owner_verified=True,
    )
    assert restored is not None
    assert restored.goal == "updated"


# =============================================================================
# TaskStatus Tests
# =============================================================================


class TestTaskStatus:
    """Test TaskStatus enum."""

    def test_all_status_values(self):
        """Test all status values are strings."""
        assert TaskStatus.PENDING.value == "pending"
        assert TaskStatus.IN_PROGRESS.value == "in_progress"
        assert TaskStatus.COMPLETED.value == "completed"
        assert TaskStatus.FAILED.value == "failed"
        assert TaskStatus.BLOCKED.value == "blocked"

    def test_status_from_string(self):
        """Test creating status from string value."""
        assert TaskStatus("pending") == TaskStatus.PENDING
        assert TaskStatus("in_progress") == TaskStatus.IN_PROGRESS
        assert TaskStatus("completed") == TaskStatus.COMPLETED


# =============================================================================
# TaskItem Tests
# =============================================================================


class TestTaskItem:
    """Test TaskItem dataclass."""

    def test_minimal_creation(self):
        """Test creating TaskItem with required fields only."""
        task = TaskItem(id="task_1", description="Test task")

        assert task.id == "task_1"
        assert task.description == "Test task"
        assert task.status == TaskStatus.PENDING
        assert task.result is None
        assert task.error is None
        assert task.completed_at is None
        assert isinstance(task.created_at, datetime)

    def test_full_creation(self):
        """Test creating TaskItem with all fields."""
        now = datetime.now()
        task = TaskItem(
            id="task_1",
            description="Test task",
            status=TaskStatus.COMPLETED,
            result="Success",
            error=None,
            created_at=now,
            completed_at=now,
        )

        assert task.status == TaskStatus.COMPLETED
        assert task.result == "Success"
        assert task.completed_at == now

    def test_to_dict(self):
        """Test serialization to dictionary."""
        task = TaskItem(id="task_1", description="Test task")
        data = task.to_dict()

        assert data["id"] == "task_1"
        assert data["description"] == "Test task"
        assert data["status"] == "pending"
        assert data["result"] is None
        assert "created_at" in data

    def test_from_dict(self):
        """Test deserialization from dictionary."""
        data = {
            "id": "task_1",
            "description": "Test task",
            "status": "completed",
            "result": "Done",
            "error": None,
            "created_at": "2024-01-01T12:00:00",
            "completed_at": "2024-01-01T13:00:00",
        }
        task = TaskItem.from_dict(data)

        assert task.id == "task_1"
        assert task.status == TaskStatus.COMPLETED
        assert task.result == "Done"
        assert task.completed_at is not None


# =============================================================================
# CollectedInfo Tests
# =============================================================================


class TestCollectedInfo:
    """Test CollectedInfo dataclass."""

    def test_creation(self):
        """Test creating CollectedInfo."""
        info = CollectedInfo(key="price", value="$100", source="kb_search")

        assert info.key == "price"
        assert info.value == "$100"
        assert info.source == "kb_search"
        assert isinstance(info.timestamp, datetime)

    def test_to_dict(self):
        """Test serialization."""
        info = CollectedInfo(key="price", value="$100", source="kb_search")
        data = info.to_dict()

        assert data["key"] == "price"
        assert data["value"] == "$100"
        assert data["source"] == "kb_search"
        assert "timestamp" in data

    def test_from_dict(self):
        """Test deserialization."""
        data = {
            "key": "price",
            "value": "$100",
            "source": "kb_search",
            "timestamp": "2024-01-01T12:00:00",
        }
        info = CollectedInfo.from_dict(data)

        assert info.key == "price"
        assert info.value == "$100"


# =============================================================================
# WorkingMemory Tests
# =============================================================================


class TestWorkingMemoryInit:
    """Test WorkingMemory initialization."""

    def test_init(self):
        """Test basic initialization."""
        memory = WorkingMemory(session_id="session_123")

        assert memory.session_id == "session_123"
        assert memory.goal is None
        assert memory.tasks == []
        assert memory.collected_info == []
        assert memory.notes == []


class TestWorkingMemoryTasks:
    """Test WorkingMemory task management."""

    def test_set_goal(self):
        """Test setting goal."""
        memory = WorkingMemory(session_id="s1")
        memory.set_goal("Complete the project")

        assert memory.goal == "Complete the project"

    def test_add_task(self):
        """Test adding tasks."""
        memory = WorkingMemory(session_id="s1")

        task1 = memory.add_task("t1", "First task")
        memory.add_task("t2", "Second task")

        assert len(memory.tasks) == 2
        assert task1.id == "t1"
        assert task1.description == "First task"
        assert task1.status == TaskStatus.PENDING

    def test_update_task_status(self):
        """Test updating task status."""
        memory = WorkingMemory(session_id="s1")
        memory.add_task("t1", "Test task")

        result = memory.update_task("t1", TaskStatus.IN_PROGRESS)
        assert result is True
        assert memory.tasks[0].status == TaskStatus.IN_PROGRESS

    def test_update_task_with_result(self):
        """Test updating task with result."""
        memory = WorkingMemory(session_id="s1")
        memory.add_task("t1", "Test task")

        memory.update_task("t1", TaskStatus.COMPLETED, result="Task done successfully")

        assert memory.tasks[0].status == TaskStatus.COMPLETED
        assert memory.tasks[0].result == "Task done successfully"
        assert memory.tasks[0].completed_at is not None

    def test_update_task_result_truncation(self):
        """Test that long results are truncated."""
        memory = WorkingMemory(session_id="s1")
        memory.add_task("t1", "Test task")

        long_result = "x" * 1000
        memory.update_task("t1", TaskStatus.COMPLETED, result=long_result)

        assert len(memory.tasks[0].result) == 500

    def test_update_task_with_error(self):
        """Test updating task with error."""
        memory = WorkingMemory(session_id="s1")
        memory.add_task("t1", "Test task")

        memory.update_task("t1", TaskStatus.FAILED, error="Connection timeout")

        assert memory.tasks[0].status == TaskStatus.FAILED
        assert memory.tasks[0].error == "Connection timeout"

    def test_update_nonexistent_task(self):
        """Test updating nonexistent task returns False."""
        memory = WorkingMemory(session_id="s1")

        result = memory.update_task("nonexistent", TaskStatus.COMPLETED)
        assert result is False

    def test_get_task(self):
        """Test getting task by ID."""
        memory = WorkingMemory(session_id="s1")
        memory.add_task("t1", "Task one")
        memory.add_task("t2", "Task two")

        task = memory.get_task("t2")
        assert task is not None
        assert task.description == "Task two"

    def test_get_nonexistent_task(self):
        """Test getting nonexistent task returns None."""
        memory = WorkingMemory(session_id="s1")

        task = memory.get_task("nonexistent")
        assert task is None

    def test_get_current_task(self):
        """Test getting current (in-progress) task."""
        memory = WorkingMemory(session_id="s1")
        memory.add_task("t1", "Task one")
        memory.add_task("t2", "Task two")
        memory.update_task("t2", TaskStatus.IN_PROGRESS)

        current = memory.get_current_task()
        assert current is not None
        assert current.id == "t2"

    def test_get_current_task_none_in_progress(self):
        """Test getting current task when none in progress."""
        memory = WorkingMemory(session_id="s1")
        memory.add_task("t1", "Task one")

        current = memory.get_current_task()
        assert current is None


class TestWorkingMemoryInfo:
    """Test WorkingMemory collected information."""

    def test_add_info(self):
        """Test adding collected information."""
        memory = WorkingMemory(session_id="s1")

        memory.add_info("price_a", "$1000", source="kb_search")
        memory.add_info("price_b", "$1200", source="kb_search")

        assert len(memory.collected_info) == 2
        assert memory.collected_info[0].key == "price_a"
        assert memory.collected_info[0].value == "$1000"
        assert memory.collected_info[0].source == "kb_search"


class TestWorkingMemoryNotes:
    """Test WorkingMemory notes."""

    def test_add_note(self):
        """Test adding notes."""
        memory = WorkingMemory(session_id="s1")

        memory.add_note("User prefers table format")
        memory.add_note("Consider adding charts")

        assert len(memory.notes) == 2
        assert memory.notes[0] == "User prefers table format"


class TestWorkingMemoryMarkdown:
    """Test WorkingMemory markdown rendering."""

    def test_to_markdown_minimal(self):
        """Test markdown with minimal content."""
        memory = WorkingMemory(session_id="s1")

        md = memory.to_markdown()

        assert "# Current Task State" in md

    def test_to_markdown_with_goal(self):
        """Test markdown includes goal."""
        memory = WorkingMemory(session_id="s1")
        memory.set_goal("Complete the project")

        md = memory.to_markdown()

        assert "**Goal:** Complete the project" in md

    def test_to_markdown_with_tasks(self):
        """Test markdown includes tasks with status indicators."""
        memory = WorkingMemory(session_id="s1")
        memory.add_task("t1", "First task")
        memory.add_task("t2", "Second task")
        memory.add_task("t3", "Third task")
        memory.update_task("t1", TaskStatus.COMPLETED)
        memory.update_task("t2", TaskStatus.IN_PROGRESS)

        md = memory.to_markdown()

        assert "## Tasks" in md
        assert "[x] First task" in md
        assert "[~] Second task <- current" in md
        assert "[ ] Third task" in md

    def test_to_markdown_with_failed_task(self):
        """Test markdown shows error for failed tasks."""
        memory = WorkingMemory(session_id="s1")
        memory.add_task("t1", "Failed task")
        memory.update_task("t1", TaskStatus.FAILED, error="Network error")

        md = memory.to_markdown()

        assert "[!] Failed task (error: Network error)" in md

    def test_to_markdown_with_collected_info(self):
        """Test markdown includes collected information."""
        memory = WorkingMemory(session_id="s1")
        memory.add_info("price", "$1000", source="kb")

        md = memory.to_markdown()

        assert "## Collected Information" in md
        assert "**price**: $1000" in md

    def test_to_markdown_truncates_long_values(self):
        """Test markdown truncates long collected info values."""
        memory = WorkingMemory(session_id="s1")
        long_value = "x" * 200
        memory.add_info("data", long_value, source="kb")

        md = memory.to_markdown()

        assert "..." in md
        # Should be truncated to 100 chars + "..."
        assert ("x" * 100 + "...") in md

    def test_to_markdown_with_notes(self):
        """Test markdown includes notes."""
        memory = WorkingMemory(session_id="s1")
        memory.add_note("Important observation")

        md = memory.to_markdown()

        assert "## Notes" in md
        assert "- Important observation" in md

    def test_to_markdown_full_example(self):
        """Test complete markdown output."""
        memory = WorkingMemory(session_id="s1")
        memory.set_goal("Generate product comparison report")
        memory.add_task("t1", "Search Product A specs")
        memory.add_task("t2", "Search Product B specs")
        memory.add_task("t3", "Generate comparison")
        memory.update_task("t1", TaskStatus.COMPLETED, result="Found specs")
        memory.update_task("t2", TaskStatus.IN_PROGRESS)
        memory.add_info("product_a_price", "$1000", source="kb_search")
        memory.add_note("User wants table format")

        md = memory.to_markdown()

        assert "# Current Task State" in md
        assert "**Goal:** Generate product comparison report" in md
        assert "## Tasks" in md
        assert "[x] Search Product A specs" in md
        assert "[~] Search Product B specs <- current" in md
        assert "[ ] Generate comparison" in md
        assert "## Collected Information" in md
        assert "**product_a_price**: $1000" in md
        assert "## Notes" in md
        assert "- User wants table format" in md


class TestWorkingMemoryProgress:
    """Test WorkingMemory progress tracking."""

    def test_progress_empty(self):
        """Test progress with no tasks."""
        memory = WorkingMemory(session_id="s1")

        progress = memory.get_progress()

        assert progress["total"] == 0
        assert progress["completed"] == 0
        assert progress["failed"] == 0
        assert progress["percentage"] == 0

    def test_progress_partial(self):
        """Test progress with partial completion."""
        memory = WorkingMemory(session_id="s1")
        memory.add_task("t1", "Task 1")
        memory.add_task("t2", "Task 2")
        memory.add_task("t3", "Task 3")
        memory.add_task("t4", "Task 4")
        memory.update_task("t1", TaskStatus.COMPLETED)
        memory.update_task("t2", TaskStatus.COMPLETED)
        memory.update_task("t3", TaskStatus.FAILED)

        progress = memory.get_progress()

        assert progress["total"] == 4
        assert progress["completed"] == 2
        assert progress["failed"] == 1
        assert progress["percentage"] == 50.0

    def test_progress_all_complete(self):
        """Test progress with all tasks complete."""
        memory = WorkingMemory(session_id="s1")
        memory.add_task("t1", "Task 1")
        memory.add_task("t2", "Task 2")
        memory.update_task("t1", TaskStatus.COMPLETED)
        memory.update_task("t2", TaskStatus.COMPLETED)

        progress = memory.get_progress()

        assert progress["percentage"] == 100.0


class TestWorkingMemoryClear:
    """Test WorkingMemory clear functionality."""

    def test_clear(self):
        """Test clearing all working memory state."""
        memory = WorkingMemory(session_id="s1")
        memory.set_goal("Test goal")
        memory.add_task("t1", "Task 1")
        memory.add_info("key", "value", "source")
        memory.add_note("Note")

        memory.clear()

        assert memory.goal is None
        assert memory.tasks == []
        assert memory.collected_info == []
        assert memory.notes == []
        # session_id should remain
        assert memory.session_id == "s1"


class TestWorkingMemorySerialization:
    """Test WorkingMemory serialization."""

    def test_to_dict(self):
        """Test serialization to dictionary."""
        memory = WorkingMemory(session_id="s1")
        memory.set_goal("Test goal")
        memory.add_task("t1", "Task 1")
        memory.add_info("key", "value", "source")
        memory.add_note("Note")

        data = memory.to_dict()

        assert data["session_id"] == "s1"
        assert data["goal"] == "Test goal"
        assert len(data["tasks"]) == 1
        assert len(data["collected_info"]) == 1
        assert data["notes"] == ["Note"]

    def test_from_dict(self):
        """Test deserialization from dictionary."""
        data = {
            "session_id": "s1",
            "goal": "Test goal",
            "tasks": [
                {
                    "id": "t1",
                    "description": "Task 1",
                    "status": "completed",
                    "result": None,
                    "error": None,
                    "created_at": "2024-01-01T12:00:00",
                    "completed_at": "2024-01-01T13:00:00",
                }
            ],
            "collected_info": [
                {
                    "key": "price",
                    "value": "$100",
                    "source": "kb",
                    "timestamp": "2024-01-01T12:00:00",
                }
            ],
            "notes": ["Test note"],
        }

        memory = WorkingMemory.from_dict(data)

        assert memory.session_id == "s1"
        assert memory.goal == "Test goal"
        assert len(memory.tasks) == 1
        assert memory.tasks[0].status == TaskStatus.COMPLETED
        assert len(memory.collected_info) == 1
        assert memory.notes == ["Test note"]

    def test_round_trip(self):
        """Test serialization round-trip preserves data."""
        memory = WorkingMemory(session_id="s1")
        memory.set_goal("Test goal")
        memory.add_task("t1", "Task 1")
        memory.update_task("t1", TaskStatus.COMPLETED, result="Done")
        memory.add_info("key", "value", "source")
        memory.add_note("Note")

        # Serialize and deserialize
        data = memory.to_dict()
        restored = WorkingMemory.from_dict(data)

        assert restored.session_id == memory.session_id
        assert restored.goal == memory.goal
        assert len(restored.tasks) == len(memory.tasks)
        assert restored.tasks[0].status == memory.tasks[0].status
        assert len(restored.collected_info) == len(memory.collected_info)
        assert restored.notes == memory.notes
