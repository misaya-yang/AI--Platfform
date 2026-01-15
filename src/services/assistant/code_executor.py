"""
Code Executor Service - Execute Python code in Docker sandbox containers.

Provides safe execution of Python code with:
- Docker container isolation
- Resource limits (memory, CPU, timeout)
- Network isolation
- File I/O support (input files, output files, KB documents)
- Automatic cleanup
"""

from __future__ import annotations

import asyncio
import base64
import os
import shutil
import tempfile
import time
import uuid
from dataclasses import dataclass, field
from datetime import datetime
from enum import Enum
from pathlib import Path
from typing import Any, Dict, List, Optional, TYPE_CHECKING

from ...core.observability.logging import get_logger

logger = get_logger(__name__)


# =============================================================================
# Data Classes
# =============================================================================


class ExecutionStatus(str, Enum):
    """Status of code execution."""
    PENDING = "pending"
    RUNNING = "running"
    SUCCESS = "success"
    ERROR = "error"
    TIMEOUT = "timeout"
    CANCELLED = "cancelled"


@dataclass
class CodeExecutionConfig:
    """Configuration for code execution environment."""
    # Resource limits
    memory_limit: str = "512m"
    cpu_limit: float = 0.5
    timeout_seconds: int = 30

    # Security settings
    network_disabled: bool = True
    read_only_root: bool = False

    # Docker image
    image: str = "python:3.11-slim"

    # Workspace paths (inside container)
    workspace_path: str = "/workspace"
    input_path: str = "/workspace/input"
    output_path: str = "/workspace/output"
    kb_docs_path: str = "/workspace/kb_docs"
    main_script_path: str = "/workspace/main.py"

    # Additional packages to install (pip install)
    packages: List[str] = field(default_factory=lambda: [
        "numpy",
        "pandas",
        "matplotlib",
    ])


@dataclass
class InputFile:
    """Input file to be provided to the code execution."""
    filename: str
    content: bytes
    mime_type: Optional[str] = None

    @classmethod
    def from_base64(cls, filename: str, data: str, mime_type: Optional[str] = None) -> "InputFile":
        """Create InputFile from base64 encoded data."""
        content = base64.b64decode(data)
        return cls(filename=filename, content=content, mime_type=mime_type)

    @classmethod
    def from_text(cls, filename: str, text: str, encoding: str = "utf-8") -> "InputFile":
        """Create InputFile from text content."""
        content = text.encode(encoding)
        return cls(filename=filename, content=content, mime_type="text/plain")


@dataclass
class OutputFile:
    """Output file produced by code execution."""
    filename: str
    content: bytes
    mime_type: Optional[str] = None
    size_bytes: int = 0

    def to_base64(self) -> str:
        """Convert content to base64 string."""
        return base64.b64encode(self.content).decode("utf-8")

    def to_text(self, encoding: str = "utf-8") -> str:
        """Convert content to text (for text files)."""
        return self.content.decode(encoding)


@dataclass
class KBDocument:
    """Knowledge base document to be provided to code execution."""
    filename: str
    content: str
    document_id: Optional[str] = None
    metadata: Dict[str, Any] = field(default_factory=dict)


@dataclass
class CodeExecutionResult:
    """Result of code execution."""
    execution_id: str
    status: ExecutionStatus

    # Output
    stdout: str = ""
    stderr: str = ""
    output_files: List[OutputFile] = field(default_factory=list)

    # Timing
    started_at: Optional[datetime] = None
    completed_at: Optional[datetime] = None
    duration_ms: float = 0.0

    # Error info
    error_message: Optional[str] = None
    exit_code: Optional[int] = None

    # Resource usage
    memory_used_bytes: Optional[int] = None

    def is_success(self) -> bool:
        """Check if execution was successful."""
        return self.status == ExecutionStatus.SUCCESS

    def to_dict(self) -> Dict[str, Any]:
        """Convert to dictionary for serialization."""
        return {
            "execution_id": self.execution_id,
            "status": self.status.value,
            "stdout": self.stdout,
            "stderr": self.stderr,
            "output_files": [
                {
                    "filename": f.filename,
                    "content_base64": f.to_base64(),
                    "mime_type": f.mime_type,
                    "size_bytes": f.size_bytes,
                }
                for f in self.output_files
            ],
            "started_at": self.started_at.isoformat() if self.started_at else None,
            "completed_at": self.completed_at.isoformat() if self.completed_at else None,
            "duration_ms": self.duration_ms,
            "error_message": self.error_message,
            "exit_code": self.exit_code,
            "memory_used_bytes": self.memory_used_bytes,
        }


