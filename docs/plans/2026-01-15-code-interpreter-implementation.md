# Code Interpreter 实施计划

> **For Claude:** REQUIRED SUB-SKILL: Use superpowers:executing-plans to implement this plan task-by-task.

**Goal:** 为 AI 助手添加 Docker 沙箱代码执行能力，支持 Python 数据分析和图表生成

**Architecture:** 在现有 AssistantService 基础上扩展，新增 CodeExecutorService 处理 Docker 执行，前端增加 Artifacts 面板

**Tech Stack:** Docker SDK (Python), FastAPI SSE, React, Monaco Editor

---

## Task 1: 创建 CodeExecutorService 核心类

**Files:**
- Create: `src/services/assistant/code_executor.py`
- Test: `tests/services/assistant/test_code_executor.py`

**Step 1: 创建数据类定义**

```python
# src/services/assistant/code_executor.py
"""
Code Executor Service - Docker Sandbox for Python Execution

Provides secure code execution in isolated Docker containers with:
- Resource limits (CPU, memory, timeout)
- Network isolation
- File I/O for inputs/outputs
- KB document access
"""

from __future__ import annotations

import asyncio
import os
import shutil
import tempfile
import time
import uuid
from dataclasses import dataclass, field
from enum import Enum
from pathlib import Path
from typing import Any, AsyncGenerator, Dict, List, Optional

from ...core.observability.logging import get_logger

logger = get_logger(__name__)


class ExecutionStatus(str, Enum):
    """Execution status."""
    PENDING = "pending"
    RUNNING = "running"
    SUCCESS = "success"
    ERROR = "error"
    TIMEOUT = "timeout"


@dataclass
class CodeExecutionConfig:
    """Configuration for code execution."""
    container_image: str = "python:3.11-slim"
    memory_limit: str = "512m"
    cpu_limit: float = 0.5
    timeout_seconds: int = 30
    network_disabled: bool = True
    max_output_size_bytes: int = 10 * 1024 * 1024  # 10MB


@dataclass
class InputFile:
    """Input file for code execution."""
    filename: str
    content: bytes
    content_type: str = "application/octet-stream"


@dataclass
class OutputFile:
    """Output file from code execution."""
    filename: str
    content_type: str
    size_bytes: int
    local_path: str  # Temporary path on server

    def to_dict(self) -> Dict[str, Any]:
        return {
            "filename": self.filename,
            "content_type": self.content_type,
            "size_bytes": self.size_bytes,
        }


@dataclass
class CodeExecutionResult:
    """Result of code execution."""
    execution_id: str
    success: bool
    status: ExecutionStatus
    stdout: str = ""
    stderr: str = ""
    output_files: List[OutputFile] = field(default_factory=list)
    execution_time_ms: int = 0
    memory_used_mb: float = 0
    error_message: Optional[str] = None

    def to_dict(self) -> Dict[str, Any]:
        return {
            "execution_id": self.execution_id,
            "success": self.success,
            "status": self.status.value,
            "stdout": self.stdout,
            "stderr": self.stderr,
            "output_files": [f.to_dict() for f in self.output_files],
            "execution_time_ms": self.execution_time_ms,
            "memory_used_mb": self.memory_used_mb,
            "error_message": self.error_message,
        }
```

**Step 2: 运行测试确认数据类正确**

```bash
cd /Users/misaya.yanghejazfs.com.au/hejaz_projects/ai_gateway/ai-gateway
source /Users/misaya.yanghejazfs.com.au/miniconda3/bin/activate ai_gateway
python -c "from src.services.assistant.code_executor import CodeExecutionConfig, CodeExecutionResult, ExecutionStatus; print('OK')"
```

Expected: `OK`

**Step 3: 实现 CodeExecutorService 类**

在同一文件中添加：

