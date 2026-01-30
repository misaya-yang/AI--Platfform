"""
Task Planning Integration Tests

Tests for the integration of TaskPlanner and ToolOrchestrator into AssistantService.
This covers:
- AssistantConfig with enable_task_planning and max_parallel_tools
- task_planner and tool_orchestrator property accessors
- _execute_with_planning method
- Integration into chat_stream with planning mode
"""

import pytest
from unittest.mock import AsyncMock, MagicMock, patch
from typing import AsyncIterator, List, Dict, Any

from src.services.assistant.assistant_service import (
    AssistantService,
    AssistantConfig,
    AssistantStreamEvent,
    StreamEventType,
    RetrievedContext,
)
from src.services.assistant.task_planner import (
    TaskPlanner,
    ExecutionPlan,
    PlannedTask,
    TaskType,
)
from src.services.assistant.tool_orchestrator import (
    ToolOrchestrator,
    ToolExecutionResult,
)
from src.services.assistant.working_memory import (
    WorkingMemory,
    TaskStatus,
)
from src.core.auth.user_resolver import UserContext


# =============================================================================
# Fixtures
# =============================================================================


@pytest.fixture
def mock_model_registry():
    """Create a mock model registry."""
    registry = MagicMock()
    registry.get_model.return_value = MagicMock(
        context_window=128000,
        supports_vision=True,
        supports_tools=True,
    )
    registry.get_available_models.return_value = []
    return registry


@pytest.fixture
def mock_tool_registry():
    """Create a mock tool registry."""
    registry = MagicMock()
    registry.list_tools.return_value = [
        MagicMock(name="kb_search"),
        MagicMock(name="generate_text"),
    ]
    registry.get_openai_schemas.return_value = []
    return registry


@pytest.fixture
def user_context():
    """Create a test user context."""
    return UserContext(
        tenant_id="test-tenant",
        user_id="test-user",
        roles=["user"],
    )


@pytest.fixture
def mock_context_manager():
    """Create a mock context manager that returns proper values."""
    context_manager = MagicMock()
    # Mock the process_history method to return a proper result
    context_result = MagicMock()
    context_result.messages = []
    context_result.truncated_count = 0
    context_result.original_count = 0
    context_result.total_tokens = 0
    context_manager.process_history.return_value = context_result
    return context_manager


@pytest.fixture
def assistant_service(mock_model_registry, mock_context_manager):
    """Create an AssistantService instance for testing."""
    with patch("src.services.assistant.assistant_service.get_context_manager") as mock_get_ctx:
        mock_get_ctx.return_value = mock_context_manager
        with patch("src.services.assistant.assistant_service.get_rag_evaluator"):
            with patch("src.services.assistant.assistant_service.get_artifact_storage"):
                with patch("src.services.assistant.assistant_service.create_file_processor"):
                    service = AssistantService(
                        model_registry=mock_model_registry,
                        kb_service=None,
                        tavily_api_key=None,
                        session_manager=None,
                        enable_rag_evaluation=False,
                    )
                    return service


# =============================================================================
# AssistantConfig Tests
# =============================================================================


class TestAssistantConfigTaskPlanning:
    """Test AssistantConfig task planning fields."""

    def test_default_task_planning_disabled(self):
        """Test that task planning is disabled by default."""
        config = AssistantConfig()
        assert config.enable_task_planning is False
        assert config.max_parallel_tools == 5

    def test_enable_task_planning(self):
        """Test enabling task planning."""
        config = AssistantConfig(enable_task_planning=True)
        assert config.enable_task_planning is True

    def test_custom_max_parallel_tools(self):
        """Test setting custom max parallel tools."""
        config = AssistantConfig(
            enable_task_planning=True,
            max_parallel_tools=10,
        )
        assert config.max_parallel_tools == 10

    def test_task_planning_with_other_options(self):
        """Test task planning with other config options."""
        config = AssistantConfig(
            model_id="gpt-4o",
            enable_task_planning=True,
            max_parallel_tools=3,
            kb_dataset_ids=["test-dataset"],
            web_search_enabled=True,
        )
        assert config.enable_task_planning is True
        assert config.max_parallel_tools == 3
        assert config.model_id == "gpt-4o"


# =============================================================================
# AssistantService Property Tests
# =============================================================================


