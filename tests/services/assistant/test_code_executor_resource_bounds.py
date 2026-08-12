"""Focused regressions for code-executor cleanup and bounded result capture."""

from __future__ import annotations

import asyncio
from unittest.mock import AsyncMock, MagicMock

import assistant_service.core.code_executor as code_executor_module
import pytest
from assistant_service.core.code_executor import CodeExecutionConfig, CodeExecutorService
from requests.exceptions import ReadTimeout


def _docker_executor(monkeypatch: pytest.MonkeyPatch, tmp_path):
    container_root = tmp_path / "container-root"
    workspace = container_root / "code_exec_123"
    workspace.mkdir(parents=True)
    monkeypatch.setenv("SANDBOX_WORKSPACE", str(container_root))
    monkeypatch.setenv("SANDBOX_WORKSPACE_HOST", "/host/sandbox-root")
    container = MagicMock()
    client = MagicMock()
    client.containers.run.return_value = container
    executor = CodeExecutorService(CodeExecutionConfig(sandbox_runtime=None))
    executor._docker_client = client
    return executor, container, workspace


@pytest.mark.asyncio
async def test_transport_wait_timeout_kills_and_removes_container(monkeypatch, tmp_path):
    executor, container, workspace = _docker_executor(monkeypatch, tmp_path)
    container.wait.side_effect = ReadTimeout("synthetic docker wait timeout")

    with pytest.raises(asyncio.TimeoutError):
        await executor._run_container(workspace, executor.config)

    container.kill.assert_called_once_with()
    container.remove.assert_called_once_with(force=True)


@pytest.mark.asyncio
async def test_docker_logs_are_streamed_with_visible_byte_bound(monkeypatch, tmp_path):
    executor, container, workspace = _docker_executor(monkeypatch, tmp_path)
    monkeypatch.setattr(code_executor_module, "_MAX_STREAM_BYTES", 8, raising=False)
    container.wait.return_value = {"StatusCode": 0}
    container.logs.side_effect = [
        iter((b"12345", b"67890")),
        iter((b"error",)),
    ]

    returned, stdout, stderr, exit_code = await executor._run_container(workspace, executor.config)

    assert returned is container
    assert exit_code == 0
    assert stdout == "12345678\n...[stream truncated]"
    assert stderr == "error"
    assert all(call.kwargs["stream"] is True for call in container.logs.call_args_list)
    assert all(call.kwargs["follow"] is False for call in container.logs.call_args_list)


@pytest.mark.asyncio
async def test_output_files_have_per_file_aggregate_and_count_receipts(monkeypatch, tmp_path):
    monkeypatch.setattr(code_executor_module, "_MAX_OUTPUT_FILE_BYTES", 4, raising=False)
    monkeypatch.setattr(code_executor_module, "_MAX_OUTPUT_TOTAL_BYTES", 6, raising=False)
    monkeypatch.setattr(code_executor_module, "_MAX_OUTPUT_FILES", 2, raising=False)
    workspace = tmp_path / "workspace"
    output = workspace / "output"
    output.mkdir(parents=True)
    (output / "a.txt").write_bytes(b"abcdef")
    (output / "b.txt").write_bytes(b"12345")
    (output / "c.txt").write_bytes(b"omitted")
    receipts: list[dict] = []

    files = await CodeExecutorService()._collect_output_files(
        workspace,
        CodeExecutionConfig(),
        truncation_receipts=receipts,
    )

    assert len(files) == 2
    assert files[0].content in {b"abcd", b"1234", b"omit"}
    assert files[0].size_bytes in {5, 6, 7}
    assert files[0].captured_size_bytes == 4
    assert files[0].content_truncated is True
    assert len(files[1].content) == 2
    assert files[1].size_bytes in {5, 6, 7}
    assert files[1].captured_size_bytes == 2
    assert files[1].content_truncated is True
    assert {receipt["reason"] for receipt in receipts} >= {
        "per_file_byte_limit",
        "aggregate_byte_limit",
        "file_count_limit",
    }
    assert all(receipt["truncated"] is True for receipt in receipts)


def test_result_serializes_visible_truncation_receipt():
    result = code_executor_module.CodeExecutionResult(
        execution_id="exec-1",
        status=code_executor_module.ExecutionStatus.SUCCESS,
        truncation_receipts=[
            {
                "kind": "output_files",
                "reason": "aggregate_byte_limit",
                "truncated": True,
                "limit_bytes": 6,
            }
        ],
    )

    assert result.to_dict()["truncation_receipts"] == result.truncation_receipts


@pytest.mark.asyncio
async def test_execute_surfaces_file_truncation_in_existing_tool_visible_stderr(
    monkeypatch, tmp_path
):
    executor = CodeExecutorService(CodeExecutionConfig(sandbox_runtime=None))
    container = MagicMock()
    monkeypatch.setattr(executor, "is_docker_available", lambda: True)
    monkeypatch.setattr(executor, "_setup_workspace", AsyncMock(return_value=tmp_path))
    monkeypatch.setattr(
        executor,
        "_run_container",
        AsyncMock(return_value=(container, "", "", 0)),
    )

    async def collect_files(*, workspace_dir, config, truncation_receipts):
        _ = workspace_dir, config
        truncation_receipts.append(
            {
                "kind": "output_files",
                "reason": "aggregate_byte_limit",
                "truncated": True,
                "limit_bytes": 6,
            }
        )
        return []

    monkeypatch.setattr(executor, "_collect_output_files", collect_files)
    monkeypatch.setattr(executor, "_cleanup_container", AsyncMock())
    monkeypatch.setattr(executor, "_cleanup_workspace", AsyncMock())

    result = await executor.execute("print('bounded')")

    assert "result capture truncated by host safety limits" in result.stderr
    assert "aggregate_byte_limit=1" in result.stderr
    assert result.truncation_receipts[0]["reason"] == "aggregate_byte_limit"
