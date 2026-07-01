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

import ast
from typing import TYPE_CHECKING, Any

from ai_gateway_core.logging import get_logger

from .tool_registry import (
    ToolCallRequest,
    ToolCallResult,
    ToolCategory,
    ToolDefinition,
    ToolExample,
    ToolExecutor,
    ToolParameter,
    ToolRiskLevel,
    register_tool,
)

if TYPE_CHECKING:
    from ..code_executor import CodeExecutorService

logger = get_logger(__name__)


# AST-level denylist: modules that suggest sandbox-escape intent.
# Docker + gVisor are the primary defense; this is defense-in-depth to
# reject obvious escape attempts BEFORE they reach the sandbox.
_DENIED_IMPORTS = frozenset({
    "ctypes",           # direct memory / syscalls
    "ctypes.util",
    "mmap",             # memory mapping
    "resource",         # ulimit manipulation
    "pty",              # terminal allocation
    "fcntl",            # file descriptor control
    "termios",
    "tty",
    "multiprocessing",  # process spawning
    "subprocess",       # shell out
    "sh",
    "pexpect",
    "os.exec",
    # Networking — code executor has no network by design; any import is suspicious
    "socket",
    "_socket",
    "ssl",
    "urllib.request",
    "urllib2",
    "http.client",
    "httplib",
    # Low-level process/signal
    "signal",
    "syslog",
})


def _scan_code_for_denied_imports(code: str) -> str | None:
    """Return a human-readable reason if the code imports a denied module,
    otherwise None. Uses Python's AST so it's robust to whitespace/comments
    and cannot be bypassed by `exec`-style string concatenation of literal
    imports; if the code constructs import names dynamically we fall back
    to Docker/gVisor.

    Returns None on parse error — syntax errors will be surfaced by the
    sandbox execution itself with proper error messages.
    """
    try:
        tree = ast.parse(code)
    except SyntaxError:
        return None

    for node in ast.walk(tree):
        if isinstance(node, ast.Import):
            for alias in node.names:
                root = (alias.name or "").split(".")[0]
                if alias.name in _DENIED_IMPORTS or root in _DENIED_IMPORTS:
                    return f"import of '{alias.name}' is not allowed in the sandbox"
        elif isinstance(node, ast.ImportFrom):
            mod = (node.module or "").split(".")[0]
            if (node.module or "") in _DENIED_IMPORTS or mod in _DENIED_IMPORTS:
                return f"from-import of '{node.module}' is not allowed in the sandbox"
        # Block __import__ calls with literal denied module
        elif (
            isinstance(node, ast.Call)
            and isinstance(node.func, ast.Name)
            and node.func.id == "__import__"
            and node.args
        ):
            first = node.args[0]
            if isinstance(first, ast.Constant) and isinstance(first.value, str):
                root = first.value.split(".")[0]
                if first.value in _DENIED_IMPORTS or root in _DENIED_IMPORTS:
                    return f"__import__('{first.value}') is not allowed in the sandbox"
    return None


# =============================================================================
# Code Executor Tool Definition
# =============================================================================

