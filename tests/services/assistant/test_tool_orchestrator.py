"""
Tool Orchestrator Tests

Comprehensive tests for the ToolOrchestrator module including:
- ToolExecutionResult dataclass
- ToolOrchestrator class with parallel execution
- Parameter resolution with ${task_id.field} references
- Semaphore-based concurrency limiting
- Working memory updates
- Error handling and edge cases
"""

import asyncio
import pytest
from unittest.mock import AsyncMock, MagicMock, patch
from dataclasses import dataclass

from src.services.assistant.tool_orchestrator import (
    ToolExecutionResult,
    ToolOrchestrator,
    create_tool_orchestrator,
)
from src.services.assistant.task_planner import (
    TaskType,
    PlannedTask,
    ExecutionPlan,
)
from src.services.assistant.working_memory import (
    WorkingMemory,
    TaskStatus,
)


# =============================================================================
# Test Fixtures
# =============================================================================


@pytest.fixture
def mock_tool_result():
    """Create a mock tool call result."""
    result = MagicMock()
    result.success = True
    result.result = {"data": "test_result"}
    result.error = None
    return result


@pytest.fixture
def mock_tool_registry(mock_tool_result):
    """Create a mock tool registry."""
    registry = MagicMock()
    registry.execute = AsyncMock(return_value=mock_tool_result)
    return registry


@pytest.fixture
def working_memory():
    """Create a fresh working memory instance."""
    return WorkingMemory(session_id="test_session")


@pytest.fixture
def orchestrator(mock_tool_registry):
    """Create an orchestrator with mock registry."""
    return ToolOrchestrator(
        tool_registry=mock_tool_registry,
        max_parallel=3,
    )


@pytest.fixture
def simple_plan():
    """Create a simple execution plan with one task."""
    task = PlannedTask(
        id="task_1",
        type=TaskType.RETRIEVE,
        tool="kb_search",
        description="Search for information",
        parameters={"query": "test query"},
    )
    return ExecutionPlan(
        goal="Simple search",
        tasks=[task],
        parallel_groups=[["task_1"]],
    )


@pytest.fixture
def sequential_plan():
    """Create a plan with sequential tasks."""
    tasks = [
        PlannedTask(
            id="search_1",
            type=TaskType.RETRIEVE,
            tool="kb_search",
            description="Search KB",
            parameters={"query": "products"},
        ),
        PlannedTask(
            id="analyze_1",
            type=TaskType.ANALYZE,
            tool="analyze",
            description="Analyze results",
            parameters={"input": "${search_1.result}"},
            dependencies={"search_1"},
        ),
        PlannedTask(
            id="generate_1",
            type=TaskType.GENERATE,
            tool="generate_text",
            description="Generate response",
            parameters={"context": "${analyze_1.result}"},
            dependencies={"analyze_1"},
        ),
    ]
    return ExecutionPlan(
        goal="Search, analyze, and generate",
        tasks=tasks,
        parallel_groups=[["search_1"], ["analyze_1"], ["generate_1"]],
    )


@pytest.fixture
def parallel_plan():
    """Create a plan with parallel tasks."""
    tasks = [
        PlannedTask(
            id="search_a",
            type=TaskType.RETRIEVE,
            tool="kb_search",
            description="Search A",
            parameters={"query": "product A"},
        ),
        PlannedTask(
            id="search_b",
            type=TaskType.RETRIEVE,
            tool="kb_search",
            description="Search B",
            parameters={"query": "product B"},
        ),
        PlannedTask(
            id="compare",
            type=TaskType.ANALYZE,
            tool="analyze",
            description="Compare products",
            parameters={
                "data_a": "${search_a.result}",
                "data_b": "${search_b.result}",
            },
            dependencies={"search_a", "search_b"},
        ),
    ]
    return ExecutionPlan(
        goal="Compare products A and B",
        tasks=tasks,
        parallel_groups=[["search_a", "search_b"], ["compare"]],
    )


# =============================================================================
# ToolExecutionResult Tests
# =============================================================================


