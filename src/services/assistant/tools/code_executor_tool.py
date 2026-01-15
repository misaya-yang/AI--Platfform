"""
Code Executor Tool for Assistant Service

Phase 2: Provides a tool for executing Python code in Docker sandbox.

Features:
- Execute Python code for data analysis
- Generate charts and visualizations
- Process files and perform calculations
- Safe execution in Docker containers
"""

from __future__ import annotations

from typing import Any, Dict, List, Optional, TYPE_CHECKING

from .tool_registry import (
    ToolDefinition,
    ToolParameter,
    ToolExample,
    ToolCategory,
    ToolRiskLevel,
    ToolExecutor,
    ToolCallRequest,
    ToolCallResult,
    register_tool,
)
from ....core.observability.logging import get_logger

if TYPE_CHECKING:
    from ..code_executor import CodeExecutorService

logger = get_logger(__name__)


# =============================================================================
# Code Executor Tool Definition
# =============================================================================

CODE_EXECUTOR_TOOL = ToolDefinition(
    name="execute_python_code",
    description="Execute Python code to perform data analysis, generate charts, process files, "
                "or perform complex calculations. Code runs in a sandboxed Docker container with "
                "access to numpy, pandas, and matplotlib.",
    parameters=[
        ToolParameter(
            name="code",
            type="string",
            description="Python code to execute. The code has access to:\n"
                       "- numpy, pandas, matplotlib (pre-installed)\n"
                       "- Input files in /workspace/input/\n"
                       "- KB documents in /workspace/kb_docs/\n"
                       "- Output files should be saved to /workspace/output/",
            required=True,
        ),
    ],
    category=ToolCategory.ANALYSIS,
    risk_level=ToolRiskLevel.MEDIUM,
    requires_confirmation=False,
    when_to_use="When the user asks to analyze data, create charts or visualizations, "
                "perform mathematical calculations, process CSV/JSON files, "
                "generate reports with data, or any task requiring code execution.",
    when_not_to_use="Do not use for simple questions that can be answered directly, "
                    "or for tasks that don't require computation or data processing.",
    examples=[
        ToolExample(
            description="Analyze data and create a chart",
            input={
                "code": """
import pandas as pd
import matplotlib.pyplot as plt

# Sample data analysis
data = {'Month': ['Jan', 'Feb', 'Mar'], 'Sales': [100, 150, 200]}
df = pd.DataFrame(data)

# Create bar chart
plt.figure(figsize=(10, 6))
plt.bar(df['Month'], df['Sales'])
plt.title('Monthly Sales')
plt.savefig('/workspace/output/sales_chart.png')
print(f"Total sales: {df['Sales'].sum()}")
"""
            },
            expected_output="Generates a bar chart and prints total sales",
        ),
        ToolExample(
            description="Perform calculations",
            input={
                "code": """
import numpy as np

# Calculate statistics
data = [23, 45, 67, 89, 12, 34, 56, 78, 90, 11]
mean = np.mean(data)
std = np.std(data)
print(f"Mean: {mean:.2f}, Std: {std:.2f}")
"""
            },
            expected_output="Prints statistical calculations",
        ),
    ],
    timeout_seconds=60,
    max_retries=1,
)


# =============================================================================
# Code Executor Tool Executor
# =============================================================================


