from __future__ import annotations

from dataclasses import dataclass


@dataclass(slots=True)
class ModuleStatus:
    name: str
    status: str
    detail: str | None = None