class TestToolExecutionResult:
    """Test ToolExecutionResult dataclass."""

    def test_minimal_creation(self):
        """Test creating result with minimal fields."""
        result = ToolExecutionResult(
            task_id="task_1",
            tool="kb_search",
            success=True,
        )

        assert result.task_id == "task_1"
        assert result.tool == "kb_search"
        assert result.success is True
        assert result.result is None
        assert result.error is None
        assert result.duration_ms == 0

    def test_full_creation_success(self):
        """Test creating a successful result with all fields."""
        result = ToolExecutionResult(
            task_id="task_1",
            tool="kb_search",
            success=True,
            result={"documents": ["doc1", "doc2"]},
            duration_ms=150.5,
        )

        assert result.success is True
        assert result.result == {"documents": ["doc1", "doc2"]}
        assert result.error is None
        assert result.duration_ms == 150.5

    def test_full_creation_failure(self):
        """Test creating a failed result with error."""
        result = ToolExecutionResult(
            task_id="task_1",
            tool="kb_search",
            success=False,
            error="Connection timeout",
            duration_ms=30000,
        )

        assert result.success is False
        assert result.result is None
        assert result.error == "Connection timeout"
        assert result.duration_ms == 30000

    def test_to_dict(self):
        """Test serialization to dictionary."""
        result = ToolExecutionResult(
            task_id="task_1",
            tool="analyze",
            success=True,
            result={"score": 0.95},
            duration_ms=250.0,
        )

        data = result.to_dict()

        assert data["task_id"] == "task_1"
        assert data["tool"] == "analyze"
        assert data["success"] is True
        assert data["result"] == {"score": 0.95}
        assert data["error"] is None
        assert data["duration_ms"] == 250.0

    def test_from_dict(self):
        """Test deserialization from dictionary."""
        data = {
            "task_id": "task_2",
            "tool": "generate_text",
            "success": False,
            "result": None,
            "error": "API error",
            "duration_ms": 500.0,
        }

        result = ToolExecutionResult.from_dict(data)

        assert result.task_id == "task_2"
        assert result.tool == "generate_text"
        assert result.success is False
        assert result.error == "API error"
        assert result.duration_ms == 500.0

    def test_round_trip_serialization(self):
        """Test serialization round-trip preserves data."""
        original = ToolExecutionResult(
            task_id="task_1",
            tool="kb_search",
            success=True,
            result={"key": "value", "nested": {"a": 1}},
            duration_ms=123.456,
        )

        data = original.to_dict()
        restored = ToolExecutionResult.from_dict(data)

        assert restored.task_id == original.task_id
        assert restored.tool == original.tool
        assert restored.success == original.success
        assert restored.result == original.result
        assert restored.error == original.error
        assert restored.duration_ms == original.duration_ms


# =============================================================================
# ToolOrchestrator Initialization Tests
# =============================================================================


class TestToolOrchestratorInit:
    """Test ToolOrchestrator initialization."""

    def test_default_initialization(self, mock_tool_registry):
        """Test initialization with default parameters."""
        orchestrator = ToolOrchestrator(mock_tool_registry)

        assert orchestrator.tool_registry is mock_tool_registry
        assert orchestrator.max_parallel == 5
        assert isinstance(orchestrator.semaphore, asyncio.Semaphore)

    def test_custom_max_parallel(self, mock_tool_registry):
        """Test initialization with custom max_parallel."""
        orchestrator = ToolOrchestrator(mock_tool_registry, max_parallel=10)

        assert orchestrator.max_parallel == 10

    def test_minimum_max_parallel(self, mock_tool_registry):
        """Test initialization with max_parallel=1."""
        orchestrator = ToolOrchestrator(mock_tool_registry, max_parallel=1)

        assert orchestrator.max_parallel == 1


# =============================================================================
# ToolOrchestrator execute_plan Tests
# =============================================================================