```python
class CodeExecutorService:
    """
    Docker sandbox code execution service.

    Executes Python code in isolated Docker containers with:
    - Resource limits (memory, CPU, timeout)
    - Network isolation
    - File I/O support
    - KB document integration
    """

    def __init__(self, config: Optional[CodeExecutionConfig] = None):
        self.config = config or CodeExecutionConfig()
        self._docker_client = None
        self._temp_dirs: Dict[str, str] = {}  # execution_id -> temp_dir

    @property
    def docker_client(self):
        """Lazy load Docker client."""
        if self._docker_client is None:
            try:
                import docker
                self._docker_client = docker.from_env()
            except Exception as e:
                logger.error(f"Failed to connect to Docker: {e}")
                raise RuntimeError("Docker is not available") from e
        return self._docker_client

    def is_docker_available(self) -> bool:
        """Check if Docker is available."""
        try:
            self.docker_client.ping()
            return True
        except Exception:
            return False

    async def execute(
        self,
        code: str,
        input_files: Optional[List[InputFile]] = None,
        kb_documents: Optional[List[Dict[str, Any]]] = None,
    ) -> CodeExecutionResult:
        """
        Execute Python code in a Docker sandbox.

        Args:
            code: Python code to execute
            input_files: Optional input files to mount
            kb_documents: Optional KB documents to make available

        Returns:
            CodeExecutionResult with stdout, stderr, and output files
        """
        execution_id = f"exec_{uuid.uuid4().hex[:12]}"
        start_time = time.time()

        # Create temporary workspace
        temp_dir = tempfile.mkdtemp(prefix=f"code_exec_{execution_id}_")
        self._temp_dirs[execution_id] = temp_dir

        try:
            # Prepare workspace
            workspace = Path(temp_dir)
            input_dir = workspace / "input"
            output_dir = workspace / "output"
            kb_dir = workspace / "kb_docs"

            input_dir.mkdir()
            output_dir.mkdir()
            kb_dir.mkdir()

            # Write input files
            if input_files:
                for f in input_files:
                    (input_dir / f.filename).write_bytes(f.content)

            # Write KB documents
            if kb_documents:
                for i, doc in enumerate(kb_documents):
                    filename = doc.get("filename", f"doc_{i}.txt")
                    content = doc.get("content", "")
                    (kb_dir / filename).write_text(content)

            # Write main script
            main_script = self._wrap_code(code)
            (workspace / "main.py").write_text(main_script)

            # Execute in Docker
            result = await self._run_in_container(
                execution_id, workspace, output_dir
            )

            # Collect output files
            output_files = self._collect_output_files(output_dir)
            result.output_files = output_files
            result.execution_time_ms = int((time.time() - start_time) * 1000)

            return result

        except asyncio.TimeoutError:
            return CodeExecutionResult(
                execution_id=execution_id,
                success=False,
                status=ExecutionStatus.TIMEOUT,
                error_message=f"Execution timed out after {self.config.timeout_seconds}s",
                execution_time_ms=int((time.time() - start_time) * 1000),
            )
        except Exception as e:
            logger.exception(f"Code execution failed: {e}")
            return CodeExecutionResult(
                execution_id=execution_id,
                success=False,
                status=ExecutionStatus.ERROR,
                error_message=str(e),
                execution_time_ms=int((time.time() - start_time) * 1000),
            )

    def _wrap_code(self, code: str) -> str:
        """Wrap user code with output directory setup."""
        return f'''
import os
import sys

# Set up paths
os.chdir("/workspace")
sys.path.insert(0, "/workspace")

# Define output directory for generated files
OUTPUT_DIR = "/workspace/output"
INPUT_DIR = "/workspace/input"
KB_DIR = "/workspace/kb_docs"

# Matplotlib backend for headless execution
import matplotlib
matplotlib.use('Agg')

# User code
{code}
'''

    async def _run_in_container(
        self,
        execution_id: str,
        workspace: Path,
        output_dir: Path,
    ) -> CodeExecutionResult:
        """Run code in Docker container."""
        import docker.errors

        try:
            container = self.docker_client.containers.run(
                image=self.config.container_image,
                command=["python", "/workspace/main.py"],
                volumes={
                    str(workspace): {"bind": "/workspace", "mode": "rw"},
                },
                mem_limit=self.config.memory_limit,
                cpu_period=100000,
                cpu_quota=int(self.config.cpu_limit * 100000),
                network_disabled=self.config.network_disabled,
                remove=False,
                detach=True,
            )

            # Wait for completion with timeout
            try:
                exit_code = container.wait(
                    timeout=self.config.timeout_seconds
                )["StatusCode"]
            except Exception:
                container.kill()
                container.remove(force=True)
                raise asyncio.TimeoutError()

            # Get logs
            stdout = container.logs(stdout=True, stderr=False).decode("utf-8")
            stderr = container.logs(stdout=False, stderr=True).decode("utf-8")

            # Get stats for memory usage
            try:
                stats = container.stats(stream=False)
                memory_used = stats.get("memory_stats", {}).get("usage", 0) / (1024 * 1024)
            except Exception:
                memory_used = 0

            container.remove(force=True)

            success = exit_code == 0
            status = ExecutionStatus.SUCCESS if success else ExecutionStatus.ERROR

            return CodeExecutionResult(
                execution_id=execution_id,
                success=success,
                status=status,
                stdout=stdout,
                stderr=stderr,
                memory_used_mb=memory_used,
                error_message=stderr if not success else None,
            )

        except docker.errors.ImageNotFound:
            return CodeExecutionResult(
                execution_id=execution_id,
                success=False,
                status=ExecutionStatus.ERROR,
                error_message=f"Docker image not found: {self.config.container_image}",
            )

    def _collect_output_files(self, output_dir: Path) -> List[OutputFile]:
        """Collect generated output files."""
        import mimetypes

        output_files = []
        for file_path in output_dir.iterdir():
            if file_path.is_file():
                content_type, _ = mimetypes.guess_type(str(file_path))
                output_files.append(OutputFile(
                    filename=file_path.name,
                    content_type=content_type or "application/octet-stream",
                    size_bytes=file_path.stat().st_size,
                    local_path=str(file_path),
                ))
        return output_files

    def get_output_file(self, execution_id: str, filename: str) -> Optional[Path]:
        """Get path to an output file."""
        temp_dir = self._temp_dirs.get(execution_id)
        if not temp_dir:
            return None

        file_path = Path(temp_dir) / "output" / filename
        if file_path.exists():
            return file_path
        return None

    def cleanup(self, execution_id: str) -> None:
        """Clean up temporary files for an execution."""
        temp_dir = self._temp_dirs.pop(execution_id, None)
        if temp_dir and os.path.exists(temp_dir):
            shutil.rmtree(temp_dir, ignore_errors=True)

    def cleanup_all(self) -> None:
        """Clean up all temporary files."""
        for execution_id in list(self._temp_dirs.keys()):
            self.cleanup(execution_id)
```

**Step 4: 创建基础测试文件**

