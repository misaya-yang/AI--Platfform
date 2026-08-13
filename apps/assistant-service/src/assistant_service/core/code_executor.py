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

import ast
import asyncio
import base64
import contextlib
import logging
import os
import re
import shutil
import stat
import tempfile
import time
import uuid
from dataclasses import dataclass, field, replace
from datetime import datetime
from enum import Enum
from pathlib import Path
from typing import Any

from ai_gateway_core.logging import get_logger, record_internal_exception

logger = get_logger(__name__)

# Host-owned result bounds. These are deliberately not caller/model configurable:
# sandbox resource limits are ineffective if the host then materializes unbounded
# logs or artifacts into the Assistant process.
_MAX_STREAM_BYTES = 2_000_000
_MAX_OUTPUT_FILE_BYTES = 8_000_000
_MAX_OUTPUT_TOTAL_BYTES = 24_000_000
_MAX_OUTPUT_FILES = 64
_STREAM_TRUNCATION_MARKER = b"\n...[stream truncated]"


def _validate_workspace_filename(filename: str) -> str:
    """Return a safe leaf filename for a sandbox workspace.

    Input and KB files are copied into fixed workspace subdirectories. Nested
    paths are not part of that contract, so reject them instead of silently
    normalizing names that could collide or escape the workspace.
    """

    if not isinstance(filename, str) or not filename:
        raise ValueError("workspace filename must be a non-empty string")
    if filename in {".", ".."}:
        raise ValueError("workspace filename must name a file")
    if Path(filename).is_absolute() or "/" in filename or "\\" in filename:
        raise ValueError("workspace filename must not contain a path")
    if any(ord(character) < 32 or ord(character) == 127 for character in filename):
        raise ValueError("workspace filename must not contain control characters")
    return filename


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
    read_only_root: bool = True

    # Sandbox runtime — "runsc" for gVisor isolation, None for default runc
    sandbox_runtime: str | None = "runsc"
    sandbox_backend: str = "docker"
    allow_default_runtime_fallback: bool = False

    # Docker image
    image: str = "python:3.12-slim"
    python_executable: str = "python"

    # Workspace paths (inside container)
    workspace_path: str = "/workspace"
    input_path: str = "/workspace/input"
    output_path: str = "/workspace/output"
    kb_docs_path: str = "/workspace/kb_docs"
    main_script_path: str = "/workspace/main.py"

    # Additional packages to install (pip install)
    packages: list[str] = field(
        default_factory=lambda: [
            "numpy",
            "pandas",
            "matplotlib",
        ]
    )


@dataclass
class InputFile:
    """Input file to be provided to the code execution."""

    filename: str
    content: bytes
    mime_type: str | None = None

    @classmethod
    def from_base64(cls, filename: str, data: str, mime_type: str | None = None) -> InputFile:
        """Create InputFile from base64 encoded data."""
        content = base64.b64decode(data)
        return cls(filename=filename, content=content, mime_type=mime_type)

    @classmethod
    def from_text(cls, filename: str, text: str, encoding: str = "utf-8") -> InputFile:
        """Create InputFile from text content."""
        content = text.encode(encoding)
        return cls(filename=filename, content=content, mime_type="text/plain")


@dataclass
class OutputFile:
    """Output file produced by code execution."""

    filename: str
    content: bytes
    mime_type: str | None = None
    size_bytes: int = 0
    captured_size_bytes: int | None = None
    content_truncated: bool = False

    def __post_init__(self) -> None:
        if self.captured_size_bytes is None:
            self.captured_size_bytes = len(self.content)

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
    document_id: str | None = None
    metadata: dict[str, Any] = field(default_factory=dict)


@dataclass
class CodeExecutionResult:
    """Result of code execution."""

    execution_id: str
    status: ExecutionStatus

    # Output
    stdout: str = ""
    stderr: str = ""
    output_files: list[OutputFile] = field(default_factory=list)

    # Timing
    started_at: datetime | None = None
    completed_at: datetime | None = None
    duration_ms: float = 0.0

    # Error info
    error_message: str | None = None
    exit_code: int | None = None

    # Resource usage
    memory_used_bytes: int | None = None
    truncation_receipts: list[dict[str, Any]] = field(default_factory=list)

    def is_success(self) -> bool:
        """Check if execution was successful."""
        return self.status == ExecutionStatus.SUCCESS

    def to_dict(self) -> dict[str, Any]:
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
                    "captured_size_bytes": f.captured_size_bytes,
                    "content_truncated": f.content_truncated,
                }
                for f in self.output_files
            ],
            "started_at": self.started_at.isoformat() if self.started_at else None,
            "completed_at": self.completed_at.isoformat() if self.completed_at else None,
            "duration_ms": self.duration_ms,
            "error_message": self.error_message,
            "exit_code": self.exit_code,
            "memory_used_bytes": self.memory_used_bytes,
            "truncation_receipts": list(self.truncation_receipts),
        }


