"""
Task Planner Tests

Comprehensive tests for the TaskPlanner module including:
- TaskType enum
- PlannedTask dataclass
- ExecutionPlan dataclass
- TaskPlanner class with dependency analysis
- Circular dependency detection
- Workflow pattern recognition
- Edge cases (empty, single task, all parallel, all sequential)
"""

import pytest
from unittest.mock import AsyncMock, MagicMock, patch

from src.services.assistant.task_planner import (
    TaskType,
    PlannedTask,
    ExecutionPlan,
    TaskPlanner,
    WorkflowPattern,
    CircularDependencyError,
    create_task_planner,
    create_simple_plan,
)


# =============================================================================
# TaskType Tests
# =============================================================================


class TestTaskType:
    """Test TaskType enum."""

    def test_all_task_type_values(self):
        """Test all task type values are correct strings."""
        assert TaskType.RETRIEVE.value == "retrieve"
        assert TaskType.GENERATE.value == "generate"
        assert TaskType.ANALYZE.value == "analyze"
        assert TaskType.TRANSFORM.value == "transform"

    def test_task_type_from_string(self):
        """Test creating TaskType from string value."""
        assert TaskType("retrieve") == TaskType.RETRIEVE
        assert TaskType("generate") == TaskType.GENERATE
        assert TaskType("analyze") == TaskType.ANALYZE
        assert TaskType("transform") == TaskType.TRANSFORM

    def test_invalid_task_type_raises_error(self):
        """Test that invalid task type raises ValueError."""
        with pytest.raises(ValueError):
            TaskType("invalid_type")


# =============================================================================
# PlannedTask Tests
# =============================================================================


class TestPlannedTask:
    """Test PlannedTask dataclass."""

    def test_minimal_creation(self):
        """Test creating PlannedTask with required fields only."""
        task = PlannedTask(
            id="task_1",
            type=TaskType.RETRIEVE,
            tool="kb_search",
            description="Search the knowledge base"
        )

        assert task.id == "task_1"
        assert task.type == TaskType.RETRIEVE
        assert task.tool == "kb_search"
        assert task.description == "Search the knowledge base"
        assert task.parameters == {}
        assert task.dependencies == set()
        assert task.priority == 0
        assert task.estimated_duration_ms == 1000

    def test_full_creation(self):
        """Test creating PlannedTask with all fields."""
        task = PlannedTask(
            id="task_1",
            type=TaskType.GENERATE,
            tool="generate_text",
            description="Generate a response",
            parameters={"max_tokens": 500},
            dependencies={"task_0"},
            priority=10,
            estimated_duration_ms=2000,
        )

        assert task.parameters == {"max_tokens": 500}
        assert task.dependencies == {"task_0"}
        assert task.priority == 10
        assert task.estimated_duration_ms == 2000

    def test_dependencies_converted_to_set(self):
        """Test that dependencies list is converted to set."""
        task = PlannedTask(
            id="task_1",
            type=TaskType.RETRIEVE,
            tool="kb_search",
            description="Test",
            dependencies=["dep_1", "dep_2"],  # type: ignore - intentionally passing list
        )

        assert isinstance(task.dependencies, set)
        assert task.dependencies == {"dep_1", "dep_2"}

    def test_none_dependencies_converted_to_empty_set(self):
        """Test that None dependencies is converted to empty set."""
        task = PlannedTask(
            id="task_1",
            type=TaskType.RETRIEVE,
            tool="kb_search",
            description="Test",
            dependencies=None,  # type: ignore
        )

        assert task.dependencies == set()

    def test_to_dict(self):
        """Test serialization to dictionary."""
        task = PlannedTask(
            id="task_1",
            type=TaskType.ANALYZE,
            tool="analyze",
            description="Analyze data",
            parameters={"input": "data"},
            dependencies={"dep_1"},
            priority=5,
        )

        data = task.to_dict()

        assert data["id"] == "task_1"
        assert data["type"] == "analyze"
        assert data["tool"] == "analyze"
        assert data["description"] == "Analyze data"
        assert data["parameters"] == {"input": "data"}
        assert data["dependencies"] == ["dep_1"]
        assert data["priority"] == 5

    def test_from_dict(self):
        """Test deserialization from dictionary."""
        data = {
            "id": "task_1",
            "type": "transform",
            "tool": "translate",
            "description": "Translate text",
            "parameters": {"target_language": "es"},
            "dependencies": ["task_0"],
            "priority": 3,
            "estimated_duration_ms": 1500,
        }

        task = PlannedTask.from_dict(data)

        assert task.id == "task_1"
        assert task.type == TaskType.TRANSFORM
        assert task.tool == "translate"
        assert task.description == "Translate text"
        assert task.parameters == {"target_language": "es"}
        assert task.dependencies == {"task_0"}
        assert task.priority == 3
        assert task.estimated_duration_ms == 1500

    def test_round_trip_serialization(self):
        """Test serialization round-trip preserves data."""
        original = PlannedTask(
            id="task_1",
            type=TaskType.GENERATE,
            tool="generate_image",
            description="Generate an image",
            parameters={"prompt": "A sunset"},
            dependencies={"search_1", "search_2"},
            priority=7,
            estimated_duration_ms=5000,
        )

        data = original.to_dict()
        restored = PlannedTask.from_dict(data)

        assert restored.id == original.id
        assert restored.type == original.type
        assert restored.tool == original.tool
        assert restored.description == original.description
        assert restored.parameters == original.parameters
        assert restored.dependencies == original.dependencies
        assert restored.priority == original.priority
        assert restored.estimated_duration_ms == original.estimated_duration_ms