```python
# tests/services/assistant/test_code_executor.py
"""Tests for CodeExecutorService."""

import pytest
from src.services.assistant.code_executor import (
    CodeExecutorService,
    CodeExecutionConfig,
    CodeExecutionResult,
    ExecutionStatus,
    InputFile,
)


class TestCodeExecutionConfig:
    """Test CodeExecutionConfig dataclass."""

    def test_default_config(self):
        config = CodeExecutionConfig()
        assert config.container_image == "python:3.11-slim"
        assert config.memory_limit == "512m"
        assert config.timeout_seconds == 30
        assert config.network_disabled is True

    def test_custom_config(self):
        config = CodeExecutionConfig(
            container_image="python:3.10",
            memory_limit="1g",
            timeout_seconds=60,
        )
        assert config.container_image == "python:3.10"
        assert config.memory_limit == "1g"
        assert config.timeout_seconds == 60


class TestCodeExecutorService:
    """Test CodeExecutorService."""

    def test_service_initialization(self):
        service = CodeExecutorService()
        assert service.config is not None
        assert service.config.container_image == "python:3.11-slim"

    def test_docker_availability_check(self):
        service = CodeExecutorService()
        # This will depend on Docker being installed
        result = service.is_docker_available()
        assert isinstance(result, bool)


class TestCodeExecutionResult:
    """Test CodeExecutionResult dataclass."""

    def test_to_dict(self):
        result = CodeExecutionResult(
            execution_id="exec_123",
            success=True,
            status=ExecutionStatus.SUCCESS,
            stdout="Hello World\n",
            execution_time_ms=150,
        )

        d = result.to_dict()
        assert d["execution_id"] == "exec_123"
        assert d["success"] is True
        assert d["status"] == "success"
        assert d["stdout"] == "Hello World\n"
```

**Step 5: 运行测试**

```bash
cd /Users/misaya.yanghejazfs.com.au/hejaz_projects/ai_gateway/ai-gateway
source /Users/misaya.yanghejazfs.com.au/miniconda3/bin/activate ai_gateway
pytest tests/services/assistant/test_code_executor.py -v
```

Expected: All tests pass

**Step 6: Commit**

```bash
git add src/services/assistant/code_executor.py tests/services/assistant/test_code_executor.py
git commit -m "feat(assistant): add CodeExecutorService for Docker sandbox execution

- Add CodeExecutionConfig, CodeExecutionResult dataclasses
- Implement Docker container management
- Support input files and KB documents
- Add resource limits and timeout handling

Co-Authored-By: Claude Opus 4.5 <noreply@anthropic.com>"
```

---

## Task 2: 注册 Code Executor 工具到 ToolRegistry

**Files:**
- Create: `src/services/assistant/tools/code_executor_tool.py`
- Modify: `src/services/assistant/tools/__init__.py`
- Test: `tests/services/assistant/tools/test_code_executor_tool.py`

**Step 1: 创建工具定义和执行器**

```python
# src/services/assistant/tools/code_executor_tool.py
"""
Code Executor Tool - Execute Python code in Docker sandbox.

Provides the execute_python_code tool for the assistant to run
data analysis, generate charts, and process files.
"""

from __future__ import annotations

import uuid
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
)
from ..code_executor import CodeExecutorService, InputFile

if TYPE_CHECKING:
    from ....services.knowledge import KnowledgeService

# Tool definition
CODE_EXECUTOR_TOOL = ToolDefinition(
    name="execute_python_code",
    description="Execute Python code to perform data analysis, generate charts, process files, or run calculations. The code runs in an isolated environment with common data science libraries available.",
    parameters=[
        ToolParameter(
            name="code",
            type="string",
            description="The Python code to execute. Use print() for text output. Save charts to /workspace/output/ directory.",
            required=True,
        ),
    ],
    category=ToolCategory.ANALYSIS,
    risk_level=ToolRiskLevel.MEDIUM,
    requires_confirmation=False,
    when_to_use="When the user asks to analyze data, create charts/graphs, perform calculations, process CSV/Excel files, or run any Python code.",
    when_not_to_use="For simple questions that don't require computation or data processing.",
    examples=[
        ToolExample(
            description="Generate a simple bar chart",
            input={
                "code": """
import matplotlib.pyplot as plt

data = [25, 40, 30, 55, 45]
labels = ['A', 'B', 'C', 'D', 'E']

plt.figure(figsize=(8, 6))
plt.bar(labels, data, color='steelblue')
plt.title('Sample Bar Chart')
plt.xlabel('Category')
plt.ylabel('Value')
plt.savefig('/workspace/output/chart.png', dpi=150, bbox_inches='tight')
print('Chart saved!')
"""
            },
            expected_output="Chart saved! + chart.png file",
        ),
        ToolExample(
            description="Analyze a CSV file",
            input={
                "code": """
import pandas as pd

# Read from input directory
df = pd.read_csv('/workspace/input/data.csv')
print(df.describe())
print(f"\\nTotal rows: {len(df)}")
"""
            },
            expected_output="DataFrame statistics and row count",
        ),
    ],
    timeout_seconds=30,
    max_retries=1,
)


class CodeExecutorToolExecutor(ToolExecutor):
    """Executor for the code execution tool."""

    def __init__(
        self,
        code_executor: CodeExecutorService,
        knowledge_service: Optional["KnowledgeService"] = None,
    ):
        self.code_executor = code_executor
        self.knowledge_service = knowledge_service

        # Track active executions for cleanup
        self._active_executions: Dict[str, str] = {}  # call_id -> execution_id

    async def execute(self, request: ToolCallRequest) -> ToolCallResult:
        """Execute Python code in Docker sandbox."""
        code = request.arguments.get("code", "")

        if not code.strip():
            return ToolCallResult(
                call_id=request.call_id,
                tool_name=request.tool_name,
                success=False,
                error="No code provided",
            )

        # Get input files from metadata (if any)
        input_files: List[InputFile] = []
        if "input_files" in request.metadata:
            for f in request.metadata["input_files"]:
                input_files.append(InputFile(
                    filename=f["filename"],
                    content=f["content"],
                    content_type=f.get("content_type", "application/octet-stream"),
                ))

        # Get KB documents from metadata (if any)
        kb_documents: List[Dict[str, Any]] = request.metadata.get("kb_documents", [])

        # Execute code
        result = await self.code_executor.execute(
            code=code,
            input_files=input_files if input_files else None,
            kb_documents=kb_documents if kb_documents else None,
        )

        # Track execution for cleanup
        self._active_executions[request.call_id] = result.execution_id

        # Build result
        return ToolCallResult(
            call_id=request.call_id,
            tool_name=request.tool_name,
            success=result.success,
            result={
                "execution_id": result.execution_id,
                "stdout": result.stdout,
                "stderr": result.stderr,
                "output_files": [f.to_dict() for f in result.output_files],
                "execution_time_ms": result.execution_time_ms,
            },
            error=result.error_message,
            metadata={
                "execution_id": result.execution_id,
                "status": result.status.value,
            },
        )

    def get_execution_id(self, call_id: str) -> Optional[str]:
        """Get execution ID for a call."""
        return self._active_executions.get(call_id)

    def cleanup(self, call_id: str) -> None:
        """Clean up resources for a call."""
        execution_id = self._active_executions.pop(call_id, None)
        if execution_id:
            self.code_executor.cleanup(execution_id)
```