class TestAssistantServiceProperties:
    """Test AssistantService task planner and orchestrator properties."""

    def test_task_planner_property_creates_instance(self, assistant_service):
        """Test that task_planner property creates instance on demand."""
        assert assistant_service._task_planner is None
        planner = assistant_service.task_planner
        assert planner is not None
        assert isinstance(planner, TaskPlanner)
        # Verify same instance is returned on second access
        assert assistant_service.task_planner is planner

    def test_task_planner_with_injected_instance(self, mock_model_registry):
        """Test that injected task planner is used."""
        custom_planner = TaskPlanner()

        with patch("src.services.assistant.assistant_service.get_context_manager"):
            with patch("src.services.assistant.assistant_service.get_rag_evaluator"):
                with patch("src.services.assistant.assistant_service.get_artifact_storage"):
                    with patch("src.services.assistant.assistant_service.create_file_processor"):
                        service = AssistantService(
                            model_registry=mock_model_registry,
                            task_planner=custom_planner,
                            enable_rag_evaluation=False,
                        )

        assert service._task_planner is custom_planner
        assert service.task_planner is custom_planner

    def test_get_tool_orchestrator_creates_instance(self, assistant_service):
        """Test that get_tool_orchestrator creates instance on demand."""
        assert assistant_service._tool_orchestrator is None

        with patch("src.services.assistant.tools.get_tool_registry") as mock_get_registry:
            mock_registry = MagicMock()
            mock_get_registry.return_value = mock_registry

            orchestrator = assistant_service.get_tool_orchestrator(max_parallel=3)

        assert orchestrator is not None
        assert isinstance(orchestrator, ToolOrchestrator)
        assert orchestrator.max_parallel == 3

    def test_get_tool_orchestrator_returns_cached(self, assistant_service):
        """Test that orchestrator is cached after first creation."""
        with patch("src.services.assistant.tools.get_tool_registry") as mock_get_registry:
            mock_registry = MagicMock()
            mock_get_registry.return_value = mock_registry

            orch1 = assistant_service.get_tool_orchestrator(max_parallel=5)
            orch2 = assistant_service.get_tool_orchestrator(max_parallel=10)  # Should be ignored

        assert orch1 is orch2  # Same instance


# =============================================================================
# _execute_with_planning Tests
# =============================================================================


class TestExecuteWithPlanning:
    """Test _execute_with_planning method."""

    @pytest.mark.asyncio
    async def test_execute_with_planning_yields_task_planning_event(
        self, assistant_service, user_context
    ):
        """Test that TASK_PLANNING event is yielded."""
        config = AssistantConfig(enable_task_planning=True)

        # Mock the tool registry
        with patch("src.services.assistant.tools.get_tool_registry") as mock_get_registry:
            mock_registry = MagicMock()
            mock_registry.list_tools.return_value = []
            mock_get_registry.return_value = mock_registry

            events = []
            async for event in assistant_service._execute_with_planning(
                user=user_context,
                session_id="test-session",
                message="Search for product specifications",
                config=config,
                history=[],
                retrieved_contexts=[],
            ):
                events.append(event)

        # Should have TASK_PLANNING event
        task_planning_events = [
            e for e in events if e.event_type == StreamEventType.TASK_PLANNING.value
        ]
        assert len(task_planning_events) >= 1

    @pytest.mark.asyncio
    async def test_execute_with_planning_yields_working_memory_updates(
        self, assistant_service, user_context
    ):
        """Test that WORKING_MEMORY_UPDATE events are yielded."""
        config = AssistantConfig(enable_task_planning=True)

        with patch("src.services.assistant.tools.get_tool_registry") as mock_get_registry:
            mock_registry = MagicMock()
            mock_registry.list_tools.return_value = []
            mock_get_registry.return_value = mock_registry

            events = []
            async for event in assistant_service._execute_with_planning(
                user=user_context,
                session_id="test-session",
                message="Compare products",
                config=config,
                history=[],
                retrieved_contexts=[],
            ):
                events.append(event)

        # Should have WORKING_MEMORY_UPDATE events
        memory_events = [
            e for e in events if e.event_type == StreamEventType.WORKING_MEMORY_UPDATE.value
        ]
        assert len(memory_events) >= 1

    @pytest.mark.asyncio
    async def test_execute_with_planning_sets_goal_in_working_memory(
        self, assistant_service, user_context
    ):
        """Test that goal is set in working memory."""
        config = AssistantConfig(enable_task_planning=True)
        message = "Generate a comparison report"

        with patch("src.services.assistant.tools.get_tool_registry") as mock_get_registry:
            mock_registry = MagicMock()
            mock_registry.list_tools.return_value = []
            mock_get_registry.return_value = mock_registry

            events = []
            async for event in assistant_service._execute_with_planning(
                user=user_context,
                session_id="test-session-goal",
                message=message,
                config=config,
                history=[],
                retrieved_contexts=[],
            ):
                events.append(event)

        # Verify working memory has the goal
        working_memory = assistant_service.get_working_memory("test-session-goal")
        assert working_memory.goal == message

    @pytest.mark.asyncio
    async def test_execute_with_planning_handles_error_gracefully(
        self, assistant_service, user_context
    ):
        """Test that errors are handled gracefully."""
        config = AssistantConfig(enable_task_planning=True)

        # Replace the task planner with one that raises an error
        mock_planner = MagicMock()
        mock_planner.create_plan = AsyncMock(side_effect=Exception("Planning failed"))
        assistant_service._task_planner = mock_planner

        with patch("src.services.assistant.tools.get_tool_registry") as mock_get_registry:
            mock_registry = MagicMock()
            mock_registry.list_tools.return_value = []
            mock_get_registry.return_value = mock_registry

            events = []
            async for event in assistant_service._execute_with_planning(
                user=user_context,
                session_id="test-session-error",
                message="Test message",
                config=config,
                history=[],
                retrieved_contexts=[],
            ):
                events.append(event)

        # Should have an ERROR event
        error_events = [
            e for e in events if e.event_type == StreamEventType.ERROR.value
        ]
        assert len(error_events) >= 1
        assert "Planning failed" in error_events[0].data["message"]