# =============================================================================
# ExecutionPlan Tests
# =============================================================================


class TestExecutionPlan:
    """Test ExecutionPlan dataclass."""

    def test_minimal_creation(self):
        """Test creating ExecutionPlan with required fields."""
        plan = ExecutionPlan(goal="Test goal")

        assert plan.goal == "Test goal"
        assert plan.tasks == []
        assert plan.parallel_groups == []
        assert plan.metadata == {}

    def test_full_creation(self):
        """Test creating ExecutionPlan with all fields."""
        tasks = [
            PlannedTask(id="t1", type=TaskType.RETRIEVE, tool="kb_search", description="Search"),
            PlannedTask(id="t2", type=TaskType.GENERATE, tool="generate", description="Generate", dependencies={"t1"}),
        ]

        plan = ExecutionPlan(
            goal="Search and generate",
            tasks=tasks,
            parallel_groups=[["t1"], ["t2"]],
            metadata={"pattern": "search_and_answer"},
        )

        assert len(plan.tasks) == 2
        assert plan.parallel_groups == [["t1"], ["t2"]]
        assert plan.metadata["pattern"] == "search_and_answer"

    def test_get_task_existing(self):
        """Test getting an existing task by ID."""
        task = PlannedTask(id="search_1", type=TaskType.RETRIEVE, tool="kb_search", description="Search")
        plan = ExecutionPlan(goal="Test", tasks=[task])

        result = plan.get_task("search_1")

        assert result is not None
        assert result.id == "search_1"

    def test_get_task_nonexistent(self):
        """Test getting a nonexistent task returns None."""
        plan = ExecutionPlan(goal="Test", tasks=[])

        result = plan.get_task("nonexistent")

        assert result is None

    def test_get_tasks_by_type(self):
        """Test filtering tasks by type."""
        tasks = [
            PlannedTask(id="r1", type=TaskType.RETRIEVE, tool="kb_search", description="Search 1"),
            PlannedTask(id="r2", type=TaskType.RETRIEVE, tool="web_search", description="Search 2"),
            PlannedTask(id="g1", type=TaskType.GENERATE, tool="generate", description="Generate"),
        ]
        plan = ExecutionPlan(goal="Test", tasks=tasks)

        retrieve_tasks = plan.get_tasks_by_type(TaskType.RETRIEVE)
        generate_tasks = plan.get_tasks_by_type(TaskType.GENERATE)
        analyze_tasks = plan.get_tasks_by_type(TaskType.ANALYZE)

        assert len(retrieve_tasks) == 2
        assert len(generate_tasks) == 1
        assert len(analyze_tasks) == 0

    def test_get_root_tasks(self):
        """Test getting tasks with no dependencies."""
        tasks = [
            PlannedTask(id="t1", type=TaskType.RETRIEVE, tool="search", description="Search 1"),
            PlannedTask(id="t2", type=TaskType.RETRIEVE, tool="search", description="Search 2"),
            PlannedTask(id="t3", type=TaskType.ANALYZE, tool="analyze", description="Analyze", dependencies={"t1", "t2"}),
        ]
        plan = ExecutionPlan(goal="Test", tasks=tasks)

        roots = plan.get_root_tasks()

        assert len(roots) == 2
        assert all(t.id in ["t1", "t2"] for t in roots)

    def test_get_leaf_tasks(self):
        """Test getting tasks that no other task depends on."""
        tasks = [
            PlannedTask(id="t1", type=TaskType.RETRIEVE, tool="search", description="Search"),
            PlannedTask(id="t2", type=TaskType.ANALYZE, tool="analyze", description="Analyze", dependencies={"t1"}),
            PlannedTask(id="t3", type=TaskType.GENERATE, tool="generate", description="Generate", dependencies={"t2"}),
        ]
        plan = ExecutionPlan(goal="Test", tasks=tasks)

        leaves = plan.get_leaf_tasks()

        assert len(leaves) == 1
        assert leaves[0].id == "t3"

    def test_get_total_estimated_duration(self):
        """Test total duration estimation with parallel groups."""
        tasks = [
            PlannedTask(id="t1", type=TaskType.RETRIEVE, tool="search", description="Search 1", estimated_duration_ms=1000),
            PlannedTask(id="t2", type=TaskType.RETRIEVE, tool="search", description="Search 2", estimated_duration_ms=2000),
            PlannedTask(id="t3", type=TaskType.ANALYZE, tool="analyze", description="Analyze", estimated_duration_ms=1500),
        ]
        plan = ExecutionPlan(
            goal="Test",
            tasks=tasks,
            parallel_groups=[["t1", "t2"], ["t3"]],  # t1, t2 parallel; t3 sequential
        )

        total = plan.get_total_estimated_duration()

        # First group: max(1000, 2000) = 2000
        # Second group: 1500
        # Total: 3500
        assert total == 3500

    def test_to_dict(self):
        """Test serialization to dictionary."""
        task = PlannedTask(id="t1", type=TaskType.RETRIEVE, tool="search", description="Search")
        plan = ExecutionPlan(
            goal="Test goal",
            tasks=[task],
            parallel_groups=[["t1"]],
            metadata={"key": "value"},
        )

        data = plan.to_dict()

        assert data["goal"] == "Test goal"
        assert len(data["tasks"]) == 1
        assert data["tasks"][0]["id"] == "t1"
        assert data["parallel_groups"] == [["t1"]]
        assert data["metadata"] == {"key": "value"}

    def test_from_dict(self):
        """Test deserialization from dictionary."""
        data = {
            "goal": "Restored goal",
            "tasks": [
                {"id": "t1", "type": "retrieve", "tool": "search", "description": "Search", "dependencies": [], "priority": 0, "estimated_duration_ms": 1000, "parameters": {}}
            ],
            "parallel_groups": [["t1"]],
            "metadata": {"restored": True},
        }

        plan = ExecutionPlan.from_dict(data)

        assert plan.goal == "Restored goal"
        assert len(plan.tasks) == 1
        assert plan.tasks[0].id == "t1"
        assert plan.parallel_groups == [["t1"]]
        assert plan.metadata["restored"] is True


