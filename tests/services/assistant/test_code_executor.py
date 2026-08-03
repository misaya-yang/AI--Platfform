"""
Code Executor Service Tests

Tests for:
- Data classes (ExecutionStatus, CodeExecutionConfig, InputFile, OutputFile, etc.)
- CodeExecutorService initialization and Docker availability check
- Workspace setup and cleanup
- Code execution (with Docker mocking for unit tests)
"""

import asyncio
import base64
import tempfile
from datetime import datetime
from pathlib import Path
from unittest.mock import MagicMock, PropertyMock, patch

import pytest
from assistant_service.core.code_executor import (
    MATPLOTLIB_SETUP,
    CodeExecutionConfig,
    CodeExecutionResult,
    CodeExecutorService,
    ExecutionStatus,
    InputFile,
    KBDocument,
    OutputFile,
    get_code_executor,
)


@pytest.fixture(autouse=True)
def _sandbox_workspace(monkeypatch, tmp_path):
    monkeypatch.setenv("SANDBOX_WORKSPACE", str(tmp_path / "sandbox-workspace"))


# =============================================================================
# Data Class Tests
# =============================================================================


class TestExecutionStatus:
    """Test ExecutionStatus enum."""

    def test_status_values(self):
        """Test that ExecutionStatus has expected values."""
        assert ExecutionStatus.PENDING == "pending"
        assert ExecutionStatus.RUNNING == "running"
        assert ExecutionStatus.SUCCESS == "success"
        assert ExecutionStatus.ERROR == "error"
        assert ExecutionStatus.TIMEOUT == "timeout"
        assert ExecutionStatus.CANCELLED == "cancelled"


class TestCodeExecutionConfig:
    """Test CodeExecutionConfig dataclass."""

    def test_default_config(self):
        """Test default configuration values."""
        config = CodeExecutionConfig()

        assert config.memory_limit == "512m"
        assert config.cpu_limit == 0.5
        assert config.timeout_seconds == 30
        assert config.network_disabled is True
        assert config.image == "python:3.12-slim"
        assert config.workspace_path == "/workspace"
        assert config.input_path == "/workspace/input"
        assert config.output_path == "/workspace/output"
        assert config.kb_docs_path == "/workspace/kb_docs"
        assert config.main_script_path == "/workspace/main.py"

    def test_custom_config(self):
        """Test custom configuration."""
        config = CodeExecutionConfig(
            memory_limit="1g",
            cpu_limit=1.0,
            timeout_seconds=60,
            network_disabled=False,
            image="python:3.10-slim",
        )

        assert config.memory_limit == "1g"
        assert config.cpu_limit == 1.0
        assert config.timeout_seconds == 60
        assert config.network_disabled is False
        assert config.image == "python:3.10-slim"

    def test_default_packages(self):
        """Test default packages list."""
        config = CodeExecutionConfig()

        assert "numpy" in config.packages
        assert "pandas" in config.packages
        assert "matplotlib" in config.packages


class TestInputFile:
    """Test InputFile dataclass."""

    def test_from_text(self):
        """Test creating InputFile from text."""
        input_file = InputFile.from_text("test.txt", "Hello, World!")

        assert input_file.filename == "test.txt"
        assert input_file.content == b"Hello, World!"
        assert input_file.mime_type == "text/plain"

    def test_from_base64(self):
        """Test creating InputFile from base64."""
        original_content = b"Binary content"
        encoded = base64.b64encode(original_content).decode("utf-8")

        input_file = InputFile.from_base64(
            "data.bin", encoded, mime_type="application/octet-stream"
        )

        assert input_file.filename == "data.bin"
        assert input_file.content == original_content
        assert input_file.mime_type == "application/octet-stream"

    def test_direct_creation(self):
        """Test direct InputFile creation."""
        input_file = InputFile(
            filename="image.png",
            content=b"\x89PNG...",
            mime_type="image/png",
        )

        assert input_file.filename == "image.png"
        assert input_file.content == b"\x89PNG..."
        assert input_file.mime_type == "image/png"


