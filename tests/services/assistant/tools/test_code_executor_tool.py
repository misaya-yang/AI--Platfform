"""
Tests for Code Executor Tool

Tests the CODE_EXECUTOR_TOOL definition and CodeExecutorToolExecutor class.
"""

import pytest
from unittest.mock import AsyncMock, MagicMock, patch
from dataclasses import dataclass
from datetime import datetime
from typing import List

from src.services.assistant.tools.code_executor_tool import (
    CODE_EXECUTOR_TOOL,
    CodeExecutorToolExecutor,
    register_code_executor_tool,
)
from src.services.assistant.tools.tool_registry import (
    ToolCategory,
    ToolRiskLevel,
    ToolCallRequest,
    ToolCallResult,
    ToolRegistry,
)
from src.services.assistant.code_executor import (
    CodeExecutorService,
    CodeExecutionResult,
    ExecutionStatus,
    OutputFile,
)


# =============================================================================
# Tests for CODE_EXECUTOR_TOOL Definition
# =============================================================================


class TestCodeExecutorToolDefinition:
    """Tests for the CODE_EXECUTOR_TOOL definition."""

    def test_tool_name(self):
        """Test that the tool has the correct name."""
        assert CODE_EXECUTOR_TOOL.name == "execute_python_code"

    def test_tool_description(self):
        """Test that the tool has a meaningful description."""
        assert "Execute Python code" in CODE_EXECUTOR_TOOL.description
        assert "data analysis" in CODE_EXECUTOR_TOOL.description
        assert "Docker" in CODE_EXECUTOR_TOOL.description

    def test_tool_category(self):
        """Test that the tool is in the ANALYSIS category."""
        assert CODE_EXECUTOR_TOOL.category == ToolCategory.ANALYSIS

    def test_tool_risk_level(self):
        """Test that the tool has MEDIUM risk level."""
        assert CODE_EXECUTOR_TOOL.risk_level == ToolRiskLevel.MEDIUM

    def test_tool_requires_confirmation(self):
        """Test that the tool does not require confirmation."""
        assert CODE_EXECUTOR_TOOL.requires_confirmation is False

    def test_tool_has_code_parameter(self):
        """Test that the tool has a code parameter."""
        code_param = None
        for param in CODE_EXECUTOR_TOOL.parameters:
            if param.name == "code":
                code_param = param
                break

        assert code_param is not None
        assert code_param.type == "string"
        assert code_param.required is True
        assert "Python code" in code_param.description

    def test_tool_has_when_to_use(self):
        """Test that the tool has usage guidance."""
        assert CODE_EXECUTOR_TOOL.when_to_use is not None
        assert "analyze data" in CODE_EXECUTOR_TOOL.when_to_use

    def test_tool_has_when_not_to_use(self):
        """Test that the tool has guidance on when not to use."""
        assert CODE_EXECUTOR_TOOL.when_not_to_use is not None

    def test_tool_has_examples(self):
        """Test that the tool has usage examples."""
        assert len(CODE_EXECUTOR_TOOL.examples) >= 1

    def test_tool_timeout(self):
        """Test that the tool has a reasonable timeout."""
        assert CODE_EXECUTOR_TOOL.timeout_seconds == 60

    def test_openai_schema_generation(self):
        """Test that OpenAI schema can be generated."""
        schema = CODE_EXECUTOR_TOOL.to_openai_schema()

        assert schema["type"] == "function"
        assert schema["function"]["name"] == "execute_python_code"
        assert "parameters" in schema["function"]
        assert "code" in schema["function"]["parameters"]["properties"]
        assert "code" in schema["function"]["parameters"]["required"]

    def test_anthropic_schema_generation(self):
        """Test that Anthropic schema can be generated."""
        schema = CODE_EXECUTOR_TOOL.to_anthropic_schema()

        assert schema["name"] == "execute_python_code"
        assert "input_schema" in schema
        assert "code" in schema["input_schema"]["properties"]
        assert "code" in schema["input_schema"]["required"]


# =============================================================================
# Tests for CodeExecutorToolExecutor
# =============================================================================