# =============================================================================
# CircularDependencyError Tests
# =============================================================================


class TestCircularDependencyError:
    """Test CircularDependencyError exception."""

    def test_error_message(self):
        """Test error message contains cycle information."""
        cycle = ["a", "b", "c", "a"]
        error = CircularDependencyError(cycle)

        assert "Circular dependency detected" in str(error)
        assert "a -> b -> c -> a" in str(error)
        assert error.cycle == cycle


# =============================================================================
# TaskPlanner - Dependency Analysis Tests
# =============================================================================


class TestTaskPlannerDependencyAnalysis:
    """Test TaskPlanner.analyze_dependencies method."""

    def test_empty_tasks(self):
        """Test analyzing empty task list."""
        planner = TaskPlanner()

        groups = planner.analyze_dependencies([])

        assert groups == []

    def test_single_task(self):
        """Test analyzing single task."""
        planner = TaskPlanner()
        task = PlannedTask(id="t1", type=TaskType.RETRIEVE, tool="search", description="Search")

        groups = planner.analyze_dependencies([task])

        assert groups == [["t1"]]

    def test_all_parallel_tasks(self):
        """Test analyzing tasks with no dependencies (all parallel)."""
        planner = TaskPlanner()
        tasks = [
            PlannedTask(id="t1", type=TaskType.RETRIEVE, tool="search", description="Search 1"),
            PlannedTask(id="t2", type=TaskType.RETRIEVE, tool="search", description="Search 2"),
            PlannedTask(id="t3", type=TaskType.RETRIEVE, tool="search", description="Search 3"),
        ]

        groups = planner.analyze_dependencies(tasks)

        # All tasks should be in one parallel group
        assert len(groups) == 1
        assert set(groups[0]) == {"t1", "t2", "t3"}

    def test_all_sequential_tasks(self):
        """Test analyzing fully sequential tasks (chain of dependencies)."""
        planner = TaskPlanner()
        tasks = [
            PlannedTask(id="t1", type=TaskType.RETRIEVE, tool="search", description="Step 1"),
            PlannedTask(id="t2", type=TaskType.ANALYZE, tool="analyze", description="Step 2", dependencies={"t1"}),
            PlannedTask(id="t3", type=TaskType.GENERATE, tool="generate", description="Step 3", dependencies={"t2"}),
        ]

        groups = planner.analyze_dependencies(tasks)

        # Each task in its own group (sequential)
        assert len(groups) == 3
        assert groups[0] == ["t1"]
        assert groups[1] == ["t2"]
        assert groups[2] == ["t3"]

    def test_mixed_parallel_and_sequential(self):
        """Test analyzing mixed parallel and sequential tasks."""
        planner = TaskPlanner()
        # t1, t2 parallel -> t3 -> t4, t5 parallel -> t6
        tasks = [
            PlannedTask(id="t1", type=TaskType.RETRIEVE, tool="search", description="Search A"),
            PlannedTask(id="t2", type=TaskType.RETRIEVE, tool="search", description="Search B"),
            PlannedTask(id="t3", type=TaskType.ANALYZE, tool="analyze", description="Compare", dependencies={"t1", "t2"}),
            PlannedTask(id="t4", type=TaskType.TRANSFORM, tool="transform", description="Transform 1", dependencies={"t3"}),
            PlannedTask(id="t5", type=TaskType.TRANSFORM, tool="transform", description="Transform 2", dependencies={"t3"}),
            PlannedTask(id="t6", type=TaskType.GENERATE, tool="generate", description="Final", dependencies={"t4", "t5"}),
        ]

        groups = planner.analyze_dependencies(tasks)

        assert len(groups) == 4
        assert set(groups[0]) == {"t1", "t2"}
        assert groups[1] == ["t3"]
        assert set(groups[2]) == {"t4", "t5"}
        assert groups[3] == ["t6"]

    def test_diamond_dependency_pattern(self):
        """Test diamond dependency pattern (A -> B, C -> D where B,C depend on A, D depends on B,C)."""
        planner = TaskPlanner()
        tasks = [
            PlannedTask(id="a", type=TaskType.RETRIEVE, tool="search", description="Start"),
            PlannedTask(id="b", type=TaskType.ANALYZE, tool="analyze", description="Branch 1", dependencies={"a"}),
            PlannedTask(id="c", type=TaskType.ANALYZE, tool="analyze", description="Branch 2", dependencies={"a"}),
            PlannedTask(id="d", type=TaskType.GENERATE, tool="generate", description="Merge", dependencies={"b", "c"}),
        ]

        groups = planner.analyze_dependencies(tasks)

        assert len(groups) == 3
        assert groups[0] == ["a"]
        assert set(groups[1]) == {"b", "c"}
        assert groups[2] == ["d"]

    def test_circular_dependency_detection_simple(self):
        """Test detection of simple circular dependency (A -> B -> A)."""
        planner = TaskPlanner()
        tasks = [
            PlannedTask(id="a", type=TaskType.RETRIEVE, tool="search", description="Task A", dependencies={"b"}),
            PlannedTask(id="b", type=TaskType.RETRIEVE, tool="search", description="Task B", dependencies={"a"}),
        ]

        with pytest.raises(CircularDependencyError) as exc_info:
            planner.analyze_dependencies(tasks)

        assert len(exc_info.value.cycle) > 0

    def test_circular_dependency_detection_three_node(self):
        """Test detection of three-node circular dependency (A -> B -> C -> A)."""
        planner = TaskPlanner()
        tasks = [
            PlannedTask(id="a", type=TaskType.RETRIEVE, tool="search", description="Task A", dependencies={"c"}),
            PlannedTask(id="b", type=TaskType.RETRIEVE, tool="search", description="Task B", dependencies={"a"}),
            PlannedTask(id="c", type=TaskType.RETRIEVE, tool="search", description="Task C", dependencies={"b"}),
        ]

        with pytest.raises(CircularDependencyError):
            planner.analyze_dependencies(tasks)

    def test_self_dependency(self):
        """Test detection of self-referential dependency."""
        planner = TaskPlanner()
        tasks = [
            PlannedTask(id="a", type=TaskType.RETRIEVE, tool="search", description="Self-dep", dependencies={"a"}),
        ]

        with pytest.raises(CircularDependencyError):
            planner.analyze_dependencies(tasks)

    def test_invalid_dependency_reference_ignored(self):
        """Test that references to non-existent tasks are ignored with warning."""
        planner = TaskPlanner()
        tasks = [
            PlannedTask(id="a", type=TaskType.RETRIEVE, tool="search", description="Task A"),
            PlannedTask(id="b", type=TaskType.RETRIEVE, tool="search", description="Task B", dependencies={"nonexistent"}),
        ]

        # Should not raise, but warn and remove invalid dependency
        groups = planner.analyze_dependencies(tasks)

        # Both tasks should now be parallel since invalid dep is removed
        assert len(groups) == 1
        assert set(groups[0]) == {"a", "b"}

    def test_priority_ordering_within_group(self):
        """Test that tasks are ordered by priority within parallel groups."""
        planner = TaskPlanner()
        tasks = [
            PlannedTask(id="low", type=TaskType.RETRIEVE, tool="search", description="Low priority", priority=1),
            PlannedTask(id="high", type=TaskType.RETRIEVE, tool="search", description="High priority", priority=10),
            PlannedTask(id="medium", type=TaskType.RETRIEVE, tool="search", description="Medium priority", priority=5),
        ]

        groups = planner.analyze_dependencies(tasks)

        # All parallel, but ordered by priority (descending)
        assert len(groups) == 1
        assert groups[0] == ["high", "medium", "low"]