class TestToolOrchestratorExecutePlan:
    """Test ToolOrchestrator.execute_plan method."""

    @pytest.mark.asyncio
    async def test_execute_simple_plan(self, orchestrator, simple_plan, working_memory):
        """Test executing a simple single-task plan."""
        results = []
        async for result in orchestrator.execute_plan(simple_plan, working_memory):
            results.append(result)

        assert len(results) == 1
        assert results[0].task_id == "task_1"
        assert results[0].tool == "kb_search"
        assert results[0].success is True

    @pytest.mark.asyncio
    async def test_execute_sequential_plan(
        self, mock_tool_registry, sequential_plan, working_memory
    ):
        """Test executing a plan with sequential tasks."""
        # Setup mock to return different results for different tasks
        call_count = 0

        async def mock_execute(request):
            nonlocal call_count
            call_count += 1
            result = MagicMock()
            result.success = True
            result.result = {"step": call_count}
            result.error = None
            return result

        mock_tool_registry.execute = mock_execute
        orchestrator = ToolOrchestrator(mock_tool_registry, max_parallel=3)

        results = []
        async for result in orchestrator.execute_plan(sequential_plan, working_memory):
            results.append(result)

        assert len(results) == 3
        # Results should be in order for sequential plan
        assert results[0].task_id == "search_1"
        assert results[1].task_id == "analyze_1"
        assert results[2].task_id == "generate_1"

    @pytest.mark.asyncio
    async def test_execute_parallel_plan(
        self, mock_tool_registry, parallel_plan, working_memory
    ):
        """Test executing a plan with parallel tasks."""
        mock_tool_registry.execute = AsyncMock(
            return_value=MagicMock(success=True, result={"data": "test"}, error=None)
        )
        orchestrator = ToolOrchestrator(mock_tool_registry, max_parallel=3)

        results = []
        async for result in orchestrator.execute_plan(parallel_plan, working_memory):
            results.append(result)

        assert len(results) == 3
        # First group (search_a and search_b) should complete before compare
        task_ids = [r.task_id for r in results]
        compare_index = task_ids.index("compare")
        search_a_index = task_ids.index("search_a")
        search_b_index = task_ids.index("search_b")

        # Compare must come after both searches
        assert compare_index > search_a_index
        assert compare_index > search_b_index

    @pytest.mark.asyncio
    async def test_working_memory_updated(
        self, orchestrator, simple_plan, working_memory
    ):
        """Test that working memory is updated during execution."""
        async for _ in orchestrator.execute_plan(simple_plan, working_memory):
            pass

        # Check working memory state
        assert working_memory.goal == "Simple search"
        task = working_memory.get_task("task_1")
        assert task is not None
        assert task.status == TaskStatus.COMPLETED

    @pytest.mark.asyncio
    async def test_failed_task_updates_memory(
        self, mock_tool_registry, simple_plan, working_memory
    ):
        """Test that failed tasks update working memory with FAILED status."""
        mock_tool_registry.execute = AsyncMock(
            return_value=MagicMock(success=False, result=None, error="Tool error")
        )
        orchestrator = ToolOrchestrator(mock_tool_registry, max_parallel=3)

        results = []
        async for result in orchestrator.execute_plan(simple_plan, working_memory):
            results.append(result)

        assert results[0].success is False
        assert results[0].error == "Tool error"

        task = working_memory.get_task("task_1")
        assert task.status == TaskStatus.FAILED
        assert task.error == "Tool error"

    @pytest.mark.asyncio
    async def test_empty_plan(self, orchestrator, working_memory):
        """Test executing an empty plan."""
        empty_plan = ExecutionPlan(goal="Empty", tasks=[], parallel_groups=[])

        results = []
        async for result in orchestrator.execute_plan(empty_plan, working_memory):
            results.append(result)

        assert len(results) == 0

    @pytest.mark.asyncio
    async def test_duration_tracking(self, mock_tool_registry, simple_plan, working_memory):
        """Test that execution duration is tracked."""
        # Add a small delay to make duration measurable
        async def slow_execute(request):
            await asyncio.sleep(0.05)  # 50ms
            result = MagicMock()
            result.success = True
            result.result = {}
            result.error = None
            return result

        mock_tool_registry.execute = slow_execute
        orchestrator = ToolOrchestrator(mock_tool_registry, max_parallel=3)

        results = []
        async for result in orchestrator.execute_plan(simple_plan, working_memory):
            results.append(result)

        # Duration should be at least 50ms
        assert results[0].duration_ms >= 50


# =============================================================================
# ToolOrchestrator _execute_parallel Tests
# =============================================================================