# =============================================================================
# Code Executor Service
# =============================================================================


# Matplotlib backend setup code to inject before user code
MATPLOTLIB_SETUP = """
# Setup matplotlib for non-interactive backend (required in container).
# Also transparently redirect relative savefig paths into ./output/ so that
# the host-side collector (which scans only the output/ subdirectory) can
# pick up plots saved with simple `plt.savefig('chart.png')` calls. Absolute
# paths and paths already under output/ are untouched. Same shim is applied
# to Figure.savefig (used by object-oriented matplotlib code).
import os as _os
import matplotlib
matplotlib.use('Agg')

def _redirect_relative_to_output(fname):
    # Accept pathlib.Path / os.PathLike transparently. BytesIO and other
    # non-path-like objects pass through unchanged so matplotlib can
    # handle them directly.
    if hasattr(fname, '__fspath__'):
        try:
            fname = _os.fspath(fname)
        except TypeError:
            return fname
    if not isinstance(fname, str):
        return fname
    if _os.path.isabs(fname):
        return fname
    # Normalize leading `./` so paths like `./chart.png` produce
    # `output/chart.png` instead of `output/./chart.png`.
    _cleaned = fname[2:] if fname.startswith('./') else fname
    if _cleaned.startswith('output/'):
        return _cleaned
    _os.makedirs('output', exist_ok=True)
    return _os.path.join('output', _cleaned)

import matplotlib.pyplot as _plt
_orig_plt_savefig = _plt.savefig
def _patched_plt_savefig(fname, *a, **kw):
    return _orig_plt_savefig(_redirect_relative_to_output(fname), *a, **kw)
_plt.savefig = _patched_plt_savefig

from matplotlib.figure import Figure as _Figure
_orig_fig_savefig = _Figure.savefig
def _patched_fig_savefig(self, fname, *a, **kw):
    return _orig_fig_savefig(self, _redirect_relative_to_output(fname), *a, **kw)
_Figure.savefig = _patched_fig_savefig
"""