# =============================================================================
# _format_execution_results Tests
# =============================================================================


class TestFormatExecutionResults:
    """Test _format_execution_results method."""

    def test_format_empty_results(self, assistant_service):
        """Test formatting empty results."""
        result = assistant_service._format_execution_results([])
        assert result == ""

    def test_format_successful_results(self, assistant_service):
        """Test formatting successful results."""
        results = [
            ToolExecutionResult(
                task_id="search_1",
                tool="kb_search",
                success=True,
                result={"documents": ["doc1", "doc2"]},
                duration_ms=150.5,
            ),
            ToolExecutionResult(
                task_id="analyze_1",
                tool="analyze",
                success=True,
                result="Analysis complete",
                duration_ms=250.0,
            ),
        ]

        formatted = assistant_service._format_execution_results(results)

        assert "## Task Execution Results" in formatted
        assert "search_1" in formatted
        assert "kb_search" in formatted
        assert "SUCCESS" in formatted
        assert "analyze_1" in formatted

    def test_format_failed_results(self, assistant_service):
        """Test formatting failed results."""
        results = [
            ToolExecutionResult(
                task_id="failed_task",
                tool="some_tool",
                success=False,
                error="Tool execution failed",
                duration_ms=100.0,
            ),
        ]

        formatted = assistant_service._format_execution_results(results)

        assert "FAILED" in formatted
        assert "Tool execution failed" in formatted

    def test_format_truncates_long_results(self, assistant_service):
        """Test that long results are truncated."""
        long_result = "x" * 1000  # Longer than 500 chars
        results = [
            ToolExecutionResult(
                task_id="long_result",
                tool="test_tool",
                success=True,
                result=long_result,
                duration_ms=100.0,
            ),
        ]

        formatted = assistant_service._format_execution_results(results)

        # Result should be truncated to 500 chars + "..."
        assert "..." in formatted
        assert len(formatted) < len(long_result)


# =============================================================================
# Integration with chat_stream Tests
# =============================================================================