CODE_EXECUTOR_TOOL = ToolDefinition(
    name="execute_python_code",
    description="""Execute Python code in a secure Docker sandbox for data analysis and visualization.

## Language — Python ONLY
This tool runs **Python**. MATLAB, R, Julia, Octave, Mathematica, and any
other language are **NOT available**. If the user asks for a "MATLAB
script" or "R code", write **Python** (with matplotlib / pandas / numpy)
and run it through this tool — then show the generated chart. Never
return raw MATLAB/R code unexecuted.

## NEVER fabricate data
If the user asks for a chart based on real-world data (scores, stock
prices, weather, league standings), you **must** obtain the actual data
before plotting — via your model's native web search, `web_fetch` (with
a known URL), or user-uploaded files.
Do NOT invent "representative" numbers to satisfy the totals. If real
data is unavailable after a reasonable search, say so explicitly rather
than silently substituting placeholder data.

## When to Use (MUST use for these scenarios)
- Analyzing Excel/CSV data files (calculations, statistics, growth rates, trends)
- Creating charts and visualizations (line, bar, pie, scatter, heatmap)
- Complex mathematical computations that require precision
- Data transformation, cleaning, and aggregation
- Processing uploaded files (Excel, CSV, JSON, text)
- Generating reports with computed metrics

## Available Files
- User uploads: /workspace/input/ (Excel, CSV, PDF, etc. - use os.listdir() to check)
- KB documents: /workspace/kb_docs/ (reference materials as text files)
- Output directory: /workspace/output/ (save all generated files here)

## Output file paths — IMPORTANT
Any file you want the user to see (plots, CSVs, reports) **must be written
with a relative filename or a path under `/workspace/output/`**. A relative
filename such as `plt.savefig('chart.png')` or `df.to_csv('data.csv')`
lands next to your code and is auto-collected into the artifacts panel.
**Never write to absolute paths outside `/workspace/`** (e.g. `/tmp/foo.png`
or `/home/user/x.csv`) — those files are discarded when the sandbox exits
and will not appear as artifacts.

## Pre-installed Libraries
numpy, pandas, matplotlib, seaborn, openpyxl, xlrd, scipy, plotly

## Best Practices
1. Always list input directory first: os.listdir('/workspace/input/')
2. Use pandas.read_excel() or read_csv() for data files
3. **CRITICAL — charts must be written to disk with `plt.savefig(...)`**:
   only files in `/workspace/output/` become user-visible artifacts.
   Never use `plt.show()` alone — it produces no artifact. After each
   figure, call `plt.savefig('/workspace/output/<name>.png', dpi=150,
   bbox_inches='tight')` then `plt.close()`.
4. **CRITICAL — when embedding images in your reply, use ONLY the URLs
   returned at the end of the tool result under "Artifact URLs"**.
   Do NOT reference files by their sandbox path (e.g.
   `![chart](/workspace/output/foo.png)`) — those paths don't resolve
   from the browser. Copy the presigned URL as-is into the markdown.
5. Print key metrics and analysis results to stdout
6. Use Chinese labels for charts when user speaks Chinese

## Example: Analyze uploaded Excel
```python
import pandas as pd
import matplotlib.pyplot as plt
import os

# Check available input files
print("Available files:", os.listdir('/workspace/input/'))

# Read uploaded Excel file
df = pd.read_excel('/workspace/input/sales_data.xlsx')
print(f"Data shape: {df.shape}")
print(df.head())

# Calculate growth rate
df['growth'] = df['revenue'].pct_change() * 100
avg_growth = df['growth'].mean()
print(f"Average growth rate: {avg_growth:.2f}%")

# Create trend visualization
plt.figure(figsize=(12, 6))
plt.plot(df['month'], df['revenue'], marker='o', linewidth=2)
plt.title('Revenue Trend Analysis', fontsize=14)
plt.xlabel('Month')
plt.ylabel('Revenue')
plt.grid(True, alpha=0.3)
plt.savefig('/workspace/output/revenue_trend.png', dpi=150, bbox_inches='tight')
print("Chart saved to /workspace/output/revenue_trend.png")
```
""",
    parameters=[
        ToolParameter(
            name="code",
            type="string",
            description="Python code to execute. Has access to data analysis libraries (numpy, pandas, matplotlib, etc.), "
            "user uploads in /workspace/input/, and should save outputs to /workspace/output/.",
            required=True,
        ),
    ],
    category=ToolCategory.ANALYSIS,
    risk_level=ToolRiskLevel.MEDIUM,
    requires_confirmation=False,
    sandbox_profile="docker-gvisor-no-network",
    audit_shape={
        "input": "code_hash_and_redacted_summary",
        "output": "stdout_stderr_status_and_artifact_metadata",
    },
    redaction_policy="redact_secrets_and_provider_env",
    when_to_use="ALWAYS use when user uploads Excel/CSV files and asks to analyze data, trends, growth rates, "
    "statistics, or visualization. Use for any data computation that requires precision beyond LLM estimation. "
    "Use for creating charts, graphs, or any data visualization.",
    when_not_to_use="Do not use for generating AI images/artwork (use generate_image instead). "
    "Do not use for simple factual questions that don't require computation.",
    examples=[
        ToolExample(
            description="Analyze uploaded Excel and calculate growth rate",
            input={
                "code": """
import pandas as pd
import matplotlib.pyplot as plt
import os

# List input files
files = os.listdir('/workspace/input/')
print(f"Found files: {files}")

# Read Excel file
df = pd.read_excel('/workspace/input/' + files[0])
print(f"Columns: {df.columns.tolist()}")

# Calculate key metrics
total = df['销售额'].sum()
avg = df['销售额'].mean()
growth = df['销售额'].pct_change().mean() * 100
print(f"总销售额: {total:,.0f}")
print(f"平均销售额: {avg:,.0f}")
print(f"平均增长率: {growth:.2f}%")

# Visualize
plt.figure(figsize=(10, 6))
df.plot(x='月份', y='销售额', kind='bar', ax=plt.gca())
plt.title('月度销售额分析')
plt.ylabel('销售额')
plt.tight_layout()
plt.savefig('/workspace/output/analysis.png', dpi=150)
"""
            },
            expected_output="Analyzes Excel data, calculates growth rate, and generates bar chart",
        ),
        ToolExample(
            description="Statistical analysis with visualization",
            input={
                "code": """
import numpy as np
import pandas as pd
import matplotlib.pyplot as plt

# Statistical analysis
data = [23, 45, 67, 89, 12, 34, 56, 78, 90, 11]
stats = {
    'Mean': np.mean(data),
    'Median': np.median(data),
    'Std': np.std(data),
    'Min': np.min(data),
    'Max': np.max(data),
}
for k, v in stats.items():
    print(f"{k}: {v:.2f}")

# Create histogram
plt.figure(figsize=(8, 5))
plt.hist(data, bins=5, edgecolor='black', alpha=0.7)
plt.title('Data Distribution')
plt.xlabel('Value')
plt.ylabel('Frequency')
plt.savefig('/workspace/output/histogram.png', dpi=150)
"""
            },
            expected_output="Prints statistical metrics and generates histogram",
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

    def __init__(self, code_executor: CodeExecutorService):
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

        # Defense-in-depth: AST-level denylist for sandbox-escape imports.
        # Primary isolation is Docker + gVisor; this rejects obvious
        # attempts (socket, ctypes, subprocess...) before the sandbox runs.
        denied_reason = _scan_code_for_denied_imports(code)
        if denied_reason:
            logger.warning(
                f"[Security] code_executor rejected code "
                f"(call_id={request.call_id}): {denied_reason}"
            )
            return ToolCallResult(
                call_id=request.call_id,
                tool_name=request.tool_name,
                success=False,
                error=(
                    f"Code rejected by static analysis: {denied_reason}. "
                    f"The sandbox does not have network or shell access; "
                    f"use pandas/numpy/matplotlib for data work instead."
                ),
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
            logger.info(f"Executing code (call_id={request.call_id}, code_length={len(code)})")

            # Execute the code
            result = await self.code_executor.execute(code=code)

            # Format output files for the result
            output_files_info = []
            for f in result.output_files:
                output_files_info.append(
                    {
                        "filename": f.filename,
                        "mime_type": f.mime_type,
                        "size_bytes": f.size_bytes,
                        "content_base64": f.to_base64(),
                    }
                )

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
                output_files=output_files_info,
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

    def _format_result(self, result: dict[str, Any]) -> str:
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
    code_executor: CodeExecutorService | None = None,
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
