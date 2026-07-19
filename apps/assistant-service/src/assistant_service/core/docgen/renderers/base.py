"""Renderer Protocol + shared result type."""

from __future__ import annotations

from dataclasses import dataclass, field
from pathlib import Path
from typing import Protocol, runtime_checkable


class RenderError(RuntimeError):
    pass


@dataclass
class RenderResult:
    path: Path
    doc_type: str
    bytes_size: int
    duration_ms: int
    warnings: list[str] = field(default_factory=list)
    extra: dict = field(default_factory=dict)


@runtime_checkable
class BaseRenderer(Protocol):
    """Protocol for a format-specific renderer."""

    format: str

    async def render(self, ir, out_dir: Path) -> RenderResult: ...

    async def fix(
        self,
        ir,
        critic_findings,
        out_dir: Path,
    ) -> RenderResult: ...