class TestToolOrchestratorExecuteParallel:
    """Test ToolOrchestrator._execute_parallel method."""

    @pytest.mark.asyncio
    async def test_parallel_execution_respects_semaphore(self, mock_tool_registry):
        """Test that semaphore limits concurrent executions."""
        concurrent_count = 0
        max_concurrent = 0
        lock = asyncio.Lock()

        async def counting_execute(request):
            nonlocal concurrent_count, max_concurrent
            async with lock:
                concurrent_count += 1
                max_concurrent = max(max_concurrent, concurrent_count)

            await asyncio.sleep(0.1)  # Simulate work

            async with lock:
                concurrent_count -= 1

            result = MagicMock()
            result.success = True
            result.result = {}
            result.error = None
            return result

        mock_tool_registry.execute = counting_execute
        orchestrator = ToolOrchestrator(mock_tool_registry, max_parallel=2)

        # Create 5 parallel tasks
        tasks = [
            PlannedTask(
                id=f"task_{i}",
                type=TaskType.RETRIEVE,
                tool="kb_search",
                description=f"Task {i}",
            )
            for i in range(5)
        ]

        working_memory = WorkingMemory(session_id="test")

        results = []
        async for result in orchestrator._execute_parallel(tasks, {}, working_memory):
            results.append(result)

        assert len(results) == 5
        # Max concurrent should not exceed semaphore limit
        assert max_concurrent <= 2

    @pytest.mark.asyncio
    async def test_results_yielded_as_completed(self, mock_tool_registry):
        """Test that results are yielded as tasks complete, not in order."""
        completion_order = []

        async def variable_delay_execute(request):
            # Extract task number from call_id (which we can't control)
            # Instead, use tool arguments to determine delay
            delay = request.arguments.get("delay", 0.1)
            await asyncio.sleep(delay)

            result = MagicMock()
            result.success = True
            result.result = {"delay": delay}
            result.error = None
            return result

        mock_tool_registry.execute = variable_delay_execute
        orchestrator = ToolOrchestrator(mock_tool_registry, max_parallel=5)

        # Create tasks with different delays
        tasks = [
            PlannedTask(
                id="slow_task",
                type=TaskType.RETRIEVE,
                tool="kb_search",
                description="Slow",
                parameters={"delay": 0.2},
            ),
            PlannedTask(
                id="fast_task",
                type=TaskType.RETRIEVE,
                tool="kb_search",
                description="Fast",
                parameters={"delay": 0.05},
            ),
        ]

        working_memory = WorkingMemory(session_id="test")

        results = []
        async for result in orchestrator._execute_parallel(tasks, {}, working_memory):
            results.append(result)
            completion_order.append(result.task_id)

        # Fast task should complete first
        assert completion_order[0] == "fast_task"
        assert completion_order[1] == "slow_task"


# =============================================================================
# ToolOrchestrator _resolve_params Tests
# =============================================================================