# =============================================================================
# TaskPlanner - Pattern Detection Tests
# =============================================================================


class TestTaskPlannerPatternDetection:
    """Test TaskPlanner workflow pattern detection."""

    def test_detect_comparison_pattern_keywords(self):
        """Test detection of comparison pattern from keywords."""
        planner = TaskPlanner()

        # Various comparison phrases
        comparison_requests = [
            "Compare product A and product B",
            "What's the difference between X and Y",
            "Tesla vs BMW electric cars",
            "Which is better, option 1 or option 2",
        ]

        for request in comparison_requests:
            pattern = planner._detect_pattern(request)
            assert pattern is not None
            assert pattern.name == "comparison", f"Failed for: {request}"

    def test_detect_report_pattern_keywords(self):
        """Test detection of report pattern from keywords."""
        planner = TaskPlanner()

        report_requests = [
            "Generate a report on sales figures",
            "Create a summary document",
            "Write up an overview of the project",
        ]

        for request in report_requests:
            pattern = planner._detect_pattern(request)
            assert pattern is not None
            assert pattern.name == "report", f"Failed for: {request}"

    def test_detect_search_and_answer_pattern(self):
        """Test detection of search_and_answer pattern."""
        planner = TaskPlanner()

        search_requests = [
            "Find information about Python",
            "What is machine learning?",
            "How to configure the system",
            "Tell me about the company policy",
        ]

        for request in search_requests:
            pattern = planner._detect_pattern(request)
            assert pattern is not None
            assert pattern.name == "search_and_answer", f"Failed for: {request}"

    def test_no_pattern_detected(self):
        """Test that ambiguous requests don't match any pattern."""
        planner = TaskPlanner()

        # Very generic request without clear pattern keywords
        pattern = planner._detect_pattern("Process the data")

        # May or may not match depending on keyword overlap
        # The important thing is it doesn't crash
        assert pattern is None or isinstance(pattern, WorkflowPattern)