class TestChatStreamWithPlanning:
    """Test chat_stream integration with task planning."""

    @pytest.mark.asyncio
    async def test_chat_stream_skips_planning_when_disabled(
        self, assistant_service, user_context, mock_model_registry
    ):
        """Test that planning is skipped when enable_task_planning=False."""
        config = AssistantConfig(
            enable_task_planning=False,
            model_id="gpt-4o",
        )

        # Mock the model streaming
        async def mock_stream(*args, **kwargs):
            yield MagicMock(
                content="Hello",
                tool_calls=None,
                usage={"input_tokens": 10, "output_tokens": 5},
                finish_reason=None,
            )
            yield MagicMock(
                content=None,
                tool_calls=None,
                usage=None,
                finish_reason="stop",
            )

        mock_model_registry.chat_stream = mock_stream

        with patch("src.services.assistant.tools.get_tool_registry") as mock_get_registry:
            mock_registry = MagicMock()
            mock_registry.list_tools.return_value = []
            mock_registry.get_openai_schemas.return_value = []
            mock_get_registry.return_value = mock_registry

            events = []
            async for event in assistant_service.chat_stream(
                user=user_context,
                session_id="test-no-planning",
                message="Hello",
                config=config,
                persist_messages=False,
            ):
                events.append(event)

        # Should NOT have TASK_PLANNING event
        task_planning_events = [
            e for e in events if e.event_type == StreamEventType.TASK_PLANNING.value
        ]
        assert len(task_planning_events) == 0

    @pytest.mark.asyncio
    async def test_chat_stream_includes_planning_when_enabled(
        self, assistant_service, user_context, mock_model_registry
    ):
        """Test that planning events are included when enabled."""
        # Use traditional path (not agent loop) to test task planning
        config = AssistantConfig(
            enable_task_planning=True,
            model_id="gpt-4o",
            use_agent_loop=False,
        )

        # Mock the model streaming
        async def mock_stream(*args, **kwargs):
            yield MagicMock(
                content="Based on the results",
                tool_calls=None,
                usage={"input_tokens": 10, "output_tokens": 5},
                finish_reason=None,
            )
            yield MagicMock(
                content=None,
                tool_calls=None,
                usage=None,
                finish_reason="stop",
            )

        mock_model_registry.chat_stream = mock_stream

        with patch("src.services.assistant.tools.get_tool_registry") as mock_get_registry:
            mock_registry = MagicMock()
            mock_registry.list_tools.return_value = []
            mock_registry.get_openai_schemas.return_value = []
            mock_get_registry.return_value = mock_registry

            events = []
            async for event in assistant_service.chat_stream(
                user=user_context,
                session_id="test-with-planning",
                message="Compare product A and product B",
                config=config,
                persist_messages=False,
            ):
                events.append(event)

        # Should have TASK_PLANNING event
        task_planning_events = [
            e for e in events if e.event_type == StreamEventType.TASK_PLANNING.value
        ]
        assert len(task_planning_events) >= 1

        # Should also have WORKING_MEMORY_UPDATE events
        memory_events = [
            e for e in events if e.event_type == StreamEventType.WORKING_MEMORY_UPDATE.value
        ]
        assert len(memory_events) >= 1

    @pytest.mark.asyncio
    async def test_chat_stream_requires_plan_confirmation(
        self, assistant_service, user_context, mock_model_registry
    ):
        """Test that a plan confirmation gate is emitted when confirm_plan=True."""
        # Use traditional path (not agent loop) to test task planning
        config = AssistantConfig(
            enable_task_planning=True,
            confirm_plan=True,  # When True, user wants to confirm plan before execution
            model_id="gpt-4o",
            use_agent_loop=False,
        )

        async def mock_stream(*args, **kwargs):
            yield MagicMock(
                content="Hello",
                tool_calls=None,
                usage={"input_tokens": 10, "output_tokens": 5},
                finish_reason=None,
            )
            yield MagicMock(
                content=None,
                tool_calls=None,
                usage=None,
                finish_reason="stop",
            )

        mock_model_registry.chat_stream = mock_stream

        with patch("src.services.assistant.tools.get_tool_registry") as mock_get_registry:
            mock_registry = MagicMock()
            mock_registry.list_tools.return_value = []
            mock_registry.get_openai_schemas.return_value = []
            mock_get_registry.return_value = mock_registry

            events = []
            async for event in assistant_service.chat_stream(
                user=user_context,
                session_id="test-confirm-plan",
                message="整理会议纪要",
                config=config,
                persist_messages=False,
            ):
                events.append(event)

        status_events = [
            e for e in events if e.event_type == StreamEventType.STATUS.value
        ]
        assert any("confirm" in str(e.data).lower() for e in status_events)


# =============================================================================
# Working Memory Integration Tests
# =============================================================================


class TestWorkingMemoryIntegration:
    """Test working memory integration with task planning."""

    def test_working_memory_created_per_session(self, assistant_service):
        """Test that working memory is created per session."""
        mem1 = assistant_service.get_working_memory("session-1")
        mem2 = assistant_service.get_working_memory("session-2")

        assert mem1 is not mem2
        assert mem1.session_id == "session-1"
        assert mem2.session_id == "session-2"

    def test_working_memory_same_session_returns_same_instance(self, assistant_service):
        """Test that same session returns same working memory instance."""
        mem1 = assistant_service.get_working_memory("session-same")
        mem2 = assistant_service.get_working_memory("session-same")

        assert mem1 is mem2

    def test_clear_working_memory(self, assistant_service):
        """Test clearing working memory."""
        mem = assistant_service.get_working_memory("session-clear")
        mem.set_goal("Test goal")
        mem.add_task("task-1", "Test task")

        assistant_service.clear_working_memory("session-clear")

        # Getting working memory again should create new instance
        new_mem = assistant_service.get_working_memory("session-clear")
        assert new_mem.goal is None
        assert len(new_mem.tasks) == 0


if __name__ == "__main__":
    pytest.main([__file__, "-v"])
