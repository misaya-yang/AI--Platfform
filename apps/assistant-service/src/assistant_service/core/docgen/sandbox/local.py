"""LocalSubprocessSandbox — for dev & unit tests.

This is NOT a security boundary. It's a convenience backend that:

* runs jobs in a fresh tempdir
* scrubs env vars (keeps only PATH / HOME / LANG by default)
* enforces a wall-clock timeout
* collects any files listed under ``produce`` back to the caller

In production, prefer :class:`DockerSandbox`, which delegates to a prebuilt
image with LibreOffice / Node / etc.
"""

from __future__ import annotations

import asyncio
import contextlib
import shutil
import tempfile
import time
from collections.abc import Iterable
from pathlib import Path

from .client import SandboxClient, SandboxResult, SandboxTimeout

_DEFAULT_KEEP_ENV = ("PATH", "HOME", "LANG", "LC_ALL", "LC_CTYPE", "TMPDIR", "PWD", "SHELL", "USER", "PYTHONUNBUFFERED")


class LocalSubprocessSandbox(SandboxClient):
    def __init__(
        self,
        *,
        python_executable: str = "python3",
        node_executable: str = "node",
        extra_keep_env: tuple[str, ...] = (),
        workdir_root: Path | None = None,
    ) -> None:
        self._python = python_executable
        self._node = node_executable
        self._keep_env = tuple(_DEFAULT_KEEP_ENV) + extra_keep_env
        self._workdir_root = workdir_root

    # ------------------------------------------------------------------ helpers

    def _make_workdir(self) -> Path:
        root = self._workdir_root
        if root is not None:
            root.mkdir(parents=True, exist_ok=True)
            return Path(tempfile.mkdtemp(prefix="docgen_sb_", dir=root))
        return Path(tempfile.mkdtemp(prefix="docgen_sb_"))

    def _seed_files(self, workdir: Path, files_in: Iterable[Path] | None) -> None:
        if not files_in:
            return
        for src in files_in:
            src = Path(src)
            if not src.exists():
                raise FileNotFoundError(f"sandbox input file missing: {src}")
            dst = workdir / src.name
            if src.is_dir():
                shutil.copytree(src, dst)
            else:
                shutil.copy2(src, dst)

    def _scrub_env(self, extra: dict[str, str] | None) -> dict[str, str]:
        import os

        env = {k: os.environ[k] for k in self._keep_env if k in os.environ}
        if extra:
            env.update(extra)
        return env

    def _collect_produced(self, workdir: Path, produce: Iterable[str] | None) -> list[Path]:
        if not produce:
            return []
        out: list[Path] = []
        for name in produce:
            p = workdir / name
            if p.exists():
                out.append(p)
        return out

    async def _run(
        self,
        argv: list[str],
        *,
        timeout: float,
        files_in: Iterable[Path] | None,
        produce: Iterable[str] | None,
        env: dict[str, str] | None,
    ) -> SandboxResult:
        workdir = self._make_workdir()
        self._seed_files(workdir, files_in)
        final_env = self._scrub_env(env)
        started = time.perf_counter()
        proc = await asyncio.create_subprocess_exec(
            *argv,
            cwd=str(workdir),
            env=final_env,
            stdout=asyncio.subprocess.PIPE,
            stderr=asyncio.subprocess.PIPE,
        )
        try:
            stdout_b, stderr_b = await asyncio.wait_for(proc.communicate(), timeout=timeout)
        except asyncio.TimeoutError:
            proc.kill()
            with contextlib.suppress(Exception):
                await proc.wait()
            raise SandboxTimeout(f"sandbox job exceeded {timeout}s (workdir={workdir})") from None
        duration_ms = int((time.perf_counter() - started) * 1000)
        return SandboxResult(
            exit_code=proc.returncode if proc.returncode is not None else -1,
            stdout=(stdout_b or b"").decode("utf-8", errors="replace"),
            stderr=(stderr_b or b"").decode("utf-8", errors="replace"),
            duration_ms=duration_ms,
            workdir=workdir,
            produced_files=self._collect_produced(workdir, produce),
        )

    # ------------------------------------------------------------------ api

    async def exec_python(
        self,
        code: str,
        *,
        timeout: float = 120.0,
        files_in: Iterable[Path] | None = None,
        produce: Iterable[str] | None = None,
        env: dict[str, str] | None = None,
    ) -> SandboxResult:
        return await self._run(
            [self._python, "-c", code],
            timeout=timeout,
            files_in=files_in,
            produce=produce,
            env=env,
        )

    async def exec_node(
        self,
        code: str,
        *,
        timeout: float = 120.0,
        files_in: Iterable[Path] | None = None,
        produce: Iterable[str] | None = None,
        env: dict[str, str] | None = None,
    ) -> SandboxResult:
        return await self._run(
            [self._node, "-e", code],
            timeout=timeout,
            files_in=files_in,
            produce=produce,
            env=env,
        )

    async def exec_bash(
        self,
        cmd: str,
        *,
        timeout: float = 120.0,
        files_in: Iterable[Path] | None = None,
        produce: Iterable[str] | None = None,
        env: dict[str, str] | None = None,
    ) -> SandboxResult:
        return await self._run(
            ["bash", "-lc", cmd],
            timeout=timeout,
            files_in=files_in,
            produce=produce,
            env=env,
        )