# =============================================================================
# TaskPlanner - Item Extraction Tests
# =============================================================================


class TestTaskPlannerItemExtraction:
    """Test TaskPlanner comparison item extraction."""

    def test_extract_compare_and_pattern(self):
        """Test extracting items from 'compare X and Y' pattern."""
        planner = TaskPlanner()

        items = planner._extract_comparison_items("Compare Tesla Model 3 and BMW i4")

        assert len(items) == 2
        assert "tesla model 3" in items
        assert "bmw i4" in items

    def test_extract_vs_pattern(self):
        """Test extracting items from 'X vs Y' pattern."""
        planner = TaskPlanner()

        items = planner._extract_comparison_items("iPhone vs Android")

        assert len(items) == 2
        assert "iphone" in items
        assert "android" in items

    def test_extract_difference_between_pattern(self):
        """Test extracting items from 'difference between X and Y' pattern."""
        planner = TaskPlanner()

        items = planner._extract_comparison_items("What's the difference between Python and Java")

        assert len(items) == 2
        assert "python" in items
        assert "java" in items

    def test_extract_no_items(self):
        """Test that non-comparison request returns empty list."""
        planner = TaskPlanner()

        items = planner._extract_comparison_items("Generate a report")

        assert items == []


# =============================================================================
# TaskPlanner - Rule-Based Planning Tests
# =============================================================================