class TestOutputFile:
    """Test OutputFile dataclass."""

    def test_to_base64(self):
        """Test converting content to base64."""
        content = b"Output data"
        output_file = OutputFile(
            filename="output.txt",
            content=content,
            size_bytes=len(content),
        )

        result = output_file.to_base64()
        expected = base64.b64encode(content).decode("utf-8")

        assert result == expected

    def test_to_text(self):
        """Test converting content to text."""
        text = "Hello, output!"
        output_file = OutputFile(
            filename="output.txt",
            content=text.encode("utf-8"),
            size_bytes=len(text),
        )

        result = output_file.to_text()

        assert result == text

    def test_output_file_with_mime_type(self):
        """Test OutputFile with MIME type."""
        output_file = OutputFile(
            filename="chart.png",
            content=b"\x89PNG...",
            mime_type="image/png",
            size_bytes=7,
        )

        assert output_file.filename == "chart.png"
        assert output_file.mime_type == "image/png"
        assert output_file.size_bytes == 7


class TestKBDocument:
    """Test KBDocument dataclass."""

    def test_kb_document_creation(self):
        """Test KBDocument creation."""
        doc = KBDocument(
            filename="doc.txt",
            content="Document content",
            document_id="doc123",
            metadata={"source": "confluence"},
        )

        assert doc.filename == "doc.txt"
        assert doc.content == "Document content"
        assert doc.document_id == "doc123"
        assert doc.metadata == {"source": "confluence"}

    def test_kb_document_defaults(self):
        """Test KBDocument default values."""
        doc = KBDocument(
            filename="doc.txt",
            content="Content",
        )

        assert doc.document_id is None
        assert doc.metadata == {}


class TestCodeExecutionResult:
    """Test CodeExecutionResult dataclass."""

    def test_result_creation(self):
        """Test CodeExecutionResult creation."""
        result = CodeExecutionResult(
            execution_id="exec123",
            status=ExecutionStatus.SUCCESS,
            stdout="Hello, World!\n",
            stderr="",
            exit_code=0,
            duration_ms=150.5,
        )

        assert result.execution_id == "exec123"
        assert result.status == ExecutionStatus.SUCCESS
        assert result.stdout == "Hello, World!\n"
        assert result.exit_code == 0
        assert result.is_success() is True

    def test_is_success(self):
        """Test is_success method."""
        success_result = CodeExecutionResult(
            execution_id="1",
            status=ExecutionStatus.SUCCESS,
        )
        error_result = CodeExecutionResult(
            execution_id="2",
            status=ExecutionStatus.ERROR,
        )

        assert success_result.is_success() is True
        assert error_result.is_success() is False

    def test_to_dict(self):
        """Test to_dict serialization."""
        output_file = OutputFile(
            filename="output.txt",
            content=b"test",
            mime_type="text/plain",
            size_bytes=4,
        )

        result = CodeExecutionResult(
            execution_id="exec123",
            status=ExecutionStatus.SUCCESS,
            stdout="Hello",
            stderr="",
            output_files=[output_file],
            started_at=datetime(2024, 1, 1, 12, 0, 0),
            completed_at=datetime(2024, 1, 1, 12, 0, 1),
            duration_ms=1000.0,
            exit_code=0,
        )

        result_dict = result.to_dict()

        assert result_dict["execution_id"] == "exec123"
        assert result_dict["status"] == "success"
        assert result_dict["stdout"] == "Hello"
        assert result_dict["exit_code"] == 0
        assert result_dict["duration_ms"] == 1000.0
        assert len(result_dict["output_files"]) == 1
        assert result_dict["output_files"][0]["filename"] == "output.txt"


# =============================================================================
# CodeExecutorService Tests
# =============================================================================


class TestCodeExecutorServiceInit:
    """Test CodeExecutorService initialization."""

    def test_init_with_default_config(self):
        """Test initialization with default config."""
        executor = CodeExecutorService()

        assert executor.config is not None
        assert executor.config.memory_limit == "512m"
        assert executor._docker_client is None
        assert executor._docker_available is None

    def test_init_with_custom_config(self):
        """Test initialization with custom config."""
        config = CodeExecutionConfig(
            memory_limit="1g",
            timeout_seconds=60,
        )
        executor = CodeExecutorService(config=config)

        assert executor.config.memory_limit == "1g"
        assert executor.config.timeout_seconds == 60