class TestCodeExecutorToolExecutor:
    """Tests for the CodeExecutorToolExecutor class."""

    @pytest.fixture
    def mock_code_executor(self):
        """Create a mock CodeExecutorService."""
        mock = MagicMock(spec=CodeExecutorService)
        mock.is_docker_available.return_value = True
        return mock

    @pytest.fixture
    def executor(self, mock_code_executor):
        """Create a CodeExecutorToolExecutor instance."""
        return CodeExecutorToolExecutor(mock_code_executor)

    @pytest.fixture
    def sample_request(self):
        """Create a sample tool call request."""
        return ToolCallRequest(
            call_id="test-call-123",
            tool_name="execute_python_code",
            arguments={"code": "print('Hello, World!')"},
        )

    @pytest.fixture
    def successful_execution_result(self):
        """Create a successful execution result."""
        return CodeExecutionResult(
            execution_id="exec-123",
            status=ExecutionStatus.SUCCESS,
            stdout="Hello, World!\n",
            stderr="",
            output_files=[],
            started_at=datetime.utcnow(),
            completed_at=datetime.utcnow(),
            duration_ms=150.5,
            exit_code=0,
        )

    @pytest.fixture
    def failed_execution_result(self):
        """Create a failed execution result."""
        return CodeExecutionResult(
            execution_id="exec-456",
            status=ExecutionStatus.ERROR,
            stdout="",
            stderr="NameError: name 'undefined_var' is not defined",
            output_files=[],
            started_at=datetime.utcnow(),
            completed_at=datetime.utcnow(),
            duration_ms=50.0,
            exit_code=1,
            error_message="Process exited with code 1",
        )

    @pytest.mark.asyncio
    async def test_execute_success(
        self, executor, mock_code_executor, sample_request, successful_execution_result
    ):
        """Test successful code execution."""
        mock_code_executor.execute = AsyncMock(return_value=successful_execution_result)

        result = await executor.execute(sample_request)

        assert result.success is True
        assert result.call_id == sample_request.call_id
        assert result.tool_name == sample_request.tool_name
        assert result.error is None
        assert "Hello, World!" in result.result
        assert result.metadata["execution_id"] == "exec-123"
        assert result.metadata["status"] == "success"

    @pytest.mark.asyncio
    async def test_execute_failure(
        self, executor, mock_code_executor, sample_request, failed_execution_result
    ):
        """Test failed code execution."""
        mock_code_executor.execute = AsyncMock(return_value=failed_execution_result)

        result = await executor.execute(sample_request)

        assert result.success is False
        assert result.error is not None
        assert result.metadata["status"] == "error"

    @pytest.mark.asyncio
    async def test_execute_empty_code(self, executor, mock_code_executor):
        """Test execution with empty code."""
        request = ToolCallRequest(
            call_id="test-call-456",
            tool_name="execute_python_code",
            arguments={"code": ""},
        )

        result = await executor.execute(request)

        assert result.success is False
        assert "required" in result.error.lower()
        mock_code_executor.execute.assert_not_called()

    @pytest.mark.asyncio
    async def test_execute_whitespace_code(self, executor, mock_code_executor):
        """Test execution with whitespace-only code."""
        request = ToolCallRequest(
            call_id="test-call-789",
            tool_name="execute_python_code",
            arguments={"code": "   \n\t  "},
        )

        result = await executor.execute(request)

        assert result.success is False
        assert "empty" in result.error.lower()
        mock_code_executor.execute.assert_not_called()

    @pytest.mark.asyncio
    async def test_execute_missing_code(self, executor, mock_code_executor):
        """Test execution with missing code parameter."""
        request = ToolCallRequest(
            call_id="test-call-999",
            tool_name="execute_python_code",
            arguments={},
        )

        result = await executor.execute(request)

        assert result.success is False
        assert "required" in result.error.lower()
        mock_code_executor.execute.assert_not_called()

    @pytest.mark.asyncio
    async def test_execute_docker_not_available(self, executor, mock_code_executor, sample_request):
        """Test execution when Docker is not available."""
        mock_code_executor.is_docker_available.return_value = False

        result = await executor.execute(sample_request)

        assert result.success is False
        assert "docker" in result.error.lower()
        mock_code_executor.execute.assert_not_called()

    @pytest.mark.asyncio
    async def test_execute_with_output_files(self, executor, mock_code_executor, sample_request):
        """Test execution with output files."""
        output_file = OutputFile(
            filename="chart.png",
            content=b"\x89PNG\r\n\x1a\n...",
            mime_type="image/png",
            size_bytes=1024,
        )

        result_with_files = CodeExecutionResult(
            execution_id="exec-789",
            status=ExecutionStatus.SUCCESS,
            stdout="Chart generated\n",
            stderr="",
            output_files=[output_file],
            started_at=datetime.utcnow(),
            completed_at=datetime.utcnow(),
            duration_ms=500.0,
            exit_code=0,
        )

        mock_code_executor.execute = AsyncMock(return_value=result_with_files)

        result = await executor.execute(sample_request)

        assert result.success is True
        assert result.metadata["output_files_count"] == 1
        assert "chart.png" in result.result
        assert "image/png" in result.result

    @pytest.mark.asyncio
    async def test_execute_exception_handling(self, executor, mock_code_executor, sample_request):
        """Test that exceptions are handled gracefully."""
        mock_code_executor.execute = AsyncMock(
            side_effect=Exception("Unexpected Docker error")
        )

        result = await executor.execute(sample_request)

        assert result.success is False
        assert "Unexpected Docker error" in result.error

    @pytest.mark.asyncio
    async def test_execute_with_stderr(self, executor, mock_code_executor, sample_request):
        """Test execution with stderr output (warnings)."""
        result_with_stderr = CodeExecutionResult(
            execution_id="exec-warn",
            status=ExecutionStatus.SUCCESS,
            stdout="Result: 42\n",
            stderr="DeprecationWarning: some deprecated function\n",
            output_files=[],
            started_at=datetime.utcnow(),
            completed_at=datetime.utcnow(),
            duration_ms=100.0,
            exit_code=0,
        )

        mock_code_executor.execute = AsyncMock(return_value=result_with_stderr)

        result = await executor.execute(sample_request)

        assert result.success is True
        assert "Result: 42" in result.result
        assert "DeprecationWarning" in result.result