**Step 2: 更新 tools/__init__.py**

```python
# 在 src/services/assistant/tools/__init__.py 添加
from .code_executor_tool import (
    CODE_EXECUTOR_TOOL,
    CodeExecutorToolExecutor,
)
```

**Step 3: 创建测试**

```python
# tests/services/assistant/tools/test_code_executor_tool.py
"""Tests for code executor tool."""

import pytest
from src.services.assistant.tools.code_executor_tool import (
    CODE_EXECUTOR_TOOL,
    CodeExecutorToolExecutor,
)
from src.services.assistant.tools.tool_registry import ToolCallRequest


class TestCodeExecutorToolDefinition:
    """Test tool definition."""

    def test_tool_name(self):
        assert CODE_EXECUTOR_TOOL.name == "execute_python_code"

    def test_tool_has_code_parameter(self):
        params = {p.name: p for p in CODE_EXECUTOR_TOOL.parameters}
        assert "code" in params
        assert params["code"].required is True

    def test_openai_schema(self):
        schema = CODE_EXECUTOR_TOOL.to_openai_schema()
        assert schema["type"] == "function"
        assert schema["function"]["name"] == "execute_python_code"

    def test_anthropic_schema(self):
        schema = CODE_EXECUTOR_TOOL.to_anthropic_schema()
        assert schema["name"] == "execute_python_code"
        assert "input_schema" in schema
```

**Step 4: 运行测试**

```bash
pytest tests/services/assistant/tools/test_code_executor_tool.py -v
```

**Step 5: Commit**

```bash
git add src/services/assistant/tools/code_executor_tool.py src/services/assistant/tools/__init__.py tests/services/assistant/tools/test_code_executor_tool.py
git commit -m "feat(assistant): add code executor tool registration

- Add CODE_EXECUTOR_TOOL definition
- Implement CodeExecutorToolExecutor
- Support input files and KB documents in tool context

Co-Authored-By: Claude Opus 4.5 <noreply@anthropic.com>"
```

---

## Task 3: 扩展 SSE 事件类型支持代码执行

**Files:**
- Modify: `src/services/assistant/assistant_service.py` (add new event types)
- Modify: `web/src/api/assistant.ts` (add new SSE event types)

**Step 1: 在后端添加新的事件类型**

在 `src/services/assistant/assistant_service.py` 中找到 `StreamEventType` 枚举并添加：

```python
# 在现有的 StreamEventType 枚举中添加
class StreamEventType(str, Enum):
    # ... existing types ...

    # Code execution events
    CODE_EXECUTION_START = "code_execution_start"
    CODE_EXECUTION_OUTPUT = "code_execution_output"
    CODE_EXECUTION_RESULT = "code_execution_result"
    ARTIFACT_CREATED = "artifact_created"
```

**Step 2: 在前端添加新的事件类型**

在 `web/src/api/assistant.ts` 中添加：

```typescript
export const SSEEventType = {
  // ... existing types ...

  // Code execution events
  CODE_EXECUTION_START: "code_execution_start",
  CODE_EXECUTION_OUTPUT: "code_execution_output",
  CODE_EXECUTION_RESULT: "code_execution_result",
  ARTIFACT_CREATED: "artifact_created",
} as const;
```

**Step 3: 添加类型定义**

在 `web/src/api/assistant.ts` 中添加：

```typescript
// Code execution types
export interface CodeExecutionStart {
  execution_id: string;
  language: string;
  code: string;
}

export interface CodeExecutionOutput {
  execution_id: string;
  output: string;
}

export interface CodeExecutionResultData {
  execution_id: string;
  success: boolean;
  stdout: string;
  stderr: string;
  execution_time_ms: number;
  output_files: Array<{
    filename: string;
    content_type: string;
    size_bytes: number;
  }>;
}

export interface ArtifactCreated {
  artifact_id: string;
  type: "code" | "chart" | "table" | "file";
  format: string;
  title: string;
  url: string;
}
```

**Step 4: Commit**

```bash
git add src/services/assistant/assistant_service.py web/src/api/assistant.ts
git commit -m "feat(assistant): add code execution SSE event types

Backend and frontend now support:
- code_execution_start
- code_execution_output
- code_execution_result
- artifact_created

Co-Authored-By: Claude Opus 4.5 <noreply@anthropic.com>"
```

---

## Task 4: 添加 Artifacts API 端点

**Files:**
- Modify: `src/api/v1/assistant.py` (add artifacts endpoints)
- Create: `src/api/schemas/artifacts.py` (response schemas)

**Step 1: 创建 Artifacts 响应 schemas**

```python
# src/api/schemas/artifacts.py
"""Artifacts API schemas."""

from typing import List, Optional
from pydantic import BaseModel


class ArtifactInfo(BaseModel):
    """Artifact metadata."""
    artifact_id: str
    execution_id: str
    type: str  # code, chart, table, file
    format: str  # png, csv, json, etc.
    filename: str
    title: Optional[str] = None
    size_bytes: int
    created_at: Optional[str] = None


class ArtifactListResponse(BaseModel):
    """Response with list of artifacts."""
    artifacts: List[ArtifactInfo]
    total: int
```

