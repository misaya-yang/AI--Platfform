"""Markdown source-of-truth memory store."""

from __future__ import annotations

import re
from dataclasses import dataclass
from datetime import date, datetime, timedelta, timezone
from pathlib import Path
from typing import Any


@dataclass
class MemorySourceDocument:
    """A markdown source document for long-term memory."""

    path: str
    source_type: str
    content: str
    updated_at: datetime


class MemorySourceStore:
    """Persist assistant memory as markdown sources per tenant/user."""

    _SAFE_COMPONENT_RE = re.compile(r"[^a-zA-Z0-9_.-]+")

    def __init__(self, base_dir: str | Path | None = None) -> None:
        default_dir = Path.home() / ".ai_gateway" / "assistant_memory"
        self.base_dir = Path(base_dir) if base_dir else default_dir

    @classmethod
    def _safe_component(cls, value: str) -> str:
        cleaned = cls._SAFE_COMPONENT_RE.sub("_", str(value or "").strip())
        cleaned = cleaned.strip("._")
        return cleaned or "unknown"

    def _user_root(self, tenant_id: str, user_id: str) -> Path:
        return self.base_dir / self._safe_component(tenant_id) / self._safe_component(user_id)

    def _ensure_user_dirs(self, tenant_id: str, user_id: str) -> tuple[Path, Path]:
        root = self._user_root(tenant_id, user_id)
        memory_dir = root / "memory"
        memory_dir.mkdir(parents=True, exist_ok=True)
        return root, memory_dir

    def _daily_path(self, tenant_id: str, user_id: str, day: date) -> Path:
        _, memory_dir = self._ensure_user_dirs(tenant_id, user_id)
        return memory_dir / f"{day.isoformat()}.md"

    def _long_term_path(self, tenant_id: str, user_id: str) -> Path:
        root, _ = self._ensure_user_dirs(tenant_id, user_id)
        return root / "MEMORY.md"

    def _reflection_path(self, tenant_id: str, user_id: str, day: date) -> Path:
        root, _ = self._ensure_user_dirs(tenant_id, user_id)
        return root / f"REFLECTION-{day.isoformat()}.md"

    def append_daily_entry(
        self,
        tenant_id: str,
        user_id: str,
        text: str,
        *,
        now: datetime | None = None,
    ) -> str:
        """Append a timestamped entry to the user's daily memory file."""
        ts = now or datetime.now(timezone.utc)
        path = self._daily_path(tenant_id, user_id, ts.date())
        with path.open("a", encoding="utf-8") as f:
            f.write(f"\n## {ts.isoformat()}\n")
            f.write(text.strip())
            f.write("\n")
        return str(path)

    def append_long_term_facts(
        self,
        tenant_id: str,
        user_id: str,
        facts: list[str],
        *,
        now: datetime | None = None,
    ) -> str:
        """Append curated long-term facts to MEMORY.md."""
        if not facts:
            return str(self._long_term_path(tenant_id, user_id))

        ts = now or datetime.now(timezone.utc)
        path = self._long_term_path(tenant_id, user_id)
        if not path.exists():
            path.write_text("# Long-Term Memory\n", encoding="utf-8")

        with path.open("a", encoding="utf-8") as f:
            f.write(f"\n## Update {ts.isoformat()}\n")
            for fact in facts:
                clean_fact = fact.strip()
                if clean_fact:
                    f.write(f"- {clean_fact}\n")
        return str(path)

    def append_reflection(
        self,
        tenant_id: str,
        user_id: str,
        reflection: str,
        *,
        day: date | None = None,
    ) -> str:
        """Store reflection summary for a day."""
        target_day = day or datetime.now(timezone.utc).date()
        path = self._reflection_path(tenant_id, user_id, target_day)
        content = f"# Reflection {target_day.isoformat()}\n\n{reflection.strip()}\n"
        path.write_text(content, encoding="utf-8")
        return str(path)

    def read_recent_sources(
        self,
        tenant_id: str,
        user_id: str,
        *,
        days: int = 2,
        include_long_term: bool = True,
        include_reflections: bool = True,
        now: datetime | None = None,
    ) -> list[MemorySourceDocument]:
        """Read memory markdown sources likely relevant for current context."""
        ts = now or datetime.now(timezone.utc)
        root, _ = self._ensure_user_dirs(tenant_id, user_id)

        docs: list[MemorySourceDocument] = []
        for offset in range(max(days, 1)):
            day = (ts - timedelta(days=offset)).date()
            daily_path = self._daily_path(tenant_id, user_id, day)
            if daily_path.exists():
                docs.append(self._to_document(daily_path, "daily"))
            if include_reflections:
                reflection_path = self._reflection_path(tenant_id, user_id, day)
                if reflection_path.exists():
                    docs.append(self._to_document(reflection_path, "reflection"))

        if include_long_term:
            long_term_path = self._long_term_path(tenant_id, user_id)
            if long_term_path.exists():
                docs.append(self._to_document(long_term_path, "long_term"))

        docs.sort(key=lambda d: d.updated_at, reverse=True)
        return docs

    def list_markdown_sources(
        self,
        tenant_id: str,
        user_id: str,
    ) -> list[str]:
        """List all markdown sources for a tenant/user."""
        root = self._user_root(tenant_id, user_id)
        if not root.exists():
            return []
        return sorted(str(path) for path in root.rglob("*.md"))

    @staticmethod
    def _to_document(path: Path, source_type: str) -> MemorySourceDocument:
        stat = path.stat()
        updated_at = datetime.fromtimestamp(stat.st_mtime, tz=timezone.utc)
        content = path.read_text(encoding="utf-8", errors="ignore")
        return MemorySourceDocument(
            path=str(path),
            source_type=source_type,
            content=content,
            updated_at=updated_at,
        )

    def inspect_user_tree(self, tenant_id: str, user_id: str) -> dict[str, Any]:
        """Return a compact snapshot of source files for observability."""
        files = self.list_markdown_sources(tenant_id, user_id)
        return {
            "tenant_id": tenant_id,
            "user_id": user_id,
            "base_dir": str(self._user_root(tenant_id, user_id)),
            "file_count": len(files),
            "files": files,
        }

    def delete_source(self, tenant_id: str, user_id: str, source_path: str) -> bool:
        """Delete a markdown source only when it belongs to this tenant/user."""
        root = self._user_root(tenant_id, user_id).resolve()
        target = Path(source_path).resolve()
        try:
            target.relative_to(root)
        except ValueError:
            return False

        if target.suffix != ".md" or not target.exists() or not target.is_file():
            return False

        target.unlink()
        return True