# =============================================================================
# Tests for Tool Registration
# =============================================================================


class TestToolRegistration:
    """Tests for tool registration functions."""

    def test_register_code_executor_tool_with_service(self):
        """Test registering the tool with a valid service."""
        # Create mock code executor
        mock_executor = MagicMock(spec=CodeExecutorService)
        mock_executor.is_docker_available.return_value = True

        # Patch the register_tool function
        with patch(
            "src.services.assistant.tools.code_executor_tool.register_tool"
        ) as mock_register:
            register_code_executor_tool(mock_executor)

            # Verify registration was called
            mock_register.assert_called_once()
            call_args = mock_register.call_args
            assert call_args[0][0] == CODE_EXECUTOR_TOOL
            assert isinstance(call_args[0][1], CodeExecutorToolExecutor)

    def test_register_code_executor_tool_without_service(self):
        """Test that registration is skipped when no service is provided."""
        with patch(
            "src.services.assistant.tools.code_executor_tool.register_tool"
        ) as mock_register:
            register_code_executor_tool(None)

            # Verify registration was not called
            mock_register.assert_not_called()

    def test_register_code_executor_tool_docker_not_available(self):
        """Test registration when Docker is not available (should still register)."""
        mock_executor = MagicMock(spec=CodeExecutorService)
        mock_executor.is_docker_available.return_value = False

        with patch(
            "src.services.assistant.tools.code_executor_tool.register_tool"
        ) as mock_register:
            register_code_executor_tool(mock_executor)

            # Should still register (will fail at execution time)
            mock_register.assert_called_once()


# =============================================================================
# Tests for Integration with ToolRegistry
# =============================================================================