class CodeExecutorToolExecutor(ToolExecutor):
    """Executor for the code execution tool."""

    def __init__(self, code_executor: "CodeExecutorService"):
        """
        Initialize the code executor tool.

        Args:
            code_executor: The CodeExecutorService instance for running code.
        """
        self.code_executor = code_executor

    async def execute(self, request: ToolCallRequest) -> ToolCallResult:
        """
        Execute Python code in a Docker sandbox.

        Args:
            request: The tool call request containing the code to execute.

        Returns:
            ToolCallResult with execution results including stdout, stderr, and output files.
        """
        code = request.arguments.get("code", "")

        if not code:
            return ToolCallResult(
                call_id=request.call_id,
                tool_name=request.tool_name,
                success=False,
                error="Code is required",
            )

        if not code.strip():
            return ToolCallResult(
                call_id=request.call_id,
                tool_name=request.tool_name,
                success=False,
                error="Code cannot be empty",
            )

        # Check if Docker is available
        if not self.code_executor.is_docker_available():
            return ToolCallResult(
                call_id=request.call_id,
                tool_name=request.tool_name,
                success=False,
                error="Code execution is not available (Docker not running)",
            )

        try:
            logger.info(
                f"Executing code (call_id={request.call_id}, "
                f"code_length={len(code)})"
            )

            # Execute the code
            result = await self.code_executor.execute(code=code)

            # Format output files for the result
            output_files_info = []
            for f in result.output_files:
                output_files_info.append({
                    "filename": f.filename,
                    "mime_type": f.mime_type,
                    "size_bytes": f.size_bytes,
                    "content_base64": f.to_base64(),
                })

            # Build the result content
            result_content = {
                "execution_id": result.execution_id,
                "status": result.status.value,
                "stdout": result.stdout,
                "stderr": result.stderr,
                "output_files": output_files_info,
                "duration_ms": result.duration_ms,
            }

            if result.error_message:
                result_content["error_message"] = result.error_message

            # Format for LLM consumption
            formatted_result = self._format_result(result_content)

            return ToolCallResult(
                call_id=request.call_id,
                tool_name=request.tool_name,
                success=result.is_success(),
                result=formatted_result,
                error=result.error_message if not result.is_success() else None,
                metadata={
                    "execution_id": result.execution_id,
                    "status": result.status.value,
                    "duration_ms": result.duration_ms,
                    "output_files_count": len(result.output_files),
                    "exit_code": result.exit_code,
                },
            )

        except Exception as e:
            logger.error(f"Code execution failed: {e}", exc_info=True)
            return ToolCallResult(
                call_id=request.call_id,
                tool_name=request.tool_name,
                success=False,
                error=str(e),
            )

    def _format_result(self, result: Dict[str, Any]) -> str:
        """
        Format execution result for LLM consumption.

        Args:
            result: The execution result dictionary.

        Returns:
            Formatted string representation of the result.
        """
        parts = []

        # Execution status
        status = result.get("status", "unknown")
        duration = result.get("duration_ms", 0)
        parts.append(f"Execution Status: {status} (took {duration:.1f}ms)")

        # Standard output
        stdout = result.get("stdout", "").strip()
        if stdout:
            parts.append(f"\n--- Output ---\n{stdout}")

        # Standard error (if any)
        stderr = result.get("stderr", "").strip()
        if stderr:
            parts.append(f"\n--- Errors/Warnings ---\n{stderr}")

        # Output files
        output_files = result.get("output_files", [])
        if output_files:
            parts.append(f"\n--- Generated Files ({len(output_files)}) ---")
            for f in output_files:
                size_kb = f.get("size_bytes", 0) / 1024
                parts.append(f"- {f['filename']} ({f['mime_type']}, {size_kb:.1f}KB)")

        # Error message
        error_message = result.get("error_message")
        if error_message:
            parts.append(f"\n--- Error ---\n{error_message}")

        return "\n".join(parts)


# =============================================================================
# Tool Registration Helper
# =============================================================================


def register_code_executor_tool(
    code_executor: Optional["CodeExecutorService"] = None,
) -> None:
    """
    Register the code executor tool with the global registry.

    Args:
        code_executor: The CodeExecutorService instance. If not provided,
                      the tool will not be registered.
    """
    if code_executor is None:
        logger.warning("CodeExecutorService not available, code executor tool not registered")
        return

    if not code_executor.is_docker_available():
        logger.warning("Docker not available, code executor tool registered but may not work")

    register_tool(CODE_EXECUTOR_TOOL, CodeExecutorToolExecutor(code_executor))
    logger.info("Registered code executor tool")