class TestDockerAvailability:
    """Test Docker availability checking."""

    def test_is_docker_available_cached(self):
        """Test that Docker availability is cached."""
        executor = CodeExecutorService()
        executor._docker_available = True

        # Should return cached value without calling Docker
        assert executor.is_docker_available() is True

    @patch(
        "assistant_service.core.code_executor.CodeExecutorService.docker_client",
        new_callable=PropertyMock,
    )
    def test_is_docker_available_when_docker_works(self, mock_client_prop):
        """Test Docker available when Docker is working."""
        mock_client = MagicMock()
        mock_client.ping.return_value = True
        mock_client.info.return_value = {"Runtimes": {"runsc": {}}}
        mock_client_prop.return_value = mock_client

        executor = CodeExecutorService()
        result = executor.is_docker_available()

        assert result is True
        mock_client.ping.assert_called()

    @patch(
        "assistant_service.core.code_executor.CodeExecutorService.docker_client",
        new_callable=PropertyMock,
    )
    def test_is_docker_available_requires_configured_sandbox_runtime(self, mock_client_prop):
        """Docker without the configured sandbox runtime fails closed by default."""
        mock_client = MagicMock()
        mock_client.ping.return_value = True
        mock_client.info.return_value = {"Runtimes": {"runc": {}}}
        mock_client_prop.return_value = mock_client

        executor = CodeExecutorService()
        result = executor.is_docker_available()

        assert result is False

    @patch(
        "assistant_service.core.code_executor.CodeExecutorService.docker_client",
        new_callable=PropertyMock,
    )
    def test_is_docker_available_when_docker_fails(self, mock_client_prop):
        """Test Docker not available when Docker fails."""
        mock_client = MagicMock()
        mock_client.ping.side_effect = Exception("Docker not running")
        mock_client_prop.return_value = mock_client

        executor = CodeExecutorService()
        result = executor.is_docker_available()

        assert result is False

    @patch(
        "assistant_service.core.code_executor.CodeExecutorService.docker_client",
        new_callable=PropertyMock,
    )
    def test_is_docker_available_when_client_is_none(self, mock_client_prop):
        """Test Docker not available when client is None."""
        mock_client_prop.return_value = None

        executor = CodeExecutorService()
        result = executor.is_docker_available()

        assert result is False


class TestWorkspaceSetup:
    """Test workspace setup functionality."""

    @pytest.mark.asyncio
    async def test_setup_workspace_creates_directories(self):
        """Test that workspace setup creates required directories."""
        executor = CodeExecutorService()
        config = CodeExecutionConfig()

        workspace = await executor._setup_workspace(
            code="print('hello')",
            input_files=[],
            kb_documents=[],
            config=config,
        )

        try:
            assert workspace.exists()
            assert (workspace / "input").exists()
            assert (workspace / "output").exists()
            assert (workspace / "kb_docs").exists()
            assert (workspace / "main.py").exists()
        finally:
            await executor._cleanup_workspace(workspace)

    @pytest.mark.asyncio
    async def test_setup_workspace_writes_code(self):
        """Test that workspace setup writes main.py with matplotlib setup."""
        executor = CodeExecutorService()
        config = CodeExecutionConfig()

        workspace = await executor._setup_workspace(
            code="print('hello')",
            input_files=[],
            kb_documents=[],
            config=config,
        )

        try:
            main_py = workspace / "main.py"
            content = main_py.read_text()

            assert MATPLOTLIB_SETUP in content
            assert "print('hello')" in content
        finally:
            await executor._cleanup_workspace(workspace)

    @pytest.mark.asyncio
    async def test_setup_workspace_writes_input_files(self):
        """Test that workspace setup writes input files."""
        executor = CodeExecutorService()
        config = CodeExecutionConfig()

        input_files = [
            InputFile.from_text("data.txt", "sample data"),
            InputFile(filename="binary.bin", content=b"\x00\x01\x02"),
        ]

        workspace = await executor._setup_workspace(
            code="print('hello')",
            input_files=input_files,
            kb_documents=[],
            config=config,
        )

        try:
            data_txt = workspace / "input" / "data.txt"
            binary_bin = workspace / "input" / "binary.bin"

            assert data_txt.exists()
            assert data_txt.read_text() == "sample data"
            assert binary_bin.exists()
            assert binary_bin.read_bytes() == b"\x00\x01\x02"
        finally:
            await executor._cleanup_workspace(workspace)

    @pytest.mark.asyncio
    async def test_setup_workspace_writes_kb_documents(self):
        """Test that workspace setup writes KB documents."""
        executor = CodeExecutorService()
        config = CodeExecutionConfig()

        kb_documents = [
            KBDocument(filename="doc1.txt", content="Document 1 content"),
            KBDocument(filename="doc2.txt", content="Document 2 content", document_id="d2"),
        ]

        workspace = await executor._setup_workspace(
            code="print('hello')",
            input_files=[],
            kb_documents=kb_documents,
            config=config,
        )

        try:
            doc1 = workspace / "kb_docs" / "doc1.txt"
            doc2 = workspace / "kb_docs" / "doc2.txt"

            assert doc1.exists()
            assert doc1.read_text() == "Document 1 content"
            assert doc2.exists()
            assert doc2.read_text() == "Document 2 content"
        finally:
            await executor._cleanup_workspace(workspace)

    @pytest.mark.asyncio
    @pytest.mark.parametrize(
        "unsafe_name",
        [
            "../escape.txt",
            "nested/escape.txt",
            r"nested\escape.txt",
            "/tmp/escape.txt",
            "escape\x00.txt",
            "escape\n.txt",
        ],
    )
    async def test_setup_workspace_rejects_non_leaf_filenames(self, unsafe_name):
        executor = CodeExecutorService()

        with pytest.raises(ValueError, match="workspace filename"):
            await executor._setup_workspace(
                code="print('hello')",
                input_files=[InputFile.from_text(unsafe_name, "input")],
                kb_documents=[],
                config=CodeExecutionConfig(),
            )

        with pytest.raises(ValueError, match="workspace filename"):
            await executor._setup_workspace(
                code="print('hello')",
                input_files=[],
                kb_documents=[KBDocument(filename=unsafe_name, content="document")],
                config=CodeExecutionConfig(),
            )


