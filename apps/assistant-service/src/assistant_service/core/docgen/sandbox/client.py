"""SandboxClient Protocol + shared result / error types."""

from __future__ import annotations

from collections.abc import Iterable
from dataclasses import dataclass, field
from pathlib import Path
from typing import Protocol


class SandboxError(RuntimeError):
    """Sandbox refused to run / returned a non-zero status we cannot interpret."""


class SandboxTimeout(SandboxError):
    """Sandbox killed the job for exceeding its time budget."""


@dataclass
class SandboxResult:
    exit_code: int
    stdout: str
    stderr: str
    duration_ms: int
    workdir: Path
    produced_files: list[Path] = field(default_factory=list)

    @property
    def ok(self) -> bool:
        return self.exit_code == 0


class SandboxClient(Protocol):
    """Minimal surface every sandbox backend implements."""

    async def exec_python(
        self,
        code: str,
        *,
        timeout: float = 120.0,
        files_in: Iterable[Path] | None = None,
        produce: Iterable[str] | None = None,
        env: dict[str, str] | None = None,
    ) -> SandboxResult: ...

    async def exec_node(
        self,
        code: str,
        *,
        timeout: float = 120.0,
        files_in: Iterable[Path] | None = None,
        produce: Iterable[str] | None = None,
        env: dict[str, str] | None = None,
    ) -> SandboxResult: ...

    async def exec_bash(
        self,
        cmd: str,
        *,
        timeout: float = 120.0,
        files_in: Iterable[Path] | None = None,
        produce: Iterable[str] | None = None,
        env: dict[str, str] | None = None,
    ) -> SandboxResult: ...
