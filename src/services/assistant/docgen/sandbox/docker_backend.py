"""DockerSandbox — production backend.

Delegates to a prebuilt image (docker/sandbox.Dockerfile) that has:
  LibreOffice, Python (python-docx, pptx, openpyxl, reportlab, weasyprint,
  pypdf, matplotlib, pillow, markdown, markitdown, docxtpl, pypdfium2),
  Node 20 (docx-js, pptxgenjs, pdf-lib), poppler-utils, tesseract, Noto CJK.

The interface matches ``LocalSubprocessSandbox`` so callers don't need to
care which backend is wired up.
"""

from __future__ import annotations

import asyncio
import json
import shutil
import tempfile
import time
from pathlib import Path
from typing import Iterable, Optional

from .client import SandboxClient, SandboxResult, SandboxTimeout, SandboxError


class DockerSandbox(SandboxClient):
    def __init__(
        self,
        *,
        image: str = "ai-gateway/docgen-sandbox:latest",
        docker_bin: str = "docker",
        network: str = "none",
        memory_mb: int = 1024,
        cpus: float = 1.0,
        workdir_root: Optional[Path] = None,
    ) -> None:
        self._image = image
        self._docker = docker_bin
        self._network = network
        self._memory_mb = memory_mb
        self._cpus = cpus
        self._workdir_root = workdir_root

    async def _run_container(
        self,
        shell_cmd: str,
        *,
        timeout: float,
        files_in: Optional[Iterable[Path]],
        produce: Optional[Iterable[str]],
        env: Optional[dict[str, str]],
    ) -> SandboxResult:
        workdir = Path(tempfile.mkdtemp(prefix="docgen_dk_", dir=self._workdir_root or None))
        if files_in:
            for src in files_in:
                src = Path(src)
                dst = workdir / src.name
                if src.is_dir():
                    shutil.copytree(src, dst)
                else:
                    shutil.copy2(src, dst)

        argv = [
            self._docker,
            "run",
            "--rm",
            "--network",
            self._network,
            "--memory",
            f"{self._memory_mb}m",
            "--cpus",
            f"{self._cpus}",
            "-v",
            f"{workdir}:/work",
            "-w",
            "/work",
        ]
        for k, v in (env or {}).items():
            argv.extend(["-e", f"{k}={v}"])
        argv.extend([self._image, "bash", "-lc", shell_cmd])

        started = time.perf_counter()
        proc = await asyncio.create_subprocess_exec(
            *argv,
            stdout=asyncio.subprocess.PIPE,
            stderr=asyncio.subprocess.PIPE,
        )
        try:
            stdout_b, stderr_b = await asyncio.wait_for(proc.communicate(), timeout=timeout)
        except asyncio.TimeoutError:
            proc.kill()
            try:
                await proc.wait()
            except Exception:
                pass
            raise SandboxTimeout(f"docker job exceeded {timeout}s (workdir={workdir})") from None
        duration_ms = int((time.perf_counter() - started) * 1000)

        if proc.returncode is None:
            raise SandboxError("docker returncode is None")

        produced: list[Path] = []
        for name in produce or ():
            p = workdir / name
            if p.exists():
                produced.append(p)

        return SandboxResult(
            exit_code=proc.returncode,
            stdout=(stdout_b or b"").decode("utf-8", errors="replace"),
            stderr=(stderr_b or b"").decode("utf-8", errors="replace"),
            duration_ms=duration_ms,
            workdir=workdir,
            produced_files=produced,
        )

    async def exec_python(
        self,
        code: str,
        *,
        timeout: float = 120.0,
        files_in: Optional[Iterable[Path]] = None,
        produce: Optional[Iterable[str]] = None,
        env: Optional[dict[str, str]] = None,
    ) -> SandboxResult:
        payload = json.dumps(code)
        shell = f"python3 -c {payload}"
        return await self._run_container(shell, timeout=timeout, files_in=files_in, produce=produce, env=env)

    async def exec_node(
        self,
        code: str,
        *,
        timeout: float = 120.0,
        files_in: Optional[Iterable[Path]] = None,
        produce: Optional[Iterable[str]] = None,
        env: Optional[dict[str, str]] = None,
    ) -> SandboxResult:
        payload = json.dumps(code)
        shell = f"node -e {payload}"
        return await self._run_container(shell, timeout=timeout, files_in=files_in, produce=produce, env=env)

    async def exec_bash(
        self,
        cmd: str,
        *,
        timeout: float = 120.0,
        files_in: Optional[Iterable[Path]] = None,
        produce: Optional[Iterable[str]] = None,
        env: Optional[dict[str, str]] = None,
    ) -> SandboxResult:
        return await self._run_container(cmd, timeout=timeout, files_in=files_in, produce=produce, env=env)