class TestMimeTypeGuessing:
    """Test MIME type guessing."""

    def test_guess_mime_type_common_types(self):
        """Test MIME type guessing for common file types."""
        executor = CodeExecutorService()

        assert executor._guess_mime_type("image.png") == "image/png"
        assert executor._guess_mime_type("image.jpg") == "image/jpeg"
        assert executor._guess_mime_type("image.jpeg") == "image/jpeg"
        assert executor._guess_mime_type("data.csv") == "text/csv"
        assert executor._guess_mime_type("data.json") == "application/json"
        assert executor._guess_mime_type("doc.pdf") == "application/pdf"
        assert executor._guess_mime_type("text.txt") == "text/plain"

    def test_guess_mime_type_unknown(self):
        """Test MIME type guessing for unknown types."""
        executor = CodeExecutorService()

        assert executor._guess_mime_type("file.xyz") == "application/octet-stream"
        assert executor._guess_mime_type("noextension") == "application/octet-stream"


class TestOutputFileCollection:
    """Test output file collection."""

    @pytest.mark.asyncio
    async def test_collect_output_files(self):
        """Test collecting output files from workspace."""
        executor = CodeExecutorService()
        config = CodeExecutionConfig()

        # Create temp workspace with output files
        workspace = Path(tempfile.mkdtemp(prefix="test_"))
        output_dir = workspace / "output"
        output_dir.mkdir()

        (output_dir / "result.txt").write_text("Result content")
        (output_dir / "chart.png").write_bytes(b"\x89PNG...")

        try:
            output_files = await executor._collect_output_files(workspace, config)

            assert len(output_files) == 2

            # Find files by name
            result_file = next(f for f in output_files if f.filename == "result.txt")
            chart_file = next(f for f in output_files if f.filename == "chart.png")

            assert result_file.to_text() == "Result content"
            assert result_file.mime_type == "text/plain"
            assert chart_file.content == b"\x89PNG..."
            assert chart_file.mime_type == "image/png"
        finally:
            import shutil

            shutil.rmtree(workspace)

    @pytest.mark.asyncio
    async def test_collect_output_files_empty_directory(self):
        """Test collecting from empty output directory."""
        executor = CodeExecutorService()
        config = CodeExecutionConfig()

        workspace = Path(tempfile.mkdtemp(prefix="test_"))
        output_dir = workspace / "output"
        output_dir.mkdir()

        try:
            output_files = await executor._collect_output_files(workspace, config)
            assert output_files == []
        finally:
            import shutil

            shutil.rmtree(workspace)

    @pytest.mark.asyncio
    async def test_collect_output_files_no_output_dir(self):
        """Test collecting when output directory doesn't exist."""
        executor = CodeExecutorService()
        config = CodeExecutionConfig()

        workspace = Path(tempfile.mkdtemp(prefix="test_"))

        try:
            output_files = await executor._collect_output_files(workspace, config)
            # Recovery sweep now creates output/ as a side effect so we
            # may get an empty list rather than a short-circuit, but no
            # files should be produced either way.
            assert output_files == []
        finally:
            import shutil

            shutil.rmtree(workspace)

    @pytest.mark.asyncio
    async def test_collect_recovers_root_level_png(self):
        """Stray PNG at workspace root gets swept into output/ so the
        collector returns it — this is the fix for the matplotlib-shim
        miss (PIL.Image.save / bare savefig / pathlib.Path etc.)."""
        executor = CodeExecutorService()
        config = CodeExecutionConfig()

        workspace = Path(tempfile.mkdtemp(prefix="test_"))
        # Pretend the sandbox wrote main.py (scaffolding) and a chart at
        # the workspace root without ever touching output/.
        (workspace / "main.py").write_text("# user code")
        (workspace / "chart.png").write_bytes(b"\x89PNG\r\n\x1a\nFAKE")

        # Also create reserved dirs the way _setup_workspace would.
        (workspace / "input").mkdir()
        (workspace / "kb_docs").mkdir()

        try:
            output_files = await executor._collect_output_files(workspace, config)

            names = [f.filename for f in output_files]
            assert "chart.png" in names, f"expected chart.png, got {names}"

            chart = next(f for f in output_files if f.filename == "chart.png")
            assert chart.mime_type == "image/png"
            assert chart.content.startswith(b"\x89PNG")

            # main.py (scaffolding) must NOT be returned as an artifact.
            assert "main.py" not in names

            # And the file must have actually been moved into output/.
            assert (workspace / "output" / "chart.png").exists()
            assert not (workspace / "chart.png").exists()
        finally:
            import shutil

            shutil.rmtree(workspace)

    @pytest.mark.asyncio
    async def test_collect_skips_reserved_dirs_and_non_artifacts(self):
        """Files under input/ or kb_docs/, and non-artifact extensions
        at the root (e.g. `.pyc`), must not be swept into output/."""
        executor = CodeExecutorService()
        config = CodeExecutionConfig()

        workspace = Path(tempfile.mkdtemp(prefix="test_"))
        (workspace / "main.py").write_text("# user code")
        (workspace / "input").mkdir()
        (workspace / "input" / "upload.csv").write_text("a,b\n1,2\n")
        (workspace / "kb_docs").mkdir()
        (workspace / "kb_docs" / "ref.txt").write_text("ref")

        # Stray non-artifact at root: should be left alone.
        (workspace / "scratch.pyc").write_bytes(b"\x00")
        # Stray artifact at root: should be recovered.
        (workspace / "data.csv").write_text("x,y\n1,2\n")

        try:
            output_files = await executor._collect_output_files(workspace, config)
            names = [f.filename for f in output_files]

            assert names == ["data.csv"], f"unexpected collection: {names}"
            # Reserved-dir contents stay put.
            assert (workspace / "input" / "upload.csv").exists()
            assert (workspace / "kb_docs" / "ref.txt").exists()
            # Non-artifact stays put.
            assert (workspace / "scratch.pyc").exists()
        finally:
            import shutil

            shutil.rmtree(workspace)