def _uses_matplotlib(code: str) -> bool:
    """Return whether the submitted program imports matplotlib."""
    try:
        tree = ast.parse(code)
    except SyntaxError:
        return False
    for node in ast.walk(tree):
        if isinstance(node, ast.Import) and any(
            alias.name == "matplotlib" or alias.name.startswith("matplotlib.")
            for alias in node.names
        ):
            return True
        if isinstance(node, ast.ImportFrom) and (
            node.module == "matplotlib" or str(node.module or "").startswith("matplotlib.")
        ):
            return True
    return False


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
        config: CodeExecutionConfig | None = None,
        *,
        allow_runc_code_executor: bool | None = None,
        startup_config: Any | None = None,
    ):
        """
        Initialize the code executor service.

        Args:
            config: Execution configuration. Uses defaults if not provided.
        """
        self.config = config or CodeExecutionConfig()
        if startup_config is not None:
            self.config.sandbox_backend = str(
                startup_config.runtime_value("ASSISTANT_CODE_EXECUTOR_BACKEND")
            )
            self.config.sandbox_runtime = startup_config.runtime_value("SANDBOX_RUNTIME")
            self.config.image = str(
                startup_config.runtime_value("ASSISTANT_CODE_EXECUTOR_IMAGE")
            )
            self.config.python_executable = str(
                startup_config.runtime_value("ASSISTANT_CODE_EXECUTOR_PYTHON")
            )
        else:
            backend = os.environ.get("ASSISTANT_CODE_EXECUTOR_BACKEND", "").strip().lower()
            if backend:
                if backend not in {"docker", "sbx"}:
                    raise ValueError("ASSISTANT_CODE_EXECUTOR_BACKEND must be docker or sbx")
                self.config.sandbox_backend = backend
            # Allow env var override: SANDBOX_RUNTIME=runsc|runc|""
            env_runtime = os.environ.get("SANDBOX_RUNTIME")
            if env_runtime is not None:
                self.config.sandbox_runtime = env_runtime or None
                self.config.allow_default_runtime_fallback = env_runtime == ""
        if self.config.sandbox_backend == "sbx":
            # Docker Sandboxes exposes a Docker-compatible endpoint backed by
            # one nerdbox microVM per child container. It is a backend, not a
            # Docker Engine runtime name such as runsc.
            self.config.sandbox_runtime = None
            self.config.allow_default_runtime_fallback = False
        resolved_allow_runc = (
            os.environ.get("ASSISTANT_ALLOW_RUNC_CODE_EXECUTOR", "").lower()
            in {"1", "true", "yes", "on"}
            if allow_runc_code_executor is None
            else bool(allow_runc_code_executor)
        )
        if resolved_allow_runc:
            self.config.allow_default_runtime_fallback = True
        if startup_config is None:
            image = os.environ.get("ASSISTANT_CODE_EXECUTOR_IMAGE", "").strip()
            if image:
                self.config.image = image
            python_executable = os.environ.get(
                "ASSISTANT_CODE_EXECUTOR_PYTHON",
                "",
            ).strip()
            if python_executable:
                if not re.fullmatch(r"[A-Za-z0-9_./-]+", python_executable):
                    raise ValueError("ASSISTANT_CODE_EXECUTOR_PYTHON is invalid")
                self.config.python_executable = python_executable
        self._sbx_api_version = (
            str(startup_config.runtime_value("ASSISTANT_SBX_DOCKER_API_VERSION"))
            if startup_config is not None
            else os.environ.get("ASSISTANT_SBX_DOCKER_API_VERSION", "1.51").strip()
        )
        if not re.fullmatch(r"\d+\.\d+", self._sbx_api_version):
            raise ValueError("ASSISTANT_SBX_DOCKER_API_VERSION is invalid")
        self._sandbox_workspace = (
            str(startup_config.runtime_value("SANDBOX_WORKSPACE"))
            if startup_config is not None
            else os.environ.get("SANDBOX_WORKSPACE", "/opt/deploy/sandbox-workspace")
        )
        self._sandbox_workspace_host = (
            str(startup_config.runtime_value("SANDBOX_WORKSPACE_HOST"))
            if startup_config is not None
            else os.environ.get("SANDBOX_WORKSPACE_HOST", "").strip()
        )
        self._docker_environment = None
        if startup_config is not None:
            self._docker_environment = {
                "DOCKER_HOST": str(startup_config.runtime_value("DOCKER_HOST")),
                "DOCKER_TLS_VERIFY": (
                    "1" if startup_config.runtime_value("DOCKER_TLS_VERIFY") else ""
                ),
                "DOCKER_CERT_PATH": str(
                    startup_config.runtime_value("DOCKER_CERT_PATH")
                ),
            }
        self._docker_client = None
        self._docker_available: bool | None = None

    def _execution_config(
        self,
        override: CodeExecutionConfig | None,
    ) -> CodeExecutionConfig:
        """Apply per-call limits without changing the attested sandbox boundary."""

        if override is None:
            return self.config
        return replace(
            override,
            network_disabled=self.config.network_disabled,
            read_only_root=self.config.read_only_root,
            sandbox_runtime=self.config.sandbox_runtime,
            sandbox_backend=self.config.sandbox_backend,
            allow_default_runtime_fallback=self.config.allow_default_runtime_fallback,
        )

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

                client_options = (
                    {"version": self._sbx_api_version}
                    if self.config.sandbox_backend == "sbx"
                    else {}
                )
                if self._docker_environment is not None:
                    client_options["environment"] = self._docker_environment
                self._docker_client = docker.from_env(**client_options)
                # Test connection
                self._docker_client.ping()
                logger.info("Docker client initialized successfully")
            except Exception as e:
                record_internal_exception(
                    __name__, "assistant.core.code_executor.internal_failure", e
                )
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
                info = client.info()
                if self.config.sandbox_backend == "sbx":
                    if info.get("DefaultRuntime") != "nerdbox":
                        logger.error("Configured sbx backend is not backed by the nerdbox runtime")
                        self._docker_available = False
                        return self._docker_available
                    logger.info("Docker Sandboxes microVM backend is available")
                    return self._docker_available
                # Check if configured sandbox runtime is available
                rt = self.config.sandbox_runtime
                if rt:
                    runtimes = info.get("Runtimes") or {}
                    if rt in runtimes:
                        logger.info(f"Docker available with sandbox runtime '{rt}' (gVisor)")
                    else:
                        logger.warning(
                            f"Sandbox runtime '{rt}' not found. Available: {list(runtimes.keys())}"
                        )
                        if self.config.allow_default_runtime_fallback:
                            logger.warning(
                                "Explicitly falling back to default Docker runtime for code executor."
                            )
                            self.config.sandbox_runtime = None
                        else:
                            self._docker_available = False
                else:
                    logger.info("Docker available (sandbox runtime: default)")
            else:
                self._docker_available = False
        except Exception as e:
            record_internal_exception(
                __name__, "assistant.core.code_executor.internal_failure", e
            )
            self._docker_available = False

        return self._docker_available

    async def execute(
        self,
        code: str,
        input_files: list[InputFile] | None = None,
        kb_documents: list[KBDocument] | None = None,
        config: CodeExecutionConfig | None = None,
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
        exec_config = self._execution_config(config)
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

            # Log exec outcome before collect so we can correlate 0-file
            # returns with stderr (e.g. permission denied on savefig) or
            # silent stdout (matplotlib saved but to wrong path).
            logger.info(
                "[code_executor] exec_id=%s exit=%s stdout_len=%d stderr_len=%d",
                execution_id,
                exit_code,
                len(stdout),
                len(stderr),
            )
            if stderr.strip():
                logger.warning(
                    "[code_executor] exec_id=%s stderr_tail=%r",
                    execution_id,
                    stderr[-500:],
                )

            # Collect output files
            truncation_receipts: list[dict[str, Any]] = []
            for stream_name, stream_value in (("stdout", stdout), ("stderr", stderr)):
                if stream_value.endswith(_STREAM_TRUNCATION_MARKER.decode()):
                    truncation_receipts.append(
                        {
                            "kind": "container_log",
                            "stream": stream_name,
                            "reason": "byte_limit",
                            "truncated": True,
                            "limit_bytes": _MAX_STREAM_BYTES,
                        }
                    )
            output_files = await self._collect_output_files(
                workspace_dir=workspace_dir,
                config=exec_config,
                truncation_receipts=truncation_receipts,
            )
            if truncation_receipts:
                reason_counts: dict[str, int] = {}
                for receipt in truncation_receipts:
                    reason = str(receipt.get("reason") or "unspecified")
                    reason_counts[reason] = reason_counts.get(reason, 0) + 1
                summary = ", ".join(
                    f"{reason}={count}" for reason, count in sorted(reason_counts.items())
                )
                separator = "" if not stderr or stderr.endswith("\n") else "\n"
                stderr += (
                    f"{separator}[assistant] result capture truncated by host safety limits "
                    f"({summary}); see truncation_receipts for structured details."
                )

            # Set result
            result.stdout = stdout
            result.stderr = stderr
            result.exit_code = exit_code
            result.output_files = output_files
            result.truncation_receipts = truncation_receipts

            if exit_code == 0:
                result.status = ExecutionStatus.SUCCESS
            else:
                result.status = ExecutionStatus.ERROR
                result.error_message = f"Process exited with code {exit_code}"

        except asyncio.TimeoutError:
            result.status = ExecutionStatus.TIMEOUT
            result.error_message = (
                f"Execution timed out after {exec_config.timeout_seconds} seconds"
            )
            logger.warning(f"Execution {execution_id} timed out")

        except Exception as e:
            record_internal_exception(
                __name__, "assistant.core.code_executor.internal_failure", e
            )
            result.status = ExecutionStatus.ERROR
            result.error_message = str(e)

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
        input_files: list[InputFile],
        kb_documents: list[KBDocument],
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
        _ = config
        input_names = [
            _validate_workspace_filename(input_file.filename) for input_file in input_files
        ]
        document_names = [_validate_workspace_filename(kb_doc.filename) for kb_doc in kb_documents]

        # Create temp directory in shared workspace (accessible by both
        # the gateway container and sibling sandbox containers via Docker socket).
        # Without this, Docker-in-Docker volume mounts fail because the host
        # daemon can't see files inside the gateway container's /tmp.
        shared_base = self._sandbox_workspace
        os.makedirs(shared_base, exist_ok=True)
        workspace_dir = Path(tempfile.mkdtemp(prefix="code_exec_", dir=shared_base))

        # Create subdirectories
        input_dir = workspace_dir / "input"
        output_dir = workspace_dir / "output"
        kb_docs_dir = workspace_dir / "kb_docs"

        input_dir.mkdir(exist_ok=True)
        output_dir.mkdir(exist_ok=True)
        kb_docs_dir.mkdir(exist_ok=True)

        # Inject the plotting shim only when the submitted program actually
        # imports matplotlib. The minimal sandbox image intentionally does not
        # force plotting dependencies onto ordinary Python tasks.
        main_script = workspace_dir / "main.py"
        wrapped_code = (MATPLOTLIB_SETUP + "\n" if _uses_matplotlib(code) else "") + code
        main_script.write_text(wrapped_code, encoding="utf-8")

        # Write input files
        for input_file, filename in zip(input_files, input_names, strict=True):
            file_path = input_dir / filename
            file_path.write_bytes(input_file.content)

        # Write KB documents
        for kb_doc, filename in zip(kb_documents, document_names, strict=True):
            doc_path = kb_docs_dir / filename
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
        from docker.errors import APIError, ContainerError, ImageNotFound
        from docker.types import Mount
        from requests.exceptions import Timeout as RequestsTimeout

        client = self.docker_client

        # Pull image if not exists
        try:
            client.images.get(config.image)
        except ImageNotFound:
            logger.info(f"Pulling Docker image: {config.image}")
            client.images.pull(config.image)

        workspace_mount_source = self._workspace_mount_source(
            workspace_dir,
            container_root_value=self._sandbox_workspace,
            host_root_value=self._sandbox_workspace_host,
        )
        stdout_capture = workspace_dir / ".assistant-stdout"
        stderr_capture = workspace_dir / ".assistant-stderr"

        # Container configuration. The child receives no Docker socket or
        # provider credentials; it runs without network, Linux capabilities,
        # privilege escalation, or a writable root filesystem.
        container_config = {
            "image": config.image,
            "command": [config.python_executable, config.main_script_path],
            "mounts": [
                Mount(
                    target=config.workspace_path,
                    source=str(workspace_mount_source),
                    type="bind",
                )
            ],
            "working_dir": config.workspace_path,
            "user": f"{os.getuid()}:{os.getgid()}",
            "mem_limit": config.memory_limit,
            "nano_cpus": int(config.cpu_limit * 1e9),
            "cap_drop": ["ALL"],
            "security_opt": ["no-new-privileges:true"],
            "pids_limit": 64,
            "read_only": config.read_only_root,
            "tmpfs": {"/tmp": "rw,noexec,nosuid,size=64m"},
            "environment": {
                "HOME": "/tmp",
                "MPLCONFIGDIR": "/tmp/matplotlib",
                "PYTHONDONTWRITEBYTECODE": "1",
            },
            "labels": {"com.misaya.ai-gateway.role": "code-sandbox"},
            "detach": True,
            "remove": False,  # We'll remove manually after getting logs
        }

        if config.network_disabled:
            if config.sandbox_backend == "sbx":
                # Docker Sandboxes implements NetworkMode=none but rejects
                # Docker Engine's legacy Config.NetworkDisabled flag.
                container_config["network_mode"] = "none"
            else:
                container_config["network_disabled"] = True
        if config.sandbox_backend != "sbx":
            # The sbx Docker-compatible API rejects HostConfig.Init. Its
            # nerdbox VM already owns the child process lifecycle.
            container_config["init"] = True
        else:
            # sbx 0.38 implements container wait but not Docker's logs API.
            # Redirect through fixed positional shell arguments and read the
            # bind-mounted captures without following symlinks.
            container_config["command"] = [
                "/bin/sh",
                "-c",
                'exec "$1" "$2" >"$3" 2>"$4"',
                "assistant-code",
                config.python_executable,
                config.main_script_path,
                f"{config.workspace_path}/{stdout_capture.name}",
                f"{config.workspace_path}/{stderr_capture.name}",
            ]

        # ADR-002 Phase 2: gVisor sandbox runtime for kernel-level isolation
        if config.sandbox_runtime:
            container_config["runtime"] = config.sandbox_runtime
            logger.debug(f"Using sandbox runtime: {config.sandbox_runtime}")

        if config.read_only_root:
            container_config["read_only"] = True

        # Run container
        container = None
        stdout = ""
        stderr = ""
        exit_code = -1
        handoff_container = False

        try:
            container = client.containers.run(**container_config)

            # Wait for container with timeout
            loop = asyncio.get_running_loop()
            wait_result = await asyncio.wait_for(
                loop.run_in_executor(None, lambda: container.wait(timeout=config.timeout_seconds)),
                timeout=config.timeout_seconds + 5,  # Extra buffer
            )

            exit_code = wait_result.get("StatusCode", -1)

            if config.sandbox_backend == "sbx":
                stdout = self._read_capture(stdout_capture)
                stderr = self._read_capture(stderr_capture)
            else:
                stdout = self._read_container_logs(
                    container,
                    stdout=True,
                    stderr=False,
                )
                stderr = self._read_container_logs(
                    container,
                    stdout=False,
                    stderr=True,
                )
            handoff_container = True

        except (asyncio.TimeoutError, RequestsTimeout) as exc:
            # docker-py raises requests.ReadTimeout from its worker thread;
            # normalize it so execute() reports TIMEOUT rather than ERROR.
            raise asyncio.TimeoutError from exc

        except ContainerError as e:
            stderr = str(e)
            exit_code = e.exit_status
            handoff_container = container is not None

        except APIError as e:
            stderr = f"Docker API error: {e}"
            exit_code = -1
            handoff_container = container is not None

        finally:
            for capture in (stdout_capture, stderr_capture):
                with contextlib.suppress(OSError):
                    capture.unlink()
            # Tuple assignment in execute() cannot receive ``container`` when
            # this method raises. Own cleanup here until responsibility has
            # explicitly transferred to the caller.
            if container is not None and not handoff_container:
                await self._discard_container(container, kill=True)

        return container, stdout, stderr, exit_code

    @staticmethod
    def _read_container_logs(
        container: Any,
        *,
        stdout: bool,
        stderr: bool,
        max_bytes: int | None = None,
    ) -> str:
        """Stream a completed container's logs into a bounded host buffer."""

        limit = _MAX_STREAM_BYTES if max_bytes is None else max(0, max_bytes)
        chunks = container.logs(
            stdout=stdout,
            stderr=stderr,
            stream=True,
            follow=False,
        )
        if isinstance(chunks, (bytes, bytearray, memoryview, str)):
            chunks = (chunks,)
        payload = bytearray()
        truncated = False
        try:
            for chunk in chunks:
                if isinstance(chunk, str):
                    chunk = chunk.encode()
                elif not isinstance(chunk, (bytes, bytearray, memoryview)):
                    chunk = str(chunk).encode()
                remaining = limit - len(payload)
                if remaining <= 0:
                    truncated = True
                    break
                payload.extend(bytes(chunk)[:remaining])
                if len(chunk) > remaining:
                    truncated = True
                    break
        finally:
            close = getattr(chunks, "close", None)
            if callable(close):
                try:
                    close()
                except Exception as exc:
                    record_internal_exception(
                        __name__,
                        "assistant.core.code_executor.suppressed_failure",
                        exc,
                        level=logging.DEBUG,
                    )
        if truncated:
            payload.extend(_STREAM_TRUNCATION_MARKER)
        return bytes(payload).decode("utf-8", errors="replace")

    @staticmethod
    def _read_capture(path: Path, *, max_bytes: int | None = None) -> str:
        """Read a sandbox stream capture without following a forged link."""

        limit = _MAX_STREAM_BYTES if max_bytes is None else max(0, max_bytes)
        flags = os.O_RDONLY | getattr(os, "O_NOFOLLOW", 0)
        descriptor = os.open(path, flags)
        try:
            if not stat.S_ISREG(os.fstat(descriptor).st_mode):
                raise ValueError("sandbox stream capture is not a regular file")
            payload = os.read(descriptor, limit + 1)
        finally:
            os.close(descriptor)
        if len(payload) > limit:
            payload = payload[:limit] + _STREAM_TRUNCATION_MARKER
        return payload.decode("utf-8", errors="replace")

    @staticmethod
    def _workspace_mount_source(
        workspace_dir: Path,
        *,
        container_root_value: str | None = None,
        host_root_value: str | None = None,
    ) -> Path:
        """Translate the in-container workspace to the Docker host path.

        A host Docker daemon cannot resolve a path that only exists inside the
        Assistant container. Local opt-in deployments bind one host directory
        into the Assistant and provide its host-side path separately.
        """

        container_root = Path(
            container_root_value
            or os.environ.get("SANDBOX_WORKSPACE", "/opt/deploy/sandbox-workspace")
        ).resolve()
        resolved_workspace = workspace_dir.resolve()
        try:
            relative_workspace = resolved_workspace.relative_to(container_root)
        except ValueError as exc:
            raise ValueError("sandbox workspace escaped its configured root") from exc

        host_root_value = (
            host_root_value
            if host_root_value is not None
            else os.environ.get("SANDBOX_WORKSPACE_HOST", "").strip()
        )
        if not host_root_value:
            return resolved_workspace
        host_root = Path(host_root_value)
        if not host_root.is_absolute():
            raise ValueError("SANDBOX_WORKSPACE_HOST must be absolute")
        return host_root / relative_workspace

    # Extensions we consider user-facing artifacts when recovering stray
    # files written at the workspace root (outside /output). Kept
    # conservative so we don't slurp up .pyc caches, .lock files, etc.
    _RECOVERABLE_EXTS: frozenset[str] = frozenset(
        {
            ".png",
            ".jpg",
            ".jpeg",
            ".gif",
            ".svg",
            ".webp",
            ".bmp",
            ".pdf",
            ".csv",
            ".tsv",
            ".xlsx",
            ".xls",
            ".json",
            ".html",
            ".htm",
            ".xml",
            ".txt",
            ".md",
        }
    )

    # Workspace-root directories we never sweep — they either belong to
    # the caller (input, kb_docs) or are our own scaffolding.
    _RESERVED_ROOT_NAMES: frozenset[str] = frozenset(
        {
            "input",
            "kb_docs",
            "output",
        }
    )

    def _recover_root_artifacts(self, workspace_dir: Path) -> list[Path]:
        """Move artifact-like files written at the workspace root (or in
        other non-reserved directories) into ``output/`` so the normal
        collector picks them up.

        This is the safety net for code paths our matplotlib shim does
        not cover — e.g. ``PIL.Image.save('x.png')``, ``pd.to_csv(
        'data.csv')``, ``imageio.imwrite(...)``, or any code that writes
        with a bare filename. Returns the list of recovered paths (in
        their new ``output/`` location) for diagnostic logging.
        """
        output_dir = workspace_dir / "output"
        output_dir.mkdir(exist_ok=True)

        recovered: list[Path] = []
        try:
            root_entries = []
            for index, entry in enumerate(workspace_dir.iterdir()):
                if index >= _MAX_OUTPUT_FILES:
                    logger.warning(
                        "[code_executor] workspace artifact scan capped at %d entries",
                        _MAX_OUTPUT_FILES,
                    )
                    break
                root_entries.append(entry)
        except Exception as e:
            record_internal_exception(
                __name__, "assistant.core.code_executor.internal_failure", e
            )
            return recovered

        for entry in root_entries:
            # Skip reserved dirs and our scaffolding (main.py).
            if entry.name in self._RESERVED_ROOT_NAMES:
                continue
            if entry.name == "main.py":
                continue
            if entry.is_symlink():
                logger.warning("[code_executor] rejected symlink artifact: %s", entry.name)
                continue
            if entry.is_dir():
                # Don't descend into unknown subdirectories — user code
                # shouldn't be creating them at root, and we don't want
                # to recursively slurp arbitrary structures.
                continue
            if not entry.is_file():
                continue
            if entry.suffix.lower() not in self._RECOVERABLE_EXTS:
                continue

            # Avoid clobbering if a file of the same name already lives
            # in output/ (e.g. model wrote both via savefig shim and a
            # bare PIL call). Suffix with a counter.
            target = output_dir / entry.name
            if target.exists():
                stem, ext = entry.stem, entry.suffix
                i = 1
                while True:
                    alt = output_dir / f"{stem}_{i}{ext}"
                    if not alt.exists():
                        target = alt
                        break
                    i += 1

            try:
                shutil.move(str(entry), str(target))
                recovered.append(target)
            except Exception as e:
                record_internal_exception(
                    __name__, "assistant.core.code_executor.internal_failure", e
                )

        return recovered

    async def _collect_output_files(
        self,
        workspace_dir: Path,
        config: CodeExecutionConfig,
        *,
        truncation_receipts: list[dict[str, Any]] | None = None,
    ) -> list[OutputFile]:
        """
        Collect output files from the workspace.

        Returns:
            List of OutputFile objects.
        """
        _ = config
        output_files = []
        output_dir = workspace_dir / "output"
        receipts = truncation_receipts if truncation_receipts is not None else []

        # Fix A: sweep stray artifacts from the workspace root into
        # output/ before the normal collection pass. Catches code paths
        # our matplotlib shim doesn't cover (PIL, pandas, imageio, ...).
        recovered = self._recover_root_artifacts(workspace_dir)
        if recovered:
            logger.info(
                "[code_executor] recovered %d stray artifact(s) from "
                "workspace root into output/: %s",
                len(recovered),
                ", ".join(p.name for p in recovered[:20]),
            )

        if not output_dir.exists():
            logger.warning(
                "[code_executor] output_dir MISSING post-run: %s "
                "(workspace_dir exists=%s). Container couldn't write back.",
                output_dir,
                workspace_dir.exists(),
            )
            return output_files

        # Do not materialize an attacker-controlled directory without a count
        # bound. One extra entry is enough to produce an explicit receipt.
        all_entries: list[Path] = []
        entries_truncated = False
        for index, entry in enumerate(output_dir.iterdir()):
            if index >= _MAX_OUTPUT_FILES:
                entries_truncated = True
                break
            all_entries.append(entry)
        all_entries.sort(key=lambda path: path.name)
        if entries_truncated:
            receipts.append(
                {
                    "kind": "output_files",
                    "reason": "file_count_limit",
                    "truncated": True,
                    "limit_files": _MAX_OUTPUT_FILES,
                }
            )
        logger.info(
            "[code_executor] output_dir=%s entries=%s%d [%s]",
            output_dir,
            ">=" if entries_truncated else "",
            len(all_entries),
            ", ".join(p.name for p in all_entries[:20]) or "<empty>",
        )

        # Fix C: when output/ is empty, dump a bounded workspace listing
        # (minus reserved dirs) so next time we can tell whether the
        # model wrote files elsewhere, wrote nothing, or the container
        # couldn't write back. This is defense-in-depth after the
        # recovery sweep above.
        if not all_entries:
            try:
                root_listing = []
                for index, p in enumerate(workspace_dir.iterdir()):
                    if index >= 40:
                        root_listing.append("...[listing truncated]")
                        break
                    if p.name in self._RESERVED_ROOT_NAMES:
                        continue
                    kind = "l" if p.is_symlink() else "d" if p.is_dir() else "f"
                    try:
                        size = p.stat().st_size if p.is_file() else -1
                    except Exception as exc:
                        record_internal_exception(
                            __name__, "assistant.core.code_executor.internal_failure", exc
                        )
                        size = -1
                    root_listing.append(f"{kind}:{p.name}:{size}")
                logger.warning(
                    "[code_executor] output/ empty — workspace root contents (ex reserved): [%s]",
                    ", ".join(root_listing[:40]) or "<empty>",
                )
            except Exception as e:
                record_internal_exception(
                    __name__, "assistant.core.code_executor.internal_failure", e
                )

        captured_total = 0
        aggregate_receipt_recorded = False
        for file_path in all_entries:
            if file_path.is_symlink():
                logger.warning(
                    "[code_executor] rejected symlink output: %s",
                    file_path.name,
                )
                continue
            if file_path.is_file():
                try:
                    original_size = file_path.stat().st_size
                    remaining = max(0, _MAX_OUTPUT_TOTAL_BYTES - captured_total)
                    capture_limit = min(_MAX_OUTPUT_FILE_BYTES, remaining)
                    with file_path.open("rb") as output_handle:
                        content = output_handle.read(capture_limit + 1)
                    content_truncated = (
                        len(content) > capture_limit or original_size > capture_limit
                    )
                    content = content[:capture_limit]
                    captured_total += len(content)
                    mime_type = self._guess_mime_type(file_path.name)

                    output_files.append(
                        OutputFile(
                            filename=file_path.name,
                            content=content,
                            mime_type=mime_type,
                            size_bytes=original_size,
                            captured_size_bytes=len(content),
                            content_truncated=content_truncated,
                        )
                    )
                    if original_size > _MAX_OUTPUT_FILE_BYTES:
                        receipts.append(
                            {
                                "kind": "output_file",
                                "filename": file_path.name,
                                "reason": "per_file_byte_limit",
                                "truncated": True,
                                "original_size_bytes": original_size,
                                "captured_size_bytes": len(content),
                                "limit_bytes": _MAX_OUTPUT_FILE_BYTES,
                            }
                        )
                    if original_size > remaining and not aggregate_receipt_recorded:
                        receipts.append(
                            {
                                "kind": "output_files",
                                "reason": "aggregate_byte_limit",
                                "truncated": True,
                                "limit_bytes": _MAX_OUTPUT_TOTAL_BYTES,
                            }
                        )
                        aggregate_receipt_recorded = True

                except Exception as e:
                    record_internal_exception(
                        __name__, "assistant.core.code_executor.internal_failure", e
                    )

        logger.info("[code_executor] collected %d output file(s)", len(output_files))
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
        await self._discard_container(container, kill=False)

    @staticmethod
    async def _discard_container(container: Any, *, kill: bool) -> None:
        """Best-effort kill/remove without blocking the event loop."""

        if kill:
            try:
                await asyncio.to_thread(container.kill)
            except Exception as exc:
                record_internal_exception(
                    __name__,
                    "assistant.core.code_executor.suppressed_failure",
                    exc,
                    level=logging.DEBUG,
                )
        try:
            await asyncio.to_thread(container.remove, force=True)
            logger.debug(f"Container {container.id[:12]} removed")
        except Exception as e:
            record_internal_exception(
                __name__, "assistant.core.code_executor.internal_failure", e
            )

    async def _cleanup_workspace(self, workspace_dir: Path) -> None:
        """Remove the temporary workspace directory."""
        try:
            shutil.rmtree(workspace_dir)
            logger.debug(f"Workspace {workspace_dir} removed")
        except Exception as e:
            record_internal_exception(
                __name__, "assistant.core.code_executor.internal_failure", e
            )

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
            containers = client.containers.list(all=True, filters={"ancestor": self.config.image})

            removed_count = 0
            for container in containers:
                try:
                    # Check if container is stopped and old
                    if container.status in ("exited", "dead"):
                        container.remove(force=True)
                        removed_count += 1
                except Exception as e:
                    record_internal_exception(
                        __name__, "assistant.core.code_executor.internal_failure", e
                    )

            if removed_count > 0:
                logger.info(f"Cleaned up {removed_count} orphaned containers")

        except Exception as e:
            record_internal_exception(
                __name__, "assistant.core.code_executor.internal_failure", e
            )

    def close(self) -> None:
        """Close the Docker client connection."""
        if self._docker_client:
            try:
                self._docker_client.close()
                self._docker_client = None
                logger.info("Docker client closed")
            except Exception as e:
                record_internal_exception(
                    __name__, "assistant.core.code_executor.internal_failure", e
                )


# =============================================================================
# Factory Function
# =============================================================================


_code_executor: CodeExecutorService | None = None


def get_code_executor(
    config: CodeExecutionConfig | None = None,
    *,
    allow_runc_code_executor: bool | None = None,
    startup_config: Any | None = None,
) -> CodeExecutorService:
    """
    Get the global CodeExecutorService instance.

    Args:
        config: Optional configuration (only used on first call).

    Returns:
        CodeExecutorService instance.
    """
    global _code_executor

    if _code_executor is None:
        _code_executor = CodeExecutorService(
            config=config,
            allow_runc_code_executor=allow_runc_code_executor,
            startup_config=startup_config,
        )

    return _code_executor
