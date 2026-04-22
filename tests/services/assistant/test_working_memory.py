"""
Working Memory Tests

Tests for WorkingMemory class and related components:
- TaskItem and CollectedInfo dataclasses
- WorkingMemory task management
- Markdown rendering for context injection
- Serialization/deserialization
"""

from datetime import datetime

from assistant_service.core.working_memory import (
    CollectedInfo,
    TaskItem,
    TaskStatus,
    WorkingMemory,
)

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