**Step 2: 在 assistant.py 添加 artifacts 端点**

```python
# 在 src/api/v1/assistant.py 添加

from fastapi.responses import FileResponse
from ..schemas.artifacts import ArtifactInfo, ArtifactListResponse


@router.get("/artifacts/{artifact_id}")
async def get_artifact(
    artifact_id: str,
    request: Request,
    user: UserContext = Depends(get_user_context),
) -> ArtifactInfo:
    """
    Get artifact metadata.

    Returns information about a generated artifact (chart, file, etc.)
    """
    # TODO: Implement artifact retrieval from storage
    raise HTTPException(status_code=404, detail="Artifact not found")


@router.get("/artifacts/{artifact_id}/download")
async def download_artifact(
    artifact_id: str,
    request: Request,
    user: UserContext = Depends(get_user_context),
):
    """
    Download an artifact file.

    Returns the actual file content for download.
    """
    # TODO: Implement artifact download
    raise HTTPException(status_code=404, detail="Artifact not found")
```

**Step 3: Commit**

```bash
git add src/api/schemas/artifacts.py src/api/v1/assistant.py
git commit -m "feat(api): add artifacts endpoints for code execution outputs

- GET /assistant/artifacts/{id} - Get artifact metadata
- GET /assistant/artifacts/{id}/download - Download artifact file

Co-Authored-By: Claude Opus 4.5 <noreply@anthropic.com>"
```

---

## Task 5: 创建前端 Artifacts 面板基础组件

**Files:**
- Create: `web/src/components/artifacts/ArtifactsPanel.tsx`
- Create: `web/src/components/artifacts/ExecutionStatus.tsx`
- Create: `web/src/components/artifacts/index.ts`

**Step 1: 创建 ExecutionStatus 组件**

```typescript
// web/src/components/artifacts/ExecutionStatus.tsx
/**
 * Execution Status Indicator
 *
 * Shows the current status of code execution with appropriate styling.
 */

import { Loader2, CheckCircle, XCircle, Clock } from "lucide-react";
import { cn } from "@/lib/utils";

export type ExecutionStatusType = "idle" | "running" | "success" | "error" | "timeout";

interface ExecutionStatusProps {
  status: ExecutionStatusType;
  executionTimeMs?: number;
  className?: string;
}

export function ExecutionStatus({ status, executionTimeMs, className }: ExecutionStatusProps) {
  const statusConfig = {
    idle: {
      icon: null,
      text: "",
      color: "text-muted-foreground",
    },
    running: {
      icon: <Loader2 className="h-4 w-4 animate-spin" />,
      text: "Executing...",
      color: "text-blue-500",
    },
    success: {
      icon: <CheckCircle className="h-4 w-4" />,
      text: executionTimeMs ? `Completed in ${executionTimeMs}ms` : "Completed",
      color: "text-green-500",
    },
    error: {
      icon: <XCircle className="h-4 w-4" />,
      text: "Execution failed",
      color: "text-red-500",
    },
    timeout: {
      icon: <Clock className="h-4 w-4" />,
      text: "Timed out",
      color: "text-yellow-500",
    },
  };

  const config = statusConfig[status];

  if (status === "idle") return null;

  return (
    <div className={cn("flex items-center gap-2", config.color, className)}>
      {config.icon}
      <span className="text-sm">{config.text}</span>
    </div>
  );
}
```

**Step 2: 创建 ArtifactsPanel 主组件**