class TestToolRegistryIntegration:
    """Tests for integration with the ToolRegistry."""

    def test_tool_can_be_added_to_registry(self):
        """Test that the tool can be added to a registry."""
        registry = ToolRegistry()
        mock_executor = MagicMock(spec=CodeExecutorService)
        mock_executor.is_docker_available.return_value = True

        tool_executor = CodeExecutorToolExecutor(mock_executor)
        registry.register(CODE_EXECUTOR_TOOL, tool_executor)

        # Verify tool was registered
        tool = registry.get_tool("execute_python_code")
        assert tool is not None
        assert tool.name == "execute_python_code"

    def test_tool_appears_in_list(self):
        """Test that the tool appears in the tool list."""
        registry = ToolRegistry()
        mock_executor = MagicMock(spec=CodeExecutorService)
        mock_executor.is_docker_available.return_value = True

        tool_executor = CodeExecutorToolExecutor(mock_executor)
        registry.register(CODE_EXECUTOR_TOOL, tool_executor)

        tools = registry.list_tools()
        tool_names = [t.name for t in tools]
        assert "execute_python_code" in tool_names

    def test_tool_filtered_by_category(self):
        """Test that the tool can be filtered by category."""
        registry = ToolRegistry()
        mock_executor = MagicMock(spec=CodeExecutorService)
        mock_executor.is_docker_available.return_value = True

        tool_executor = CodeExecutorToolExecutor(mock_executor)
        registry.register(CODE_EXECUTOR_TOOL, tool_executor)

        # Filter by ANALYSIS category
        analysis_tools = registry.list_tools(category=ToolCategory.ANALYSIS)
        assert len(analysis_tools) == 1
        assert analysis_tools[0].name == "execute_python_code"

        # Filter by RETRIEVAL category (should not include our tool)
        retrieval_tools = registry.list_tools(category=ToolCategory.RETRIEVAL)
        assert len(retrieval_tools) == 0

    @pytest.mark.asyncio
    async def test_tool_execution_through_registry(self):
        """Test executing the tool through the registry."""
        registry = ToolRegistry()
        mock_code_executor = MagicMock(spec=CodeExecutorService)
        mock_code_executor.is_docker_available.return_value = True

        execution_result = CodeExecutionResult(
            execution_id="exec-registry-test",
            status=ExecutionStatus.SUCCESS,
            stdout="42\n",
            stderr="",
            output_files=[],
            started_at=datetime.utcnow(),
            completed_at=datetime.utcnow(),
            duration_ms=100.0,
            exit_code=0,
        )
        mock_code_executor.execute = AsyncMock(return_value=execution_result)

        tool_executor = CodeExecutorToolExecutor(mock_code_executor)
        registry.register(CODE_EXECUTOR_TOOL, tool_executor)

        request = ToolCallRequest(
            call_id="registry-test-123",
            tool_name="execute_python_code",
            arguments={"code": "print(6 * 7)"},
        )

        result = await registry.execute(request)

        assert result.success is True
        assert "42" in result.result


# =============================================================================
# Tests for Argument Validation
# =============================================================================


class TestArgumentValidation:
    """Tests for argument validation."""

    @pytest.fixture
    def executor(self):
        """Create an executor with a mock code executor service."""
        mock = MagicMock(spec=CodeExecutorService)
        mock.is_docker_available.return_value = True
        return CodeExecutorToolExecutor(mock)

    def test_validate_required_code_parameter(self, executor):
        """Test validation of the required code parameter."""
        errors = executor.validate_arguments(CODE_EXECUTOR_TOOL, {})
        assert len(errors) == 1
        assert "code" in errors[0].lower()

    def test_validate_code_type(self, executor):
        """Test validation of code parameter type."""
        errors = executor.validate_arguments(
            CODE_EXECUTOR_TOOL, {"code": 12345}  # Should be string
        )
        assert len(errors) == 1
        assert "string" in errors[0].lower()

    def test_validate_valid_arguments(self, executor):
        """Test validation passes for valid arguments."""
        errors = executor.validate_arguments(
            CODE_EXECUTOR_TOOL, {"code": "print('valid code')"}
        )
        assert len(errors) == 0
