"""Scan skill search paths and build a lazy SkillIndex."""

from __future__ import annotations

from collections.abc import Iterable
from dataclasses import dataclass
from pathlib import Path

from .loader import SkillLoader
from .models import Skill, SkillFrontmatter


@dataclass
class _Entry:
    name: str
    root: Path
    frontmatter: SkillFrontmatter
    resolved: Skill | None = None


class SkillRegistry:
    """Lazy registry: all frontmatters up front, bodies on demand."""

    def __init__(self, search_paths: Iterable[Path], loader: SkillLoader | None = None) -> None:
        self._paths = [Path(p) for p in search_paths]
        self._loader = loader or SkillLoader()
        self._index: dict[str, _Entry] = {}

    def scan(self) -> list[SkillFrontmatter]:
        """Walk each search path, load frontmatters.

        Returns the list of discovered frontmatters (stable order, first-seen wins).
        """
        self._index.clear()
        for base in self._paths:
            if not base.is_dir():
                continue
            for sub in sorted(base.iterdir()):
                if not sub.is_dir() or not (sub / "SKILL.md").is_file():
                    continue
                fm = self._loader.load_frontmatter_only(sub)
                if fm.name in self._index:
                    continue
                self._index[fm.name] = _Entry(name=fm.name, root=sub, frontmatter=fm)
        return [e.frontmatter for e in self._index.values()]

    def frontmatters(self) -> list[SkillFrontmatter]:
        return [e.frontmatter for e in self._index.values()]

    def get(self, name: str) -> Skill | None:
        entry = self._index.get(name)
        if entry is None:
            return None
        if entry.resolved is None:
            entry.resolved = self._loader.load(entry.root)
        return entry.resolved

    def __contains__(self, name: str) -> bool:
        return name in self._index

    def __iter__(self):
        return iter(self._index.values())