class TestToolOrchestratorResolveParams:
    """Test ToolOrchestrator._resolve_params method."""

    def test_no_references(self, orchestrator):
        """Test params without any references."""
        params = {"query": "test query", "count": 10}
        prior_results = {}

        resolved = orchestrator._resolve_params(params, prior_results)

        assert resolved == params

    def test_simple_result_reference(self, orchestrator):
        """Test resolving ${task_id.result} reference."""
        params = {"input": "${search_1.result}"}
        prior_results = {
            "search_1": ToolExecutionResult(
                task_id="search_1",
                tool="kb_search",
                success=True,
                result={"documents": ["doc1"]},
            )
        }

        resolved = orchestrator._resolve_params(params, prior_results)

        assert resolved["input"] == {"documents": ["doc1"]}

    def test_nested_field_reference(self, orchestrator):
        """Test resolving nested field like ${task_id.result.field}."""
        params = {"docs": "${search_1.result.documents}"}
        prior_results = {
            "search_1": ToolExecutionResult(
                task_id="search_1",
                tool="kb_search",
                success=True,
                result={"documents": ["doc1", "doc2"], "count": 2},
            )
        }

        resolved = orchestrator._resolve_params(params, prior_results)

        assert resolved["docs"] == ["doc1", "doc2"]

    def test_success_reference(self, orchestrator):
        """Test resolving ${task_id.success} reference."""
        params = {"check": "${search_1.success}"}
        prior_results = {
            "search_1": ToolExecutionResult(
                task_id="search_1",
                tool="kb_search",
                success=True,
                result={},
            )
        }

        resolved = orchestrator._resolve_params(params, prior_results)

        assert resolved["check"] is True

    def test_error_reference(self, orchestrator):
        """Test resolving ${task_id.error} reference."""
        params = {"err": "${failed_task.error}"}
        prior_results = {
            "failed_task": ToolExecutionResult(
                task_id="failed_task",
                tool="kb_search",
                success=False,
                error="Connection failed",
            )
        }

        resolved = orchestrator._resolve_params(params, prior_results)

        assert resolved["err"] == "Connection failed"

    def test_embedded_reference_in_string(self, orchestrator):
        """Test reference embedded in a larger string."""
        params = {"message": "Results from search: ${search_1.result}"}
        prior_results = {
            "search_1": ToolExecutionResult(
                task_id="search_1",
                tool="kb_search",
                success=True,
                result="found 5 documents",
            )
        }

        resolved = orchestrator._resolve_params(params, prior_results)

        assert resolved["message"] == "Results from search: found 5 documents"

    def test_multiple_references_in_string(self, orchestrator):
        """Test multiple references in one string."""
        params = {"combined": "${task_a.result} and ${task_b.result}"}
        prior_results = {
            "task_a": ToolExecutionResult(
                task_id="task_a",
                tool="search",
                success=True,
                result="A",
            ),
            "task_b": ToolExecutionResult(
                task_id="task_b",
                tool="search",
                success=True,
                result="B",
            ),
        }

        resolved = orchestrator._resolve_params(params, prior_results)

        assert resolved["combined"] == "A and B"

    def test_reference_to_unknown_task(self, orchestrator):
        """Test that references to unknown tasks remain unchanged."""
        params = {"input": "${unknown_task.result}"}
        prior_results = {}

        resolved = orchestrator._resolve_params(params, prior_results)

        # Should keep original reference if task not found
        assert resolved["input"] == "${unknown_task.result}"

    def test_reference_to_nonexistent_field(self, orchestrator):
        """Test that references to nonexistent fields return None for whole ref."""
        params = {"input": "${search_1.nonexistent}"}
        prior_results = {
            "search_1": ToolExecutionResult(
                task_id="search_1",
                tool="kb_search",
                success=True,
                result={"documents": []},
            )
        }

        resolved = orchestrator._resolve_params(params, prior_results)

        # For whole-string reference to invalid field, keeps original
        assert resolved["input"] == "${search_1.nonexistent}"

    def test_nested_dict_params(self, orchestrator):
        """Test resolving references in nested dictionaries."""
        params = {
            "outer": {
                "inner": "${search_1.result}",
                "static": "value",
            }
        }
        prior_results = {
            "search_1": ToolExecutionResult(
                task_id="search_1",
                tool="kb_search",
                success=True,
                result="nested_result",
            )
        }

        resolved = orchestrator._resolve_params(params, prior_results)

        assert resolved["outer"]["inner"] == "nested_result"
        assert resolved["outer"]["static"] == "value"

    def test_list_params_with_references(self, orchestrator):
        """Test resolving references in list parameters."""
        params = {
            "items": ["${task_1.result}", "static", "${task_2.result}"]
        }
        prior_results = {
            "task_1": ToolExecutionResult(
                task_id="task_1", tool="search", success=True, result="first"
            ),
            "task_2": ToolExecutionResult(
                task_id="task_2", tool="search", success=True, result="second"
            ),
        }

        resolved = orchestrator._resolve_params(params, prior_results)

        assert resolved["items"] == ["first", "static", "second"]

    def test_primitive_params_unchanged(self, orchestrator):
        """Test that primitive values are unchanged."""
        params = {
            "string": "text",
            "number": 42,
            "float": 3.14,
            "boolean": True,
            "null": None,
        }

        resolved = orchestrator._resolve_params(params, {})

        assert resolved == params

    def test_deeply_nested_reference(self, orchestrator):
        """Test resolving deeply nested field references."""
        params = {"value": "${search_1.result.level1.level2.level3}"}
        prior_results = {
            "search_1": ToolExecutionResult(
                task_id="search_1",
                tool="kb_search",
                success=True,
                result={
                    "level1": {
                        "level2": {
                            "level3": "deep_value"
                        }
                    }
                },
            )
        }

        resolved = orchestrator._resolve_params(params, prior_results)

        assert resolved["value"] == "deep_value"


# =============================================================================
# Error Handling Tests
# =============================================================================