# =============================================================================
# Code Executor Service
# =============================================================================


# Matplotlib backend setup code to inject before user code
MATPLOTLIB_SETUP = '''
# Setup matplotlib for non-interactive backend (required in container)
import matplotlib
matplotlib.use('Agg')
'''


class CodeExecutorService:
    """
    Execute Python code in Docker sandbox containers.

    Features:
    - Docker container isolation
    - Resource limits (memory, CPU, timeout)
    - Network isolation
    - File I/O support
    - Automatic cleanup

    Usage:
        executor = CodeExecutorService()

        if executor.is_docker_available():
            result = await executor.execute(
                code="print('Hello, World!')",
                input_files=[InputFile.from_text("data.txt", "sample data")],
            )
            print(result.stdout)
    """

    def __init__(
        self,
        config: Optional[CodeExecutionConfig] = None,
    ):
        """
        Initialize the code executor service.

        Args:
            config: Execution configuration. Uses defaults if not provided.
        """
        self.config = config or CodeExecutionConfig()
        self._docker_client = None
        self._docker_available: Optional[bool] = None

    @property
    def docker_client(self):
        """
        Lazy-load Docker client.

        Returns:
            docker.DockerClient or None if Docker is not available.
        """
        if self._docker_client is None:
            try:
                import docker
                self._docker_client = docker.from_env()
                # Test connection
                self._docker_client.ping()
                logger.info("Docker client initialized successfully")
            except Exception as e:
                logger.warning(f"Failed to initialize Docker client: {e}")
                self._docker_client = None
        return self._docker_client

    def is_docker_available(self) -> bool:
        """
        Check if Docker is available and running.

        Returns:
            True if Docker is available, False otherwise.
        """
        if self._docker_available is not None:
            return self._docker_available

        try:
            client = self.docker_client
            if client is not None:
                client.ping()
                self._docker_available = True
                logger.info("Docker is available")
            else:
                self._docker_available = False
        except Exception as e:
            logger.warning(f"Docker is not available: {e}")
            self._docker_available = False

        return self._docker_available

    async def execute(
        self,
        code: str,
        input_files: Optional[List[InputFile]] = None,
        kb_documents: Optional[List[KBDocument]] = None,
        config: Optional[CodeExecutionConfig] = None,
    ) -> CodeExecutionResult:
        """
        Execute Python code in a Docker sandbox container.

        Args:
            code: Python code to execute.
            input_files: Optional list of input files to provide.
            kb_documents: Optional list of KB documents to provide.
            config: Optional execution config (overrides instance config).

        Returns:
            CodeExecutionResult with execution status and outputs.
        """
        execution_id = str(uuid.uuid4())
        exec_config = config or self.config
        input_files = input_files or []
        kb_documents = kb_documents or []

        result = CodeExecutionResult(
            execution_id=execution_id,
            status=ExecutionStatus.PENDING,
            started_at=datetime.utcnow(),
        )

        # Check Docker availability
        if not self.is_docker_available():
            result.status = ExecutionStatus.ERROR
            result.error_message = "Docker is not available"
            result.completed_at = datetime.utcnow()
            return result

        # Create temporary workspace
        workspace_dir = None
        container = None

        try:
            # Setup workspace
            workspace_dir = await self._setup_workspace(
                code=code,
                input_files=input_files,
                kb_documents=kb_documents,
                config=exec_config,
            )

            result.status = ExecutionStatus.RUNNING
            start_time = time.time()

            # Run container
            container, stdout, stderr, exit_code = await self._run_container(
                workspace_dir=workspace_dir,
                config=exec_config,
            )

            end_time = time.time()
            result.duration_ms = (end_time - start_time) * 1000

            # Collect output files
            output_files = await self._collect_output_files(
                workspace_dir=workspace_dir,
                config=exec_config,
            )

            # Set result
            result.stdout = stdout
            result.stderr = stderr
            result.exit_code = exit_code
            result.output_files = output_files

            if exit_code == 0:
                result.status = ExecutionStatus.SUCCESS
            else:
                result.status = ExecutionStatus.ERROR
                result.error_message = f"Process exited with code {exit_code}"

        except asyncio.TimeoutError:
            result.status = ExecutionStatus.TIMEOUT
            result.error_message = f"Execution timed out after {exec_config.timeout_seconds} seconds"
            logger.warning(f"Execution {execution_id} timed out")

        except Exception as e:
            result.status = ExecutionStatus.ERROR
            result.error_message = str(e)
            logger.error(f"Execution {execution_id} failed: {e}", exc_info=True)

        finally:
            # Cleanup
            result.completed_at = datetime.utcnow()

            if container:
                await self._cleanup_container(container)

            if workspace_dir:
                await self._cleanup_workspace(workspace_dir)

        return result

    async def _setup_workspace(
        self,
        code: str,
        input_files: List[InputFile],
        kb_documents: List[KBDocument],
        config: CodeExecutionConfig,
    ) -> Path:
        """
        Set up the workspace directory with code and input files.

        Workspace structure:
            /tmp/code_exec_XXXX/
                input/          - Input files
                output/         - Output files (created by code)
                kb_docs/        - Knowledge base documents
                main.py         - User code with matplotlib setup
        """
        # Create temp directory
        workspace_dir = Path(tempfile.mkdtemp(prefix="code_exec_"))

        # Create subdirectories
        input_dir = workspace_dir / "input"
        output_dir = workspace_dir / "output"
        kb_docs_dir = workspace_dir / "kb_docs"

        input_dir.mkdir(exist_ok=True)
        output_dir.mkdir(exist_ok=True)
        kb_docs_dir.mkdir(exist_ok=True)

        # Write main.py with matplotlib setup
        main_script = workspace_dir / "main.py"
        wrapped_code = MATPLOTLIB_SETUP + "\n" + code
        main_script.write_text(wrapped_code, encoding="utf-8")

        # Write input files
        for input_file in input_files:
            file_path = input_dir / input_file.filename
            file_path.write_bytes(input_file.content)

        # Write KB documents
        for kb_doc in kb_documents:
            doc_path = kb_docs_dir / kb_doc.filename
            doc_path.write_text(kb_doc.content, encoding="utf-8")

        logger.debug(
            f"Workspace created at {workspace_dir} with "
            f"{len(input_files)} input files, {len(kb_documents)} KB docs"
        )

        return workspace_dir

    async def _run_container(
        self,
        workspace_dir: Path,
        config: CodeExecutionConfig,
    ) -> tuple:
        """
        Run the Docker container and execute code.

        Returns:
            Tuple of (container, stdout, stderr, exit_code)
        """
        import docker
        from docker.errors import ContainerError, ImageNotFound, APIError

        client = self.docker_client

        # Pull image if not exists
        try:
            client.images.get(config.image)
        except ImageNotFound:
            logger.info(f"Pulling Docker image: {config.image}")
            client.images.pull(config.image)

        # Container configuration
        container_config = {
            "image": config.image,
            "command": ["python", config.main_script_path],
            "volumes": {
                str(workspace_dir): {
                    "bind": config.workspace_path,
                    "mode": "rw",
                }
            },
            "working_dir": config.workspace_path,
            "mem_limit": config.memory_limit,
            "nano_cpus": int(config.cpu_limit * 1e9),
            "network_disabled": config.network_disabled,
            "detach": True,
            "remove": False,  # We'll remove manually after getting logs
        }

        if config.read_only_root:
            container_config["read_only"] = True

        # Run container
        container = None
        stdout = ""
        stderr = ""
        exit_code = -1

        try:
            container = client.containers.run(**container_config)

            # Wait for container with timeout
            loop = asyncio.get_event_loop()
            wait_result = await asyncio.wait_for(
                loop.run_in_executor(
                    None,
                    lambda: container.wait(timeout=config.timeout_seconds)
                ),
                timeout=config.timeout_seconds + 5,  # Extra buffer
            )

            exit_code = wait_result.get("StatusCode", -1)

            # Get logs
            stdout = container.logs(stdout=True, stderr=False).decode("utf-8", errors="replace")
            stderr = container.logs(stdout=False, stderr=True).decode("utf-8", errors="replace")

        except asyncio.TimeoutError:
            # Kill container on timeout
            if container:
                try:
                    container.kill()
                except Exception:
                    pass
            raise

        except ContainerError as e:
            stderr = str(e)
            exit_code = e.exit_status

        except APIError as e:
            stderr = f"Docker API error: {e}"
            exit_code = -1

        return container, stdout, stderr, exit_code

    async def _collect_output_files(
        self,
        workspace_dir: Path,
        config: CodeExecutionConfig,
    ) -> List[OutputFile]:
        """
        Collect output files from the workspace.

        Returns:
            List of OutputFile objects.
        """
        output_files = []
        output_dir = workspace_dir / "output"

        if not output_dir.exists():
            return output_files

        for file_path in output_dir.iterdir():
            if file_path.is_file():
                try:
                    content = file_path.read_bytes()
                    mime_type = self._guess_mime_type(file_path.name)

                    output_files.append(OutputFile(
                        filename=file_path.name,
                        content=content,
                        mime_type=mime_type,
                        size_bytes=len(content),
                    ))

                except Exception as e:
                    logger.warning(f"Failed to read output file {file_path}: {e}")

        logger.debug(f"Collected {len(output_files)} output files")
        return output_files

    def _guess_mime_type(self, filename: str) -> str:
        """Guess MIME type from filename."""
        ext = filename.lower().rsplit(".", 1)[-1] if "." in filename else ""

        mime_types = {
            "png": "image/png",
            "jpg": "image/jpeg",
            "jpeg": "image/jpeg",
            "gif": "image/gif",
            "svg": "image/svg+xml",
            "pdf": "application/pdf",
            "csv": "text/csv",
            "json": "application/json",
            "txt": "text/plain",
            "html": "text/html",
            "xml": "application/xml",
        }

        return mime_types.get(ext, "application/octet-stream")

    async def _cleanup_container(self, container) -> None:
        """Remove the container."""
        try:
            container.remove(force=True)
            logger.debug(f"Container {container.id[:12]} removed")
        except Exception as e:
            logger.warning(f"Failed to remove container: {e}")

    async def _cleanup_workspace(self, workspace_dir: Path) -> None:
        """Remove the temporary workspace directory."""
        try:
            shutil.rmtree(workspace_dir)
            logger.debug(f"Workspace {workspace_dir} removed")
        except Exception as e:
            logger.warning(f"Failed to remove workspace {workspace_dir}: {e}")

    async def cleanup_all(self) -> None:
        """
        Cleanup any orphaned containers from previous executions.

        This can be called periodically to clean up containers that
        may have been left behind due to crashes.
        """
        if not self.is_docker_available():
            return

        try:
            client = self.docker_client
            # Find containers created by this service
            containers = client.containers.list(
                all=True,
                filters={"ancestor": self.config.image}
            )

            removed_count = 0
            for container in containers:
                try:
                    # Check if container is stopped and old
                    if container.status in ("exited", "dead"):
                        container.remove(force=True)
                        removed_count += 1
                except Exception as e:
                    logger.warning(f"Failed to remove orphaned container: {e}")

            if removed_count > 0:
                logger.info(f"Cleaned up {removed_count} orphaned containers")

        except Exception as e:
            logger.warning(f"Failed to cleanup orphaned containers: {e}")

    def close(self) -> None:
        """Close the Docker client connection."""
        if self._docker_client:
            try:
                self._docker_client.close()
                self._docker_client = None
                logger.info("Docker client closed")
            except Exception as e:
                logger.warning(f"Failed to close Docker client: {e}")


# =============================================================================
# Factory Function
# =============================================================================


_code_executor: Optional[CodeExecutorService] = None


def get_code_executor(config: Optional[CodeExecutionConfig] = None) -> CodeExecutorService:
    """
    Get the global CodeExecutorService instance.

    Args:
        config: Optional configuration (only used on first call).

    Returns:
        CodeExecutorService instance.
    """
    global _code_executor

    if _code_executor is None:
        _code_executor = CodeExecutorService(config=config)

    return _code_executor
