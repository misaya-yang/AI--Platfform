"""Tests for the LocalSubprocessSandbox backend."""

from __future__ import annotations

import asyncio
import sys

import pytest
from assistant_service.core.docgen.sandbox import (
    DockerSandbox,
    LocalSubprocessSandbox,
    SandboxTimeout,
)


@pytest.mark.asyncio
async def test_exec_python_hello_world():
    sb = LocalSubprocessSandbox(python_executable=sys.executable)
    r = await sb.exec_python("print('hi from sandbox')")
    assert r.ok, r.stderr
    assert "hi from sandbox" in r.stdout


@pytest.mark.asyncio
async def test_exec_python_produces_file(tmp_path):
    sb = LocalSubprocessSandbox(python_executable=sys.executable)
    code = "open('out.txt','w').write('docgen ok')"
    r = await sb.exec_python(code, produce=["out.txt"])
    assert r.ok
    assert len(r.produced_files) == 1
    assert r.produced_files[0].read_text() == "docgen ok"


@pytest.mark.asyncio
async def test_exec_python_env_is_scrubbed():
    sb = LocalSubprocessSandbox(python_executable=sys.executable)
    # AWS_SECRET_ACCESS_KEY should NOT leak in.
    import os

    os.environ["AWS_SECRET_ACCESS_KEY"] = "SHOULD-NOT-LEAK"
    try:
        r = await sb.exec_python(
            "import os; print('SECRET:' + os.environ.get('AWS_SECRET_ACCESS_KEY', 'absent'))"
        )
    finally:
        del os.environ["AWS_SECRET_ACCESS_KEY"]
    assert "SECRET:absent" in r.stdout
    assert "SHOULD-NOT-LEAK" not in r.stdout


@pytest.mark.asyncio
async def test_exec_python_times_out():
    sb = LocalSubprocessSandbox(python_executable=sys.executable)
    with pytest.raises(SandboxTimeout):
        await sb.exec_python("import time; time.sleep(5)", timeout=0.2)


@pytest.mark.asyncio
async def test_exec_python_can_import_docgen_packages():
    """Confirms python-docx / pptx / openpyxl / reportlab are all importable.

    This is the Phase-0 'hello world' from the plan §5.
    """
    sb = LocalSubprocessSandbox(python_executable=sys.executable)
    r = await sb.exec_python(
        "import docx, pptx, openpyxl, reportlab; print('stack_ok')"
    )
    assert r.ok, r.stderr
    assert "stack_ok" in r.stdout


@pytest.mark.asyncio
async def test_docker_sandbox_passes_python_and_node_code_as_literal_argv(
    monkeypatch,
    tmp_path,
):
    calls: list[tuple[str, ...]] = []

    class CompletedProcess:
        returncode = 0

        async def communicate(self):
            return b"ok", b""

    async def fake_create_subprocess_exec(*argv, **_kwargs):
        calls.append(argv)
        return CompletedProcess()

    monkeypatch.setattr(asyncio, "create_subprocess_exec", fake_create_subprocess_exec)
    sandbox = DockerSandbox(workdir_root=tmp_path)
    python_code = "print('$HOME', '`id`', '$(touch should-not-run)')"
    node_code = "console.log('$HOME', '`id`', '$(touch should-not-run)')"

    await sandbox.exec_python(python_code)
    await sandbox.exec_node(node_code)

    assert calls[0][-4:] == (sandbox._image, "python3", "-c", python_code)
    assert calls[1][-4:] == (sandbox._image, "node", "-e", node_code)