class TestToolOrchestratorErrorHandling:
    """Test ToolOrchestrator error handling."""

    @pytest.mark.asyncio
    async def test_tool_execution_exception(self, mock_tool_registry, simple_plan, working_memory):
        """Test handling of exceptions during tool execution."""
        mock_tool_registry.execute = AsyncMock(
            side_effect=Exception("Unexpected error")
        )
        orchestrator = ToolOrchestrator(mock_tool_registry, max_parallel=3)

        results = []
        async for result in orchestrator.execute_plan(simple_plan, working_memory):
            results.append(result)

        assert len(results) == 1
        assert results[0].success is False
        assert "Unexpected error" in results[0].error

    @pytest.mark.asyncio
    async def test_mixed_success_and_failure(self, mock_tool_registry, working_memory):
        """Test plan with mixed successful and failed tasks."""
        call_count = 0

        async def alternating_execute(request):
            nonlocal call_count
            call_count += 1
            result = MagicMock()
            if call_count % 2 == 0:
                result.success = False
                result.error = f"Task {call_count} failed"
                result.result = None
            else:
                result.success = True
                result.result = {"count": call_count}
                result.error = None
            return result

        mock_tool_registry.execute = alternating_execute
        orchestrator = ToolOrchestrator(mock_tool_registry, max_parallel=3)

        tasks = [
            PlannedTask(id=f"task_{i}", type=TaskType.RETRIEVE, tool="search", description=f"Task {i}")
            for i in range(4)
        ]
        plan = ExecutionPlan(
            goal="Mixed results",
            tasks=tasks,
            parallel_groups=[["task_0", "task_1", "task_2", "task_3"]],
        )

        results = []
        async for result in orchestrator.execute_plan(plan, working_memory):
            results.append(result)

        assert len(results) == 4
        success_count = sum(1 for r in results if r.success)
        failure_count = sum(1 for r in results if not r.success)
        assert success_count == 2
        assert failure_count == 2

    @pytest.mark.asyncio
    async def test_empty_parallel_group(self, mock_tool_registry, working_memory):
        """Test handling of empty parallel groups in plan."""
        plan = ExecutionPlan(
            goal="Empty group",
            tasks=[
                PlannedTask(id="task_1", type=TaskType.RETRIEVE, tool="search", description="Task 1")
            ],
            parallel_groups=[[], ["task_1"], []],  # Empty groups before and after
        )

        mock_tool_registry.execute = AsyncMock(
            return_value=MagicMock(success=True, result={}, error=None)
        )
        orchestrator = ToolOrchestrator(mock_tool_registry, max_parallel=3)

        results = []
        async for result in orchestrator.execute_plan(plan, working_memory):
            results.append(result)

        assert len(results) == 1
        assert results[0].task_id == "task_1"

    @pytest.mark.asyncio
    async def test_task_id_not_in_tasks(self, mock_tool_registry, working_memory):
        """Test handling of parallel group referencing non-existent task."""
        plan = ExecutionPlan(
            goal="Missing task",
            tasks=[
                PlannedTask(id="task_1", type=TaskType.RETRIEVE, tool="search", description="Task 1")
            ],
            parallel_groups=[["task_1", "nonexistent_task"]],  # nonexistent_task not in tasks
        )

        mock_tool_registry.execute = AsyncMock(
            return_value=MagicMock(success=True, result={}, error=None)
        )
        orchestrator = ToolOrchestrator(mock_tool_registry, max_parallel=3)

        results = []
        async for result in orchestrator.execute_plan(plan, working_memory):
            results.append(result)

        # Should only execute the existing task
        assert len(results) == 1
        assert results[0].task_id == "task_1"


# =============================================================================
# Concurrency and Performance Tests
# =============================================================================


class TestToolOrchestratorConcurrency:
    """Test ToolOrchestrator concurrency behavior."""

    @pytest.mark.asyncio
    async def test_high_parallelism(self, mock_tool_registry):
        """Test with many parallel tasks."""
        mock_tool_registry.execute = AsyncMock(
            return_value=MagicMock(success=True, result={}, error=None)
        )
        orchestrator = ToolOrchestrator(mock_tool_registry, max_parallel=20)

        # Create 50 parallel tasks
        tasks = [
            PlannedTask(id=f"task_{i}", type=TaskType.RETRIEVE, tool="search", description=f"Task {i}")
            for i in range(50)
        ]
        plan = ExecutionPlan(
            goal="High parallelism",
            tasks=tasks,
            parallel_groups=[[f"task_{i}" for i in range(50)]],
        )
        working_memory = WorkingMemory(session_id="test")

        results = []
        async for result in orchestrator.execute_plan(plan, working_memory):
            results.append(result)

        assert len(results) == 50
        assert all(r.success for r in results)

    @pytest.mark.asyncio
    async def test_sequential_performance(self, mock_tool_registry):
        """Test that sequential tasks execute in order."""
        execution_order = []

        async def tracking_execute(request):
            execution_order.append(request.tool_name)
            result = MagicMock()
            result.success = True
            result.result = {}
            result.error = None
            return result

        mock_tool_registry.execute = tracking_execute
        orchestrator = ToolOrchestrator(mock_tool_registry, max_parallel=5)

        tasks = [
            PlannedTask(id="first", type=TaskType.RETRIEVE, tool="search", description="First"),
            PlannedTask(id="second", type=TaskType.ANALYZE, tool="analyze", description="Second", dependencies={"first"}),
            PlannedTask(id="third", type=TaskType.GENERATE, tool="generate", description="Third", dependencies={"second"}),
        ]
        plan = ExecutionPlan(
            goal="Sequential",
            tasks=tasks,
            parallel_groups=[["first"], ["second"], ["third"]],
        )
        working_memory = WorkingMemory(session_id="test")

        results = []
        async for result in orchestrator.execute_plan(plan, working_memory):
            results.append(result)

        # Verify sequential execution
        assert execution_order == ["search", "analyze", "generate"]