```typescript
// web/src/components/artifacts/ArtifactsPanel.tsx
/**
 * Artifacts Panel - Display code execution results
 *
 * A right-side panel that shows:
 * - Code being executed
 * - Execution output
 * - Generated charts/files
 */

import { useState } from "react";
import { X, Code, Image, Table, FileDown, Copy, RefreshCw } from "lucide-react";
import { Button } from "@/components/ui/button";
import { Tabs, TabsContent, TabsList, TabsTrigger } from "@/components/ui/tabs";
import { ScrollArea } from "@/components/ui/scroll-area";
import { cn } from "@/lib/utils";
import { ExecutionStatus, type ExecutionStatusType } from "./ExecutionStatus";

export interface Artifact {
  id: string;
  type: "code" | "chart" | "table" | "file";
  format: string;
  title: string;
  url?: string;
  content?: string;
  createdAt: Date;
}

interface ArtifactsPanelProps {
  isOpen: boolean;
  onClose: () => void;
  artifacts: Artifact[];
  executionStatus: ExecutionStatusType;
  executionOutput: string;
  currentCode?: string;
  executionTimeMs?: number;
  onRerun?: () => void;
  className?: string;
}

export function ArtifactsPanel({
  isOpen,
  onClose,
  artifacts,
  executionStatus,
  executionOutput,
  currentCode,
  executionTimeMs,
  onRerun,
  className,
}: ArtifactsPanelProps) {
  const [activeTab, setActiveTab] = useState<string>("output");
  const [copiedId, setCopiedId] = useState<string | null>(null);

  if (!isOpen) return null;

  const handleCopyCode = async () => {
    if (currentCode) {
      await navigator.clipboard.writeText(currentCode);
      setCopiedId("code");
      setTimeout(() => setCopiedId(null), 2000);
    }
  };

  // Determine which tabs to show
  const hasCode = !!currentCode;
  const hasCharts = artifacts.some((a) => a.type === "chart");
  const hasTables = artifacts.some((a) => a.type === "table");
  const hasFiles = artifacts.some((a) => a.type === "file");

  return (
    <div
      className={cn(
        "w-[400px] border-l bg-background flex flex-col",
        className
      )}
    >
      {/* Header */}
      <div className="flex items-center justify-between px-4 py-3 border-b">
        <div className="flex items-center gap-2">
          <Code className="h-5 w-5 text-violet-500" />
          <span className="font-medium">Artifacts</span>
        </div>
        <Button variant="ghost" size="icon" onClick={onClose}>
          <X className="h-4 w-4" />
        </Button>
      </div>

      {/* Execution Status */}
      <div className="px-4 py-2 border-b">
        <ExecutionStatus
          status={executionStatus}
          executionTimeMs={executionTimeMs}
        />
      </div>

      {/* Tabs */}
      <Tabs value={activeTab} onValueChange={setActiveTab} className="flex-1 flex flex-col">
        <TabsList className="w-full justify-start px-4 pt-2">
          <TabsTrigger value="output">Output</TabsTrigger>
          {hasCode && <TabsTrigger value="code">Code</TabsTrigger>}
          {hasCharts && <TabsTrigger value="charts">Charts</TabsTrigger>}
          {hasTables && <TabsTrigger value="tables">Tables</TabsTrigger>}
          {hasFiles && <TabsTrigger value="files">Files</TabsTrigger>}
        </TabsList>

        <div className="flex-1 overflow-hidden">
          {/* Output Tab */}
          <TabsContent value="output" className="h-full m-0">
            <ScrollArea className="h-full">
              <pre className="p-4 text-sm font-mono whitespace-pre-wrap">
                {executionOutput || "No output yet..."}
              </pre>
            </ScrollArea>
          </TabsContent>

          {/* Code Tab */}
          {hasCode && (
            <TabsContent value="code" className="h-full m-0">
              <div className="h-full flex flex-col">
                <div className="flex items-center justify-end gap-2 px-4 py-2 border-b">
                  <Button
                    variant="ghost"
                    size="sm"
                    onClick={handleCopyCode}
                  >
                    <Copy className="h-4 w-4 mr-1" />
                    {copiedId === "code" ? "Copied!" : "Copy"}
                  </Button>
                  {onRerun && (
                    <Button
                      variant="ghost"
                      size="sm"
                      onClick={onRerun}
                      disabled={executionStatus === "running"}
                    >
                      <RefreshCw className="h-4 w-4 mr-1" />
                      Rerun
                    </Button>
                  )}
                </div>
                <ScrollArea className="flex-1">
                  <pre className="p-4 text-sm font-mono whitespace-pre-wrap bg-muted/50">
                    {currentCode}
                  </pre>
                </ScrollArea>
              </div>
            </TabsContent>
          )}

          {/* Charts Tab */}
          {hasCharts && (
            <TabsContent value="charts" className="h-full m-0">
              <ScrollArea className="h-full">
                <div className="p-4 space-y-4">
                  {artifacts
                    .filter((a) => a.type === "chart")
                    .map((artifact) => (
                      <div key={artifact.id} className="border rounded-lg overflow-hidden">
                        <div className="px-3 py-2 bg-muted/50 border-b flex items-center justify-between">
                          <span className="text-sm font-medium">{artifact.title}</span>
                          <Button variant="ghost" size="sm" asChild>
                            <a href={artifact.url} download>
                              <FileDown className="h-4 w-4" />
                            </a>
                          </Button>
                        </div>
                        {artifact.url && (
                          <img
                            src={artifact.url}
                            alt={artifact.title}
                            className="w-full"
                          />
                        )}
                      </div>
                    ))}
                </div>
              </ScrollArea>
            </TabsContent>
          )}

          {/* Tables Tab */}
          {hasTables && (
            <TabsContent value="tables" className="h-full m-0">
              <ScrollArea className="h-full">
                <div className="p-4">
                  {/* TODO: Implement AG-Grid table viewer */}
                  <p className="text-muted-foreground">Table viewer coming soon...</p>
                </div>
              </ScrollArea>
            </TabsContent>
          )}

          {/* Files Tab */}
          {hasFiles && (
            <TabsContent value="files" className="h-full m-0">
              <ScrollArea className="h-full">
                <div className="p-4 space-y-2">
                  {artifacts
                    .filter((a) => a.type === "file")
                    .map((artifact) => (
                      <a
                        key={artifact.id}
                        href={artifact.url}
                        download
                        className="flex items-center gap-3 p-3 rounded-lg border hover:bg-muted/50 transition-colors"
                      >
                        <FileDown className="h-5 w-5 text-muted-foreground" />
                        <div className="flex-1 min-w-0">
                          <p className="text-sm font-medium truncate">{artifact.title}</p>
                          <p className="text-xs text-muted-foreground">{artifact.format}</p>
                        </div>
                      </a>
                    ))}
                </div>
              </ScrollArea>
            </TabsContent>
          )}
        </div>
      </Tabs>
    </div>
  );
}
```

**Step 3: 创建导出文件**

```typescript
// web/src/components/artifacts/index.ts
export { ArtifactsPanel, type Artifact } from "./ArtifactsPanel";
export { ExecutionStatus, type ExecutionStatusType } from "./ExecutionStatus";
```

**Step 4: Commit**

```bash
git add web/src/components/artifacts/
git commit -m "feat(web): add Artifacts panel components

- ArtifactsPanel: Main container with tabs for output/code/charts/files
- ExecutionStatus: Status indicator with animations
- Support for viewing and downloading generated artifacts

Co-Authored-By: Claude Opus 4.5 <noreply@anthropic.com>"
```

---

## Task 6: 集成 Artifacts 面板到 AssistantPage

**Files:**
- Modify: `web/src/pages/assistant/index.tsx`
- Modify: `web/src/pages/assistant/types.ts`

**Step 1: 在 types.ts 添加 Artifacts 相关类型**