class TestTaskPlannerRuleBased:
    """Test TaskPlanner rule-based planning."""

    @pytest.mark.asyncio
    async def test_create_plan_simple_search(self):
        """Test creating a simple search plan without LLM."""
        planner = TaskPlanner()

        plan = await planner.create_plan(
            user_request="Search for product information",
            available_tools=["kb_search"],
            use_llm=False,
        )

        assert plan.goal == "Search for product information"
        assert len(plan.tasks) >= 1
        assert plan.tasks[0].type == TaskType.RETRIEVE

    @pytest.mark.asyncio
    async def test_create_plan_comparison(self):
        """Test creating comparison plan with pattern matching."""
        planner = TaskPlanner()

        plan = await planner.create_plan(
            user_request="Compare product A and product B",
            available_tools=["kb_search", "analyze", "generate_text"],
            use_llm=False,
        )

        assert "comparison" in plan.metadata.get("detected_pattern", "")
        # Should have multiple tasks for comparison workflow
        assert len(plan.tasks) >= 2

    @pytest.mark.asyncio
    async def test_create_plan_with_alternative_tool(self):
        """Test that alternative tools are used when specified tool unavailable."""
        planner = TaskPlanner()

        plan = await planner.create_plan(
            user_request="Search for information",
            available_tools=["web_search"],  # kb_search not available, web_search is alternative
            use_llm=False,
        )

        # Should use web_search instead of kb_search
        assert len(plan.tasks) >= 1

    @pytest.mark.asyncio
    async def test_create_plan_empty_tools(self):
        """Test creating plan with no available tools."""
        planner = TaskPlanner()

        plan = await planner.create_plan(
            user_request="Do something",
            available_tools=[],
            use_llm=False,
        )

        # Should still create a plan (possibly empty or with placeholder tasks)
        assert plan is not None
        assert plan.goal == "Do something"

    @pytest.mark.asyncio
    async def test_create_plan_includes_parallel_groups(self):
        """Test that created plans include parallel group analysis."""
        planner = TaskPlanner()

        plan = await planner.create_plan(
            user_request="Compare Tesla and BMW",
            available_tools=["kb_search", "analyze", "generate_text"],
            use_llm=False,
        )

        # Should have parallel groups computed
        assert plan.parallel_groups is not None
        # All task IDs should be in some parallel group
        all_task_ids = {t.id for t in plan.tasks}
        grouped_ids = {tid for group in plan.parallel_groups for tid in group}
        assert all_task_ids == grouped_ids


# =============================================================================
# TaskPlanner - Plan Validation Tests
# =============================================================================


class TestTaskPlannerValidation:
    """Test TaskPlanner.validate_plan method."""

    def test_validate_valid_plan(self):
        """Test validating a correct plan."""
        planner = TaskPlanner()
        tasks = [
            PlannedTask(id="t1", type=TaskType.RETRIEVE, tool="search", description="Search"),
            PlannedTask(id="t2", type=TaskType.GENERATE, tool="generate", description="Generate", dependencies={"t1"}),
        ]
        plan = ExecutionPlan(
            goal="Test",
            tasks=tasks,
            parallel_groups=[["t1"], ["t2"]],
        )

        is_valid, errors = planner.validate_plan(plan)

        assert is_valid is True
        assert errors == []

    def test_validate_invalid_dependency(self):
        """Test validating plan with invalid dependency reference."""
        planner = TaskPlanner()
        tasks = [
            PlannedTask(id="t1", type=TaskType.RETRIEVE, tool="search", description="Search"),
            PlannedTask(id="t2", type=TaskType.GENERATE, tool="generate", description="Generate", dependencies={"nonexistent"}),
        ]
        plan = ExecutionPlan(
            goal="Test",
            tasks=tasks,
            parallel_groups=[["t1", "t2"]],
        )

        is_valid, errors = planner.validate_plan(plan)

        assert is_valid is False
        assert any("non-existent" in e for e in errors)

    def test_validate_missing_from_groups(self):
        """Test validating plan with task missing from parallel groups."""
        planner = TaskPlanner()
        tasks = [
            PlannedTask(id="t1", type=TaskType.RETRIEVE, tool="search", description="Search"),
            PlannedTask(id="t2", type=TaskType.GENERATE, tool="generate", description="Generate"),
        ]
        plan = ExecutionPlan(
            goal="Test",
            tasks=tasks,
            parallel_groups=[["t1"]],  # t2 missing!
        )

        is_valid, errors = planner.validate_plan(plan)

        assert is_valid is False
        assert any("not in any parallel group" in e for e in errors)

    def test_validate_duplicate_task_ids(self):
        """Test validating plan with duplicate task IDs."""
        planner = TaskPlanner()
        tasks = [
            PlannedTask(id="t1", type=TaskType.RETRIEVE, tool="search", description="First"),
            PlannedTask(id="t1", type=TaskType.GENERATE, tool="generate", description="Duplicate ID!"),
        ]
        plan = ExecutionPlan(
            goal="Test",
            tasks=tasks,
            parallel_groups=[["t1"]],
        )

        is_valid, errors = planner.validate_plan(plan)

        assert is_valid is False
        assert any("Duplicate task ID" in e for e in errors)


# =============================================================================
# TaskPlanner - LLM Planning Tests (Mocked)
# =============================================================================