# =============================================================================
# Factory Function Tests
# =============================================================================


class TestCreateToolOrchestrator:
    """Test create_tool_orchestrator factory function."""

    def test_with_provided_registry(self, mock_tool_registry):
        """Test creating orchestrator with provided registry."""
        orchestrator = create_tool_orchestrator(
            tool_registry=mock_tool_registry,
            max_parallel=10,
        )

        assert orchestrator.tool_registry is mock_tool_registry
        assert orchestrator.max_parallel == 10

    def test_with_default_max_parallel(self, mock_tool_registry):
        """Test creating orchestrator with default max_parallel."""
        orchestrator = create_tool_orchestrator(tool_registry=mock_tool_registry)

        assert orchestrator.max_parallel == 5


# =============================================================================
# Integration Tests
# =============================================================================


class TestToolOrchestratorIntegration:
    """Integration tests for ToolOrchestrator with realistic scenarios."""

    @pytest.mark.asyncio
    async def test_comparison_workflow(self, mock_tool_registry):
        """Test a complete comparison workflow."""
        results_data = {
            "search_a": {"product": "A", "price": 100},
            "search_b": {"product": "B", "price": 150},
            "compare": {"winner": "A", "reason": "lower price"},
        }

        async def workflow_execute(request):
            # Determine which task this is based on arguments
            result = MagicMock()
            result.success = True
            result.error = None

            # Simple logic to return appropriate results
            if "product A" in str(request.arguments.get("query", "")):
                result.result = results_data["search_a"]
            elif "product B" in str(request.arguments.get("query", "")):
                result.result = results_data["search_b"]
            else:
                result.result = results_data["compare"]

            return result

        mock_tool_registry.execute = workflow_execute
        orchestrator = ToolOrchestrator(mock_tool_registry, max_parallel=3)

        tasks = [
            PlannedTask(
                id="search_a",
                type=TaskType.RETRIEVE,
                tool="kb_search",
                description="Search product A",
                parameters={"query": "product A specs"},
            ),
            PlannedTask(
                id="search_b",
                type=TaskType.RETRIEVE,
                tool="kb_search",
                description="Search product B",
                parameters={"query": "product B specs"},
            ),
            PlannedTask(
                id="compare",
                type=TaskType.ANALYZE,
                tool="compare",
                description="Compare products",
                parameters={
                    "a": "${search_a.result}",
                    "b": "${search_b.result}",
                },
                dependencies={"search_a", "search_b"},
            ),
        ]
        plan = ExecutionPlan(
            goal="Compare products A and B",
            tasks=tasks,
            parallel_groups=[["search_a", "search_b"], ["compare"]],
        )
        working_memory = WorkingMemory(session_id="comparison_test")

        results = []
        async for result in orchestrator.execute_plan(plan, working_memory):
            results.append(result)

        assert len(results) == 3
        # All tasks should succeed
        assert all(r.success for r in results)

        # Working memory should reflect completed state
        assert working_memory.goal == "Compare products A and B"
        for task in working_memory.tasks:
            assert task.status == TaskStatus.COMPLETED

    @pytest.mark.asyncio
    async def test_report_generation_workflow(self, mock_tool_registry):
        """Test a complete report generation workflow."""
        async def report_execute(request):
            result = MagicMock()
            result.success = True
            result.error = None

            if request.tool_name == "kb_search":
                result.result = {"data": ["fact1", "fact2", "fact3"]}
            elif request.tool_name == "analyze":
                result.result = {"summary": "Key findings from data"}
            else:
                result.result = {"report": "Complete report document"}

            return result

        mock_tool_registry.execute = report_execute
        orchestrator = ToolOrchestrator(mock_tool_registry, max_parallel=3)

        tasks = [
            PlannedTask(
                id="gather",
                type=TaskType.RETRIEVE,
                tool="kb_search",
                description="Gather data",
                parameters={"query": "quarterly sales"},
            ),
            PlannedTask(
                id="analyze",
                type=TaskType.ANALYZE,
                tool="analyze",
                description="Analyze data",
                parameters={"data": "${gather.result}"},
                dependencies={"gather"},
            ),
            PlannedTask(
                id="report",
                type=TaskType.GENERATE,
                tool="generate_document",
                description="Generate report",
                parameters={
                    "analysis": "${analyze.result}",
                    "raw_data": "${gather.result}",
                },
                dependencies={"analyze"},
            ),
        ]
        plan = ExecutionPlan(
            goal="Generate quarterly report",
            tasks=tasks,
            parallel_groups=[["gather"], ["analyze"], ["report"]],
        )
        working_memory = WorkingMemory(session_id="report_test")

        results = []
        async for result in orchestrator.execute_plan(plan, working_memory):
            results.append(result)

        assert len(results) == 3
        # Verify order
        assert results[0].task_id == "gather"
        assert results[1].task_id == "analyze"
        assert results[2].task_id == "report"