class TestExecuteWithMocks:
    """Test execute method with mocked Docker."""

    @pytest.mark.asyncio
    async def test_execute_docker_not_available(self):
        """Test execute when Docker is not available."""
        executor = CodeExecutorService()
        executor._docker_available = False

        result = await executor.execute(code="print('hello')")

        assert result.status == ExecutionStatus.ERROR
        assert "Docker is not available" in result.error_message
        assert result.execution_id is not None

    @pytest.mark.asyncio
    @patch.object(CodeExecutorService, "is_docker_available", return_value=True)
    @patch.object(CodeExecutorService, "_run_container")
    @patch.object(CodeExecutorService, "_cleanup_container")
    @patch.object(CodeExecutorService, "_cleanup_workspace")
    async def test_execute_success(
        self,
        mock_cleanup_workspace,
        mock_cleanup_container,
        mock_run_container,
        _mock_docker_available,
    ):
        """Test successful code execution."""
        mock_container = MagicMock()
        mock_run_container.return_value = (mock_container, "Hello, World!\n", "", 0)

        executor = CodeExecutorService()
        result = await executor.execute(code="print('Hello, World!')")

        assert result.status == ExecutionStatus.SUCCESS
        assert result.stdout == "Hello, World!\n"
        assert result.exit_code == 0
        assert result.error_message is None
        mock_cleanup_container.assert_called_once_with(mock_container)
        mock_cleanup_workspace.assert_called_once()

    @pytest.mark.asyncio
    @patch.object(CodeExecutorService, "is_docker_available", return_value=True)
    @patch.object(CodeExecutorService, "_run_container")
    @patch.object(CodeExecutorService, "_cleanup_container")
    @patch.object(CodeExecutorService, "_cleanup_workspace")
    async def test_execute_with_error(
        self,
        _mock_cleanup_workspace,
        _mock_cleanup_container,
        mock_run_container,
        _mock_docker_available,
    ):
        """Test code execution with error."""
        mock_container = MagicMock()
        mock_run_container.return_value = (
            mock_container,
            "",
            "NameError: name 'undefined' is not defined",
            1,
        )

        executor = CodeExecutorService()
        result = await executor.execute(code="print(undefined)")

        assert result.status == ExecutionStatus.ERROR
        assert result.exit_code == 1
        assert "Process exited with code 1" in result.error_message

    @pytest.mark.asyncio
    @patch.object(CodeExecutorService, "is_docker_available", return_value=True)
    @patch.object(CodeExecutorService, "_run_container")
    @patch.object(CodeExecutorService, "_cleanup_container")
    @patch.object(CodeExecutorService, "_cleanup_workspace")
    async def test_execute_timeout(
        self,
        _mock_cleanup_workspace,
        _mock_cleanup_container,
        mock_run_container,
        _mock_docker_available,
    ):
        """Test code execution with timeout."""
        mock_run_container.side_effect = asyncio.TimeoutError()

        executor = CodeExecutorService()
        result = await executor.execute(code="import time; time.sleep(100)")

        assert result.status == ExecutionStatus.TIMEOUT
        assert "timed out" in result.error_message

    @pytest.mark.asyncio
    @patch.object(CodeExecutorService, "is_docker_available", return_value=True)
    @patch.object(CodeExecutorService, "_setup_workspace")
    @patch.object(CodeExecutorService, "_cleanup_workspace")
    async def test_execute_with_input_files(
        self,
        _mock_cleanup_workspace,
        mock_setup_workspace,
        _mock_docker_available,
    ):
        """Test execute passes input files to workspace setup."""
        mock_setup_workspace.side_effect = Exception("Test stop")

        executor = CodeExecutorService()
        input_files = [InputFile.from_text("data.txt", "test data")]

        await executor.execute(
            code="print('hello')",
            input_files=input_files,
        )

        # Verify input_files were passed
        call_args = mock_setup_workspace.call_args
        assert call_args.kwargs["input_files"] == input_files


class TestFactoryFunction:
    """Test get_code_executor factory function."""

    def test_get_code_executor_singleton(self):
        """Test that get_code_executor returns singleton."""
        # Reset global
        import assistant_service.core.code_executor as module

        module._code_executor = None

        executor1 = get_code_executor()
        executor2 = get_code_executor()

        assert executor1 is executor2

        # Cleanup
        module._code_executor = None

    def test_get_code_executor_with_config(self):
        """Test get_code_executor with custom config."""
        import assistant_service.core.code_executor as module

        module._code_executor = None

        config = CodeExecutionConfig(memory_limit="1g")
        executor = get_code_executor(config=config)

        assert executor.config.memory_limit == "1g"

        # Cleanup
        module._code_executor = None


class TestMatplotlibSetup:
    """Test matplotlib backend setup."""

    def test_matplotlib_setup_content(self):
        """Test that MATPLOTLIB_SETUP contains required code."""
        assert "matplotlib" in MATPLOTLIB_SETUP
        assert "Agg" in MATPLOTLIB_SETUP
        assert "use('Agg')" in MATPLOTLIB_SETUP