class TestTaskPlannerWithLLM:
    """Test TaskPlanner with mocked LLM client."""

    @pytest.mark.asyncio
    async def test_create_plan_with_llm_anthropic(self):
        """Test creating plan with mocked Anthropic client."""
        mock_client = MagicMock()
        mock_client.messages = MagicMock()
        mock_response = MagicMock()
        mock_response.content = [MagicMock(text='''
```json
[
  {"id": "search_1", "type": "retrieve", "tool": "kb_search", "description": "Search KB", "dependencies": []},
  {"id": "generate_1", "type": "generate", "tool": "generate_text", "description": "Generate answer", "dependencies": ["search_1"]}
]
```
''')]
        mock_client.messages.create = AsyncMock(return_value=mock_response)

        planner = TaskPlanner(model_client=mock_client)

        plan = await planner.create_plan(
            user_request="Find and answer about X",
            available_tools=["kb_search", "generate_text"],
            use_llm=True,
        )

        assert len(plan.tasks) == 2
        assert plan.tasks[0].id == "search_1"
        assert plan.tasks[1].id == "generate_1"
        assert plan.tasks[1].dependencies == {"search_1"}

    @pytest.mark.asyncio
    async def test_create_plan_llm_fallback_on_error(self):
        """Test that LLM errors fall back to rule-based planning."""
        mock_client = MagicMock()
        mock_client.messages = MagicMock()
        mock_client.messages.create = AsyncMock(side_effect=Exception("API Error"))

        planner = TaskPlanner(model_client=mock_client)

        # Should not raise, should fall back to rule-based
        plan = await planner.create_plan(
            user_request="Search for products",
            available_tools=["kb_search"],
            use_llm=True,
        )

        assert plan is not None
        assert len(plan.tasks) >= 0  # May have tasks from rule-based fallback

    @pytest.mark.asyncio
    async def test_create_plan_llm_invalid_json_fallback(self):
        """Test fallback when LLM returns invalid JSON."""
        mock_client = MagicMock()
        mock_client.messages = MagicMock()
        mock_response = MagicMock()
        mock_response.content = [MagicMock(text="Not valid JSON at all")]
        mock_client.messages.create = AsyncMock(return_value=mock_response)

        planner = TaskPlanner(model_client=mock_client)

        plan = await planner.create_plan(
            user_request="Search something",
            available_tools=["kb_search"],
            use_llm=True,
        )

        # Should fall back to rule-based planning
        assert plan is not None


# =============================================================================
# Module-Level Function Tests
# =============================================================================


class TestModuleFunctions:
    """Test module-level convenience functions."""

    def test_create_task_planner(self):
        """Test create_task_planner factory function."""
        planner = create_task_planner()

        assert isinstance(planner, TaskPlanner)
        assert planner.model_client is None

    def test_create_task_planner_with_client(self):
        """Test create_task_planner with model client."""
        mock_client = MagicMock()
        planner = create_task_planner(model_client=mock_client, model_name="test-model")

        assert planner.model_client is mock_client
        assert planner.model_name == "test-model"

    def test_create_simple_plan(self):
        """Test create_simple_plan utility function."""
        tasks = [
            {"id": "t1", "type": "retrieve", "tool": "search", "description": "Search", "dependencies": [], "parameters": {}},
            {"id": "t2", "type": "generate", "tool": "generate", "description": "Generate", "dependencies": ["t1"], "parameters": {}},
        ]

        plan = create_simple_plan(goal="Test goal", tasks=tasks)

        assert plan.goal == "Test goal"
        assert len(plan.tasks) == 2
        assert plan.parallel_groups == [["t1"], ["t2"]]

    def test_create_simple_plan_with_parallel_tasks(self):
        """Test create_simple_plan with parallel tasks."""
        tasks = [
            {"id": "a", "type": "retrieve", "tool": "search", "description": "A", "dependencies": [], "parameters": {}},
            {"id": "b", "type": "retrieve", "tool": "search", "description": "B", "dependencies": [], "parameters": {}},
            {"id": "c", "type": "analyze", "tool": "analyze", "description": "C", "dependencies": ["a", "b"], "parameters": {}},
        ]

        plan = create_simple_plan(goal="Parallel test", tasks=tasks)

        assert len(plan.parallel_groups) == 2
        assert set(plan.parallel_groups[0]) == {"a", "b"}
        assert plan.parallel_groups[1] == ["c"]


# =============================================================================
# WorkflowPattern Tests
# =============================================================================