# =============================================================================
# Edge Cases Tests
# =============================================================================


class TestToolOrchestratorEdgeCases:
    """Test edge cases and boundary conditions."""

    @pytest.mark.asyncio
    async def test_single_task_plan(self, orchestrator, working_memory):
        """Test plan with exactly one task."""
        task = PlannedTask(
            id="only_task",
            type=TaskType.RETRIEVE,
            tool="kb_search",
            description="Single task",
        )
        plan = ExecutionPlan(
            goal="Single task",
            tasks=[task],
            parallel_groups=[["only_task"]],
        )

        results = []
        async for result in orchestrator.execute_plan(plan, working_memory):
            results.append(result)

        assert len(results) == 1
        assert results[0].task_id == "only_task"

    @pytest.mark.asyncio
    async def test_all_tasks_fail(self, mock_tool_registry, working_memory):
        """Test plan where all tasks fail."""
        mock_tool_registry.execute = AsyncMock(
            return_value=MagicMock(success=False, result=None, error="All fail")
        )
        orchestrator = ToolOrchestrator(mock_tool_registry, max_parallel=3)

        tasks = [
            PlannedTask(id=f"task_{i}", type=TaskType.RETRIEVE, tool="search", description=f"Task {i}")
            for i in range(3)
        ]
        plan = ExecutionPlan(
            goal="All fail",
            tasks=tasks,
            parallel_groups=[[f"task_{i}" for i in range(3)]],
        )

        results = []
        async for result in orchestrator.execute_plan(plan, working_memory):
            results.append(result)

        assert len(results) == 3
        assert all(not r.success for r in results)
        assert all(r.error == "All fail" for r in results)

    @pytest.mark.asyncio
    async def test_special_characters_in_params(self, mock_tool_registry, working_memory):
        """Test handling of special characters in parameters."""
        mock_tool_registry.execute = AsyncMock(
            return_value=MagicMock(success=True, result={}, error=None)
        )
        orchestrator = ToolOrchestrator(mock_tool_registry, max_parallel=3)

        task = PlannedTask(
            id="special",
            type=TaskType.RETRIEVE,
            tool="kb_search",
            description="Special chars",
            parameters={
                "query": "C++ vs C# for @enterprise: what's the difference?",
                "unicode": "Unicode test",
            },
        )
        plan = ExecutionPlan(
            goal="Special characters",
            tasks=[task],
            parallel_groups=[["special"]],
        )

        results = []
        async for result in orchestrator.execute_plan(plan, working_memory):
            results.append(result)

        assert len(results) == 1
        assert results[0].success is True

    @pytest.mark.asyncio
    async def test_very_large_result(self, mock_tool_registry, working_memory):
        """Test handling of very large result data."""
        large_result = {"data": "x" * 100000}  # 100KB of data
        mock_tool_registry.execute = AsyncMock(
            return_value=MagicMock(success=True, result=large_result, error=None)
        )
        orchestrator = ToolOrchestrator(mock_tool_registry, max_parallel=3)

        task = PlannedTask(
            id="large",
            type=TaskType.RETRIEVE,
            tool="kb_search",
            description="Large result",
        )
        plan = ExecutionPlan(
            goal="Large result",
            tasks=[task],
            parallel_groups=[["large"]],
        )

        results = []
        async for result in orchestrator.execute_plan(plan, working_memory):
            results.append(result)

        assert len(results) == 1
        assert results[0].success is True
        assert len(results[0].result["data"]) == 100000

    @pytest.mark.asyncio
    async def test_circular_reference_in_params(self, orchestrator, working_memory):
        """Test that circular references in params don't cause infinite loop."""
        # This tests the param resolution, not task dependencies
        # A param referencing itself or creating a cycle shouldn't happen in normal use
        # but the resolver should handle it gracefully

        params = {"self_ref": "${task_1.result}"}
        prior_results = {
            "task_1": ToolExecutionResult(
                task_id="task_1",
                tool="search",
                success=True,
                result="${task_1.result}",  # Intentionally contains reference
            )
        }

        # Should not hang or crash
        resolved = orchestrator._resolve_params(params, prior_results)
        # The resolved value should be the string "${task_1.result}" from the result
        assert resolved["self_ref"] == "${task_1.result}"