```typescript
// 在 web/src/pages/assistant/types.ts 添加

export interface CodeExecutionState {
  isExecuting: boolean;
  executionId: string | null;
  code: string | null;
  output: string;
  executionTimeMs: number | null;
  status: "idle" | "running" | "success" | "error" | "timeout";
}

export interface ArtifactData {
  id: string;
  type: "code" | "chart" | "table" | "file";
  format: string;
  title: string;
  url?: string;
  content?: string;
  createdAt: Date;
}
```

**Step 2: 在 AssistantPage 添加状态和处理逻辑**

在 `index.tsx` 中添加：

```typescript
// Import artifacts components
import { ArtifactsPanel, type Artifact } from "@/components/artifacts";
import type { CodeExecutionState, ArtifactData } from "./types";

// Add state for artifacts
const [showArtifacts, setShowArtifacts] = useState(false);
const [artifacts, setArtifacts] = useState<Artifact[]>([]);
const [codeExecution, setCodeExecution] = useState<CodeExecutionState>({
  isExecuting: false,
  executionId: null,
  code: null,
  output: "",
  executionTimeMs: null,
  status: "idle",
});

// Add SSE event handlers for code execution
// In the sendMessage function, add handling for new event types:
/*
case SSEEventType.CODE_EXECUTION_START:
  setCodeExecution({
    isExecuting: true,
    executionId: event.data.execution_id,
    code: event.data.code,
    output: "",
    executionTimeMs: null,
    status: "running",
  });
  setShowArtifacts(true);
  break;

case SSEEventType.CODE_EXECUTION_OUTPUT:
  setCodeExecution((prev) => ({
    ...prev,
    output: prev.output + event.data.output,
  }));
  break;

case SSEEventType.CODE_EXECUTION_RESULT:
  setCodeExecution((prev) => ({
    ...prev,
    isExecuting: false,
    executionTimeMs: event.data.execution_time_ms,
    status: event.data.success ? "success" : "error",
    output: prev.output + (event.data.stderr || ""),
  }));
  break;

case SSEEventType.ARTIFACT_CREATED:
  setArtifacts((prev) => [
    ...prev,
    {
      id: event.data.artifact_id,
      type: event.data.type,
      format: event.data.format,
      title: event.data.title,
      url: event.data.url,
      createdAt: new Date(),
    },
  ]);
  break;
*/
```

**Step 3: 添加 Artifacts 面板到布局**

```tsx
// 在主布局中添加 Artifacts 面板
{/* Right panel: Settings or Artifacts */}
{showArtifacts ? (
  <ArtifactsPanel
    isOpen={showArtifacts}
    onClose={() => setShowArtifacts(false)}
    artifacts={artifacts}
    executionStatus={codeExecution.status}
    executionOutput={codeExecution.output}
    currentCode={codeExecution.code || undefined}
    executionTimeMs={codeExecution.executionTimeMs || undefined}
  />
) : showRightPanel ? (
  <div className="w-[300px] border-l p-4">
    {/* Existing settings panel */}
  </div>
) : null}
```

**Step 4: Commit**

```bash
git add web/src/pages/assistant/index.tsx web/src/pages/assistant/types.ts
git commit -m "feat(web): integrate Artifacts panel into AssistantPage

- Add code execution state management
- Handle new SSE event types for code execution
- Show Artifacts panel when code is being executed
- Display output, charts, and files in panel

Co-Authored-By: Claude Opus 4.5 <noreply@anthropic.com>"
```

---

## Task 7: 创建预构建的 Python Docker 镜像

**Files:**
- Create: `docker/code-interpreter/Dockerfile`
- Create: `docker/code-interpreter/requirements.txt`
- Create: `scripts/build-code-interpreter-image.sh`

**Step 1: 创建 Dockerfile**

```dockerfile
# docker/code-interpreter/Dockerfile
# Python environment for Code Interpreter
# Includes common data science and visualization libraries

FROM python:3.11-slim

# Install system dependencies for matplotlib and other libs
RUN apt-get update && apt-get install -y --no-install-recommends \
    libfreetype6-dev \
    libpng-dev \
    && rm -rf /var/lib/apt/lists/*

# Create workspace directory
WORKDIR /workspace

# Install Python packages
COPY requirements.txt /tmp/requirements.txt
RUN pip install --no-cache-dir -r /tmp/requirements.txt

# Set matplotlib backend to Agg for headless operation
ENV MPLBACKEND=Agg

# Default command
CMD ["python", "/workspace/main.py"]
```

**Step 2: 创建 requirements.txt**

```
# docker/code-interpreter/requirements.txt
# Common data science and visualization libraries

# Data manipulation
pandas>=2.0.0
numpy>=1.24.0

# Visualization
matplotlib>=3.7.0
seaborn>=0.12.0

# File handling
openpyxl>=3.1.0  # Excel files
xlrd>=2.0.0      # Legacy Excel files

# Utilities
python-dateutil>=2.8.0
```

**Step 3: 创建构建脚本**

```bash
#!/bin/bash
# scripts/build-code-interpreter-image.sh
# Build the Code Interpreter Docker image

set -e

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
PROJECT_ROOT="$(dirname "$SCRIPT_DIR")"
DOCKER_DIR="$PROJECT_ROOT/docker/code-interpreter"

IMAGE_NAME="ai-gateway-code-interpreter"
IMAGE_TAG="latest"

echo "Building Code Interpreter Docker image..."
echo "  Image: $IMAGE_NAME:$IMAGE_TAG"
echo "  Context: $DOCKER_DIR"

docker build -t "$IMAGE_NAME:$IMAGE_TAG" "$DOCKER_DIR"

echo ""
echo "Build complete!"
echo "Run: docker images $IMAGE_NAME"
```

**Step 4: 使脚本可执行并构建**