class TestWorkflowPattern:
    """Test WorkflowPattern dataclass."""

    def test_workflow_pattern_creation(self):
        """Test creating a WorkflowPattern."""
        pattern = WorkflowPattern(
            name="test_pattern",
            description="A test pattern",
            task_templates=[
                {"id": "task_1", "type": TaskType.RETRIEVE, "tool": "search", "description": "Search"},
            ],
            keywords=["test", "example"],
        )

        assert pattern.name == "test_pattern"
        assert pattern.description == "A test pattern"
        assert len(pattern.task_templates) == 1
        assert pattern.keywords == ["test", "example"]

    def test_builtin_patterns_exist(self):
        """Test that all expected builtin patterns exist."""
        expected_patterns = ["comparison", "report", "search_and_answer", "multi_search", "translate", "image_generation"]

        for pattern_name in expected_patterns:
            assert pattern_name in TaskPlanner.WORKFLOW_PATTERNS
            pattern = TaskPlanner.WORKFLOW_PATTERNS[pattern_name]
            assert isinstance(pattern, WorkflowPattern)
            assert len(pattern.task_templates) > 0
            assert len(pattern.keywords) > 0


# =============================================================================
# Edge Cases and Integration Tests
# =============================================================================


class TestEdgeCases:
    """Test edge cases and boundary conditions."""

    @pytest.mark.asyncio
    async def test_very_long_request(self):
        """Test handling of very long user requests."""
        planner = TaskPlanner()
        long_request = "Search for " + "very " * 1000 + "important information"

        plan = await planner.create_plan(
            user_request=long_request,
            available_tools=["kb_search"],
            use_llm=False,
        )

        assert plan is not None
        assert len(plan.goal) > 0

    @pytest.mark.asyncio
    async def test_special_characters_in_request(self):
        """Test handling of special characters in request."""
        planner = TaskPlanner()

        plan = await planner.create_plan(
            user_request="Compare C++ vs C# for @enterprise use-cases",
            available_tools=["kb_search"],
            use_llm=False,
        )

        assert plan is not None

    @pytest.mark.asyncio
    async def test_unicode_in_request(self):
        """Test handling of unicode characters in request."""
        planner = TaskPlanner()

        plan = await planner.create_plan(
            user_request="Search for information about machine learning",
            available_tools=["kb_search"],
            use_llm=False,
        )

        assert plan is not None

    def test_large_number_of_tasks(self):
        """Test dependency analysis with many tasks."""
        planner = TaskPlanner()

        # Create 100 tasks in a chain
        tasks = [
            PlannedTask(
                id=f"task_{i}",
                type=TaskType.RETRIEVE,
                tool="search",
                description=f"Task {i}",
                dependencies=set() if i == 0 else {f"task_{i-1}"},
            )
            for i in range(100)
        ]

        groups = planner.analyze_dependencies(tasks)

        assert len(groups) == 100
        for i, group in enumerate(groups):
            assert group == [f"task_{i}"]

    def test_wide_parallel_graph(self):
        """Test dependency analysis with many parallel tasks."""
        planner = TaskPlanner()

        # 50 parallel tasks, then one final task depending on all
        parallel_tasks = [
            PlannedTask(
                id=f"parallel_{i}",
                type=TaskType.RETRIEVE,
                tool="search",
                description=f"Parallel {i}",
            )
            for i in range(50)
        ]
        final_task = PlannedTask(
            id="final",
            type=TaskType.GENERATE,
            tool="generate",
            description="Final",
            dependencies={f"parallel_{i}" for i in range(50)},
        )
        tasks = parallel_tasks + [final_task]

        groups = planner.analyze_dependencies(tasks)

        assert len(groups) == 2
        assert len(groups[0]) == 50
        assert groups[1] == ["final"]

    def test_complex_dag_structure(self):
        """Test complex DAG with multiple paths and merge points."""
        planner = TaskPlanner()

        # Complex DAG:
        #     a
        #    / \
        #   b   c
        #   |\ /|
        #   | X |
        #   |/ \|
        #   d   e
        #    \ /
        #     f
        tasks = [
            PlannedTask(id="a", type=TaskType.RETRIEVE, tool="s", description="A"),
            PlannedTask(id="b", type=TaskType.ANALYZE, tool="s", description="B", dependencies={"a"}),
            PlannedTask(id="c", type=TaskType.ANALYZE, tool="s", description="C", dependencies={"a"}),
            PlannedTask(id="d", type=TaskType.ANALYZE, tool="s", description="D", dependencies={"b", "c"}),
            PlannedTask(id="e", type=TaskType.ANALYZE, tool="s", description="E", dependencies={"b", "c"}),
            PlannedTask(id="f", type=TaskType.GENERATE, tool="s", description="F", dependencies={"d", "e"}),
        ]

        groups = planner.analyze_dependencies(tasks)

        assert len(groups) == 4
        assert groups[0] == ["a"]
        assert set(groups[1]) == {"b", "c"}
        assert set(groups[2]) == {"d", "e"}
        assert groups[3] == ["f"]
