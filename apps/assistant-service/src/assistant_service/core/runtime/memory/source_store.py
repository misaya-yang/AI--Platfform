"""Markdown source-of-truth memory store."""

from __future__ import annotations

import re
import threading
from dataclasses import dataclass
from datetime import date, datetime, timedelta, timezone
from pathlib import Path
from typing import Any

from .lifecycle import (
    MemoryWriteResult,
    bounded_memory_text,
    memory_content_hash,
    scan_memory_text,
)


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
        self._lock_registry_guard = threading.Lock()
        self._path_locks: dict[str, threading.Lock] = {}

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

    def _profile_path(self, tenant_id: str, user_id: str) -> Path:
        root, _ = self._ensure_user_dirs(tenant_id, user_id)
        return root / "USER.md"

    @staticmethod
    def _atomic_write(path: Path, content: str) -> None:
        path.parent.mkdir(parents=True, exist_ok=True)
        tmp_path = path.with_name(f".{path.name}.tmp")
        tmp_path.write_text(content, encoding="utf-8")
        tmp_path.replace(path)

    def _path_lock(self, path: Path) -> threading.Lock:
        key = str(path.resolve())
        with self._lock_registry_guard:
            lock = self._path_locks.get(key)
            if lock is None:
                lock = threading.Lock()
                self._path_locks[key] = lock
            return lock

    @staticmethod
    def _read_existing(path: Path) -> str:
        if not path.exists():
            return ""
        return path.read_text(encoding="utf-8", errors="ignore")

    @staticmethod
    def _contains_entry(existing: str, entry: str) -> bool:
        normalized_existing = "\n".join(
            line.strip() for line in existing.splitlines() if line.strip()
        )
        normalized_entry = "\n".join(
            line.strip() for line in entry.splitlines() if line.strip()
        )
        return bool(normalized_entry and normalized_entry in normalized_existing)

    def append_daily_entry(
        self,
        tenant_id: str,
        user_id: str,
        text: str,
        *,
        now: datetime | None = None,
    ) -> str:
        """Append a timestamped entry to the user's daily memory file."""
        return self.append_daily_entry_result(
            tenant_id,
            user_id,
            text,
            now=now,
        ).path

    def append_daily_entry_result(
        self,
        tenant_id: str,
        user_id: str,
        text: str,
        *,
        now: datetime | None = None,
    ) -> MemoryWriteResult:
        """Append a timestamped daily entry and return bounded write metadata."""
        ts = now or datetime.now(timezone.utc)
        path = self._daily_path(tenant_id, user_id, ts.date())
        clean_text = bounded_memory_text(text)
        entry = f"\n## {ts.isoformat()}\n{clean_text}\n"
        with self._path_lock(path):
            existing = self._read_existing(path)
            duplicate = self._contains_entry(existing, clean_text)
            if not duplicate:
                self._atomic_write(path, existing + entry)
        return MemoryWriteResult(
            path=str(path),
            source_type="daily",
            written=not duplicate,
            duplicate=duplicate,
            content_hash=memory_content_hash(clean_text),
            threat_scan=scan_memory_text(clean_text),
            metadata={"entry_timestamp": ts.isoformat(), "bounded": True},
        )

    def append_long_term_facts(
        self,
        tenant_id: str,
        user_id: str,
        facts: list[str],
        *,
        now: datetime | None = None,
    ) -> str:
        """Append curated long-term facts to MEMORY.md."""
        return self.append_long_term_facts_result(
            tenant_id,
            user_id,
            facts,
            now=now,
        ).path

    def append_long_term_facts_result(
        self,
        tenant_id: str,
        user_id: str,
        facts: list[str],
        *,
        now: datetime | None = None,
    ) -> MemoryWriteResult:
        """Append curated long-term facts with dedup and atomic replacement."""
        if not facts:
            path = self._long_term_path(tenant_id, user_id)
            return MemoryWriteResult(
                path=str(path),
                source_type="long_term",
                written=False,
                duplicate=False,
                content_hash=memory_content_hash(""),
                threat_scan=scan_memory_text(""),
            )

        ts = now or datetime.now(timezone.utc)
        path = self._long_term_path(tenant_id, user_id)
        with self._path_lock(path):
            existing = self._read_existing(path) or "# Long-Term Memory\n"
            unique_facts: list[str] = []
            for fact in facts:
                clean_fact = bounded_memory_text(fact, max_chars=1000)
                if clean_fact and not self._contains_entry(existing, clean_fact):
                    unique_facts.append(clean_fact)
            duplicate = not unique_facts

            if unique_facts:
                block = f"\n## Update {ts.isoformat()}\n" + "".join(
                    f"- {fact}\n" for fact in unique_facts
                )
                self._atomic_write(path, existing + block)
        joined = "\n".join(unique_facts)
        return MemoryWriteResult(
            path=str(path),
            source_type="long_term",
            written=bool(unique_facts),
            duplicate=duplicate,
            content_hash=memory_content_hash(joined),
            threat_scan=scan_memory_text(joined),
            metadata={"fact_count": len(unique_facts), "entry_timestamp": ts.isoformat()},
        )

    def append_profile_facts(
        self,
        tenant_id: str,
        user_id: str,
        facts: list[str],
        *,
        now: datetime | None = None,
    ) -> MemoryWriteResult:
        """Append stable USER.md-equivalent profile facts."""
        ts = now or datetime.now(timezone.utc)
        path = self._profile_path(tenant_id, user_id)
        with self._path_lock(path):
            existing = self._read_existing(path) or "# User Profile Memory\n"
            unique_facts: list[str] = []
            for fact in facts:
                clean_fact = bounded_memory_text(fact, max_chars=1000)
                if clean_fact and not self._contains_entry(existing, clean_fact):
                    unique_facts.append(clean_fact)
            if unique_facts:
                block = f"\n## Profile Update {ts.isoformat()}\n" + "".join(
                    f"- {fact}\n" for fact in unique_facts
                )
                self._atomic_write(path, existing + block)
        joined = "\n".join(unique_facts)
        return MemoryWriteResult(
            path=str(path),
            source_type="profile",
            written=bool(unique_facts),
            duplicate=not bool(unique_facts),
            content_hash=memory_content_hash(joined),
            threat_scan=scan_memory_text(joined),
            metadata={"fact_count": len(unique_facts), "entry_timestamp": ts.isoformat()},
        )

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
        content = (
            f"# Reflection {target_day.isoformat()}\n\n"
            f"{bounded_memory_text(reflection)}\n"
        )
        with self._path_lock(path):
            self._atomic_write(path, content)
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
            profile_path = self._profile_path(tenant_id, user_id)
            if profile_path.exists():
                docs.append(self._to_document(profile_path, "profile"))

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

    def enumerate_workspace_sources(
        self,
        workspace_root: str | Path,
        *,
        extra_paths: list[str | Path] | None = None,
        max_files: int = 32,
    ) -> list[MemorySourceDocument]:
        """Enumerate workspace memory markdown sources without following symlinks."""
        root = Path(workspace_root).resolve()
        candidates: list[Path] = [
            root / "MEMORY.md",
            root / "memory.md",
        ]
        memory_dir = root / "memory"
        if memory_dir.exists() and memory_dir.is_dir() and not memory_dir.is_symlink():
            candidates.extend(memory_dir.rglob("*.md"))
        for item in extra_paths or []:
            path = Path(item)
            if not path.is_absolute():
                path = root / path
            candidates.append(path)

        docs: list[MemorySourceDocument] = []
        seen: set[Path] = set()
        for path in candidates:
            if len(docs) >= max(0, max_files):
                break
            try:
                resolved = path.resolve()
                resolved.relative_to(root)
            except (OSError, ValueError):
                continue
            if resolved in seen or resolved.is_symlink():
                continue
            if resolved.suffix.lower() != ".md" or not resolved.is_file():
                continue
            seen.add(resolved)
            docs.append(self._to_document(resolved, "workspace"))
        docs.sort(key=lambda d: d.path)
        return docs

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
            "source_types": sorted(
                {
                    self._source_type_for_path(Path(path))
                    for path in files
                }
            ),
        }

    def _source_type_for_path(self, path: Path) -> str:
        name = path.name
        if name == "MEMORY.md":
            return "long_term"
        if name == "USER.md":
            return "profile"
        if name.startswith("REFLECTION-"):
            return "reflection"
        return "daily"

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