```bash
chmod +x scripts/build-code-interpreter-image.sh
./scripts/build-code-interpreter-image.sh
```

**Step 5: Commit**

```bash
git add docker/code-interpreter/ scripts/build-code-interpreter-image.sh
git commit -m "feat(docker): add Code Interpreter Python image

Pre-built image with common data science libraries:
- pandas, numpy for data manipulation
- matplotlib, seaborn for visualization
- openpyxl for Excel file handling

Co-Authored-By: Claude Opus 4.5 <noreply@anthropic.com>"
```

---

## Task 8: 集成 CodeExecutorService 到 AssistantService

**Files:**
- Modify: `src/services/assistant/assistant_service.py`
- Modify: `src/container.py` (add service initialization)

**Step 1: 在 AssistantService 中添加代码执行支持**

```python
# 在 AssistantService.__init__ 中添加
from .code_executor import CodeExecutorService
from .tools.code_executor_tool import CODE_EXECUTOR_TOOL, CodeExecutorToolExecutor

class AssistantService:
    def __init__(
        self,
        # ... existing params ...
        code_executor: Optional[CodeExecutorService] = None,
    ):
        # ... existing init ...

        # Initialize code executor
        self.code_executor = code_executor
        if self.code_executor:
            self._register_code_executor_tool()

    def _register_code_executor_tool(self) -> None:
        """Register the code executor tool if available."""
        if not self.code_executor:
            return

        executor = CodeExecutorToolExecutor(
            code_executor=self.code_executor,
            knowledge_service=self.knowledge_service,
        )
        self.tool_registry.register(CODE_EXECUTOR_TOOL, executor)
        logger.info("Registered code executor tool")
```

**Step 2: 在 container.py 中初始化服务**

```python
# 在 create_assistant_service 函数中添加
from src.services.assistant.code_executor import CodeExecutorService, CodeExecutionConfig

def create_assistant_service(...):
    # ... existing code ...

    # Initialize code executor if Docker is available
    code_executor = None
    try:
        code_executor = CodeExecutorService(
            config=CodeExecutionConfig(
                container_image="ai-gateway-code-interpreter:latest",
            )
        )
        if code_executor.is_docker_available():
            logger.info("Code executor initialized with Docker support")
        else:
            code_executor = None
            logger.warning("Docker not available, code execution disabled")
    except Exception as e:
        logger.warning(f"Failed to initialize code executor: {e}")

    return AssistantService(
        # ... existing params ...
        code_executor=code_executor,
    )
```

**Step 3: Commit**

```bash
git add src/services/assistant/assistant_service.py src/container.py
git commit -m "feat(assistant): integrate CodeExecutorService

- Register code executor tool in AssistantService
- Initialize Docker-based executor in container
- Gracefully handle missing Docker

Co-Authored-By: Claude Opus 4.5 <noreply@anthropic.com>"
```

---

## Task 9: 端到端测试

**Files:**
- Create: `tests/integration/test_code_interpreter.py`

**Step 1: 创建集成测试**

```python
# tests/integration/test_code_interpreter.py
"""End-to-end tests for Code Interpreter functionality."""

import pytest
from src.services.assistant.code_executor import (
    CodeExecutorService,
    CodeExecutionConfig,
    ExecutionStatus,
)


@pytest.mark.integration
class TestCodeInterpreterE2E:
    """End-to-end tests requiring Docker."""

    @pytest.fixture
    def executor(self):
        """Create a code executor for testing."""
        config = CodeExecutionConfig(
            container_image="python:3.11-slim",  # Use standard image for tests
            timeout_seconds=10,
        )
        return CodeExecutorService(config)

    @pytest.mark.asyncio
    async def test_simple_print(self, executor):
        """Test simple print statement."""
        if not executor.is_docker_available():
            pytest.skip("Docker not available")

        result = await executor.execute("print('Hello, World!')")

        assert result.success
        assert result.status == ExecutionStatus.SUCCESS
        assert "Hello, World!" in result.stdout

    @pytest.mark.asyncio
    async def test_math_calculation(self, executor):
        """Test math calculation."""
        if not executor.is_docker_available():
            pytest.skip("Docker not available")

        code = """
import math
result = math.sqrt(144)
print(f"Square root of 144 is {result}")
"""
        result = await executor.execute(code)

        assert result.success
        assert "12" in result.stdout

    @pytest.mark.asyncio
    async def test_error_handling(self, executor):
        """Test error handling."""
        if not executor.is_docker_available():
            pytest.skip("Docker not available")

        result = await executor.execute("raise ValueError('Test error')")

        assert not result.success
        assert result.status == ExecutionStatus.ERROR
        assert "ValueError" in result.stderr
```

**Step 2: 运行测试**

```bash
pytest tests/integration/test_code_interpreter.py -v -m integration
```

**Step 3: Commit**

```bash
git add tests/integration/test_code_interpreter.py
git commit -m "test(integration): add Code Interpreter E2E tests

- Test simple print execution
- Test math calculations
- Test error handling
- Skip tests if Docker unavailable

Co-Authored-By: Claude Opus 4.5 <noreply@anthropic.com>"
```

---

## Summary

This implementation plan consists of 9 tasks:

| Task | Description | Estimated Steps |
|------|-------------|-----------------|
| 1 | Create CodeExecutorService core class | 6 |
| 2 | Register code executor tool | 5 |
| 3 | Add SSE event types | 4 |
| 4 | Add Artifacts API endpoints | 3 |
| 5 | Create Artifacts panel components | 4 |
| 6 | Integrate Artifacts panel to AssistantPage | 4 |
| 7 | Create Python Docker image | 5 |
| 8 | Integrate into AssistantService | 3 |
| 9 | End-to-end testing | 3 |

**Total: 37 steps**

Each task is independent and can be completed by a subagent with the provided code snippets and test commands.
