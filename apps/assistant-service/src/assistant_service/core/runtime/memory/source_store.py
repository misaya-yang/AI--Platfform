"""Markdown source-of-truth memory store."""

from __future__ import annotations

import fcntl
import hashlib
import os
import re
import secrets
import stat as stat_module
import threading
from collections import OrderedDict
from contextlib import contextmanager, suppress
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
    size_bytes: int = 0
    mtime_ns: int = 0
    ctime_ns: int = 0
    device: int = 0
    inode: int = 0


class MemorySourceSecurityError(RuntimeError):
    """Stable fail-closed error for unsafe memory filesystem state."""


class MemorySourceLimitError(RuntimeError):
    """Stable error for a source exceeding the configured byte ceiling."""


class MemorySourceDeletionInProgressError(RuntimeError):
    """Raised when a logical source is fenced by an unfinished deletion."""


class MemorySourceStore:
    """Persist assistant memory as markdown sources per tenant/user."""

    _SAFE_COMPONENT_RE = re.compile(r"[^a-zA-Z0-9_.-]+")
    _SOURCE_HANDLE_RE = re.compile(r"^memsrc_[0-9a-f]{32}$")
    _STAGED_SOURCE_RE = re.compile(
        r"^\.(?P<name>.+\.md)\.(?P<handle>memsrc_[0-9a-f]{32})\.deleting$"
    )
    _FINALIZING_SOURCE_RE = re.compile(
        r"^\.(?P<name>.+\.md)\.(?P<handle>memsrc_[0-9a-f]{32})\.finalizing$"
    )

    def __init__(
        self,
        base_dir: str | Path | None = None,
        *,
        legacy_base_dir: str | Path | None = None,
        max_source_bytes: int | None = None,
    ) -> None:
        default_dir = Path.home() / ".ai_gateway" / "assistant_memory"
        self.base_dir = Path(base_dir) if base_dir else default_dir
        configured_legacy = legacy_base_dir or os.getenv("ASSISTANT_RUNTIME_LEGACY_MEMORY_DIR")
        default_legacy_dir = Path.home() / ".ai_gateway" / "assistant_memory"
        self.legacy_base_dir = Path(configured_legacy) if configured_legacy else default_legacy_dir
        try:
            configured_limit = max_source_bytes or int(
                os.getenv(
                    "ASSISTANT_RUNTIME_MEMORY_MAX_SOURCE_BYTES",
                    str(4 * 1024 * 1024),
                )
            )
        except (TypeError, ValueError):
            configured_limit = 4 * 1024 * 1024
        self.max_source_bytes = max(64 * 1024, min(int(configured_limit), 64 * 1024 * 1024))
        self._lock_registry_guard = threading.Lock()
        self._path_locks: dict[str, threading.Lock] = {}
        self._document_cache_lock = threading.Lock()
        self._document_cache: OrderedDict[
            str,
            tuple[tuple[int, int, int, int, int], MemorySourceDocument],
        ] = OrderedDict()
        self._document_cache_max_entries = 64

    @classmethod
    def _safe_component(cls, value: str) -> str:
        raw = str(value or "").strip()
        cleaned = cls._SAFE_COMPONENT_RE.sub("_", raw)
        cleaned = cleaned.strip("._")
        prefix = (cleaned or "unknown")[:80]
        digest = hashlib.sha256(raw.encode()).hexdigest()
        # ``~`` is outside the accepted raw alphabet, reserving this namespace
        # for encoded values.  Without it, a crafted safe raw value could equal
        # the encoded form of an unsafe value and regain the original collision.
        return f"~{prefix}-{digest}"

    @classmethod
    def _legacy_safe_component(cls, value: str) -> str:
        cleaned = cls._SAFE_COMPONENT_RE.sub("_", str(value or "").strip())
        return cleaned.strip("._") or "unknown"

    def _user_root(self, tenant_id: str, user_id: str) -> Path:
        return self.base_dir / self._safe_component(tenant_id) / self._safe_component(user_id)

    def _legacy_user_root(self, tenant_id: str, user_id: str) -> Path:
        return (
            self.legacy_base_dir
            / self._legacy_safe_component(tenant_id)
            / self._legacy_safe_component(user_id)
        )

    def _legacy_candidate_from_record(
        self,
        tenant_id: str,
        user_id: str,
        source_path: str,
    ) -> Path | None:
        """Map one old persisted path onto the configured legacy alias.

        Compose may expose the old volume at a new mount point, so an exact
        absolute prefix is not always available.  In that case accept only one
        unambiguous ``<legacy tenant>/<legacy user>/<relative path>`` suffix.
        """

        legacy_root = self._lexical_path(self._legacy_user_root(tenant_id, user_id))
        stored = self._lexical_path(source_path)
        try:
            relative = stored.relative_to(legacy_root)
        except ValueError:
            tenant_component = self._legacy_safe_component(tenant_id)
            user_component = self._legacy_safe_component(user_id)
            parts = stored.parts
            matches = [
                index
                for index in range(max(0, len(parts) - 1))
                if parts[index : index + 2] == (tenant_component, user_component)
            ]
            if len(matches) != 1:
                return None
            relative_parts = parts[matches[0] + 2 :]
            if not relative_parts:
                return None
            relative = Path(*relative_parts)
        if not relative.parts or any(part in {"", ".", ".."} for part in relative.parts):
            return None
        try:
            target = self._validate_path_below(legacy_root, legacy_root / relative)
        except MemorySourceSecurityError:
            return None
        if target.suffix.lower() != ".md":
            return None
        return target

    def resolve_legacy_owned_source(
        self,
        tenant_id: str,
        user_id: str,
        source_path: str,
        *,
        owner_proven: bool,
    ) -> Path | None:
        """Resolve an old-layout source only with an explicit SQL owner proof."""

        if owner_proven is not True:
            return None
        return self._legacy_candidate_from_record(tenant_id, user_id, source_path)

    def _resolve_source_target(
        self,
        tenant_id: str,
        user_id: str,
        source_path: str,
        *,
        legacy_owner_proven: bool,
    ) -> Path | None:
        if legacy_owner_proven:
            return self.resolve_legacy_owned_source(
                tenant_id,
                user_id,
                source_path,
                owner_proven=True,
            )
        return self.resolve_owned_source(tenant_id, user_id, source_path)

    @classmethod
    def _assert_safe_directory_chain(
        cls,
        path: Path,
        *,
        create: bool,
    ) -> None:
        """Reject symlinks from the filesystem anchor through ``path``.

        ``Path.mkdir(parents=True)`` follows an existing symlink in any parent
        component.  Runtime memory roots are security boundaries, so walk and
        validate every component before creating the next one.
        """

        target = cls._lexical_path(path)
        current = Path(target.anchor)
        for component in target.parts[1:]:
            current = current / component
            created = False
            try:
                current_stat = os.lstat(current)
            except FileNotFoundError:
                if not create:
                    break
                try:
                    os.mkdir(current, 0o700)
                    created = True
                except FileExistsError:
                    # Another process won the creation race; validate what it
                    # installed instead of following it.
                    pass
                current_stat = os.lstat(current)
            if stat_module.S_ISLNK(current_stat.st_mode):
                raise MemorySourceSecurityError("memory_directory_unsafe")
            if not stat_module.S_ISDIR(current_stat.st_mode):
                raise MemorySourceSecurityError("memory_directory_unsafe")
            if created:
                current.chmod(0o700)

    def _ensure_private_directory(self, path: Path) -> None:
        base = Path(os.path.abspath(self.base_dir))
        target = Path(os.path.abspath(path))
        if target == base:
            self._assert_safe_directory_chain(base, create=True)
            base.chmod(0o700)
            return

        try:
            relative = target.relative_to(base)
        except ValueError as exc:
            raise MemorySourceSecurityError("memory_directory_out_of_scope") from exc
        self._assert_safe_directory_chain(base, create=True)
        base.chmod(0o700)
        current = base
        for component in relative.parts:
            current = current / component
            try:
                current_stat = os.lstat(current)
            except FileNotFoundError:
                with suppress(FileExistsError):
                    os.mkdir(current, 0o700)
                current_stat = os.lstat(current)
            if stat_module.S_ISLNK(current_stat.st_mode) or not stat_module.S_ISDIR(
                current_stat.st_mode
            ):
                raise MemorySourceSecurityError("memory_directory_unsafe")
            current.chmod(0o700)

    @staticmethod
    def _lexical_path(path: str | Path) -> Path:
        return Path(os.path.abspath(path))

    def _validate_path_below(self, root: Path, path: str | Path) -> Path:
        root_path = self._lexical_path(root)
        target = self._lexical_path(path)
        try:
            relative = target.relative_to(root_path)
        except ValueError as exc:
            raise MemorySourceSecurityError("memory_source_out_of_scope") from exc
        self._assert_safe_directory_chain(root_path, create=False)
        current = root_path
        for component in relative.parts:
            current = current / component
            try:
                current_stat = os.lstat(current)
            except FileNotFoundError:
                break
            if stat_module.S_ISLNK(current_stat.st_mode):
                raise MemorySourceSecurityError("memory_source_symlink_rejected")
        return target

    def _ensure_user_dirs(self, tenant_id: str, user_id: str) -> tuple[Path, Path]:
        self._ensure_private_directory(self.base_dir)
        root = self._user_root(tenant_id, user_id)
        memory_dir = root / "memory"
        self._ensure_private_directory(root.parent)
        self._ensure_private_directory(root)
        self._ensure_private_directory(memory_dir)
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

    def _atomic_write(self, path: Path, content: str) -> None:
        encoded = content.encode()
        if len(encoded) > self.max_source_bytes:
            raise MemorySourceLimitError("memory_source_size_limit_exceeded")
        self._ensure_private_directory(path.parent)
        if path.is_symlink():
            raise MemorySourceSecurityError("memory_source_symlink_rejected")
        pending_markers = [
            *path.parent.glob(f".{path.name}.memsrc_*.deleting"),
            *path.parent.glob(f".{path.name}.memsrc_*.finalizing"),
        ]
        if any(os.path.lexists(marker) for marker in pending_markers):
            raise MemorySourceDeletionInProgressError("memory_source_deletion_pending")

        tmp_path: Path | None = None
        descriptor: int | None = None
        try:
            for _ in range(16):
                candidate = path.with_name(f".{path.name}.{secrets.token_hex(12)}.tmp")
                flags = os.O_WRONLY | os.O_CREAT | os.O_EXCL
                if hasattr(os, "O_NOFOLLOW"):
                    flags |= os.O_NOFOLLOW
                try:
                    descriptor = os.open(candidate, flags, 0o600)
                except FileExistsError:
                    continue
                tmp_path = candidate
                break
            if descriptor is None or tmp_path is None:
                raise MemorySourceSecurityError("memory_tempfile_allocation_failed")
            os.fchmod(descriptor, 0o600)
            with os.fdopen(descriptor, "wb", closefd=True) as stream:
                descriptor = None
                stream.write(encoded)
                stream.flush()
                os.fsync(stream.fileno())
            os.replace(tmp_path, path)
            path.chmod(0o600)
        finally:
            if descriptor is not None:
                os.close(descriptor)
            if tmp_path is not None and tmp_path.exists():
                tmp_path.unlink()

    def _path_lock(self, path: Path) -> threading.Lock:
        key = str(self._lexical_path(path))
        with self._lock_registry_guard:
            lock = self._path_locks.get(key)
            if lock is None:
                lock = threading.Lock()
                self._path_locks[key] = lock
            return lock

    def _ensure_lock_directory(self, parent: Path) -> Path:
        parent_path = self._lexical_path(parent)
        current_base = self._lexical_path(self.base_dir)
        legacy_base = self._lexical_path(self.legacy_base_dir)
        if parent_path == current_base or current_base in parent_path.parents:
            self._ensure_private_directory(parent_path)
        elif parent_path == legacy_base or legacy_base in parent_path.parents:
            self._validate_path_below(legacy_base, parent_path)
            try:
                parent_stat = os.lstat(parent_path)
            except FileNotFoundError as exc:
                raise MemorySourceSecurityError("memory_directory_unsafe") from exc
            if stat_module.S_ISLNK(parent_stat.st_mode) or not stat_module.S_ISDIR(
                parent_stat.st_mode
            ):
                raise MemorySourceSecurityError("memory_directory_unsafe")
        else:
            raise MemorySourceSecurityError("memory_directory_out_of_scope")

        lock_dir = parent_path / ".locks"
        with suppress(FileExistsError):
            os.mkdir(lock_dir, 0o700)
        lock_stat = os.lstat(lock_dir)
        if stat_module.S_ISLNK(lock_stat.st_mode) or not stat_module.S_ISDIR(lock_stat.st_mode):
            raise MemorySourceSecurityError("memory_directory_unsafe")
        lock_dir.chmod(0o700)
        return lock_dir

    @contextmanager
    def _exclusive_path_lock(self, path: Path):
        thread_lock = self._path_lock(path)
        with thread_lock:
            lock_dir = self._ensure_lock_directory(path.parent)
            lock_name = hashlib.sha256(str(self._lexical_path(path)).encode()).hexdigest()
            lock_path = lock_dir / f"{lock_name}.lock"
            flags = os.O_RDWR | os.O_CREAT
            if hasattr(os, "O_NOFOLLOW"):
                flags |= os.O_NOFOLLOW
            descriptor = os.open(lock_path, flags, 0o600)
            try:
                os.fchmod(descriptor, 0o600)
                fcntl.flock(descriptor, fcntl.LOCK_EX)
                yield
            finally:
                fcntl.flock(descriptor, fcntl.LOCK_UN)
                os.close(descriptor)

    def _read_source_snapshot(self, path: Path) -> tuple[bytes, os.stat_result]:
        """Read one regular-file generation through a no-follow descriptor."""

        flags = os.O_RDONLY
        if hasattr(os, "O_CLOEXEC"):
            flags |= os.O_CLOEXEC
        if hasattr(os, "O_NOFOLLOW"):
            flags |= os.O_NOFOLLOW
        try:
            descriptor = os.open(path, flags)
        except FileNotFoundError:
            raise
        except OSError as exc:
            raise MemorySourceSecurityError("memory_source_unsafe") from exc
        try:
            before = os.fstat(descriptor)
            if not stat_module.S_ISREG(before.st_mode):
                raise MemorySourceSecurityError("memory_source_unsafe")
            if before.st_size > self.max_source_bytes:
                raise MemorySourceLimitError("memory_source_size_limit_exceeded")
            blocks: list[bytes] = []
            read_bytes = 0
            while True:
                block = os.read(
                    descriptor,
                    min(64 * 1024, self.max_source_bytes + 1 - read_bytes),
                )
                if not block:
                    break
                read_bytes += len(block)
                if read_bytes > self.max_source_bytes:
                    raise MemorySourceLimitError("memory_source_size_limit_exceeded")
                blocks.append(block)
            after = os.fstat(descriptor)
            identity_before = (
                before.st_dev,
                before.st_ino,
                before.st_size,
                before.st_mtime_ns,
                before.st_ctime_ns,
            )
            identity_after = (
                after.st_dev,
                after.st_ino,
                after.st_size,
                after.st_mtime_ns,
                after.st_ctime_ns,
            )
            if identity_before != identity_after:
                raise MemorySourceSecurityError("memory_source_changed_during_read")
            return b"".join(blocks), after
        finally:
            os.close(descriptor)

    def _read_existing(self, path: Path) -> str:
        try:
            content, _ = self._read_source_snapshot(path)
        except FileNotFoundError:
            return ""
        return content.decode("utf-8", errors="ignore")

    @staticmethod
    def _contains_entry(existing: str, entry: str) -> bool:
        normalized_existing = "\n".join(
            line.strip() for line in existing.splitlines() if line.strip()
        )
        normalized_entry = "\n".join(line.strip() for line in entry.splitlines() if line.strip())
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
        with self._exclusive_path_lock(path):
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
        with self._exclusive_path_lock(path):
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
        with self._exclusive_path_lock(path):
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
        content = f"# Reflection {target_day.isoformat()}\n\n{bounded_memory_text(reflection)}\n"
        with self._exclusive_path_lock(path):
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
            except (MemorySourceSecurityError, OSError, ValueError):
                continue
            if resolved in seen or resolved.is_symlink():
                continue
            if resolved.suffix.lower() != ".md" or not resolved.is_file():
                continue
            seen.add(resolved)
            docs.append(self._to_document(resolved, "workspace"))
        docs.sort(key=lambda d: d.path)
        return docs

    def _to_document(self, path: Path, source_type: str) -> MemorySourceDocument:
        try:
            observed = os.lstat(path)
        except OSError as exc:
            raise MemorySourceSecurityError("memory_source_unsafe") from exc
        if stat_module.S_ISLNK(observed.st_mode) or not stat_module.S_ISREG(
            observed.st_mode
        ):
            raise MemorySourceSecurityError("memory_source_unsafe")
        if observed.st_size > self.max_source_bytes:
            raise MemorySourceLimitError("memory_source_size_limit_exceeded")
        cache_key = str(self._lexical_path(path))
        observed_identity = (
            observed.st_dev,
            observed.st_ino,
            observed.st_size,
            observed.st_mtime_ns,
            observed.st_ctime_ns,
        )
        with self._document_cache_lock:
            cached = self._document_cache.get(cache_key)
            if cached is not None and cached[0] == observed_identity:
                self._document_cache.move_to_end(cache_key)
                return cached[1]

        content_bytes, source_stat = self._read_source_snapshot(path)
        updated_at = datetime.fromtimestamp(source_stat.st_mtime, tz=timezone.utc)
        content = content_bytes.decode("utf-8", errors="ignore")
        document = MemorySourceDocument(
            path=str(path),
            source_type=source_type,
            content=content,
            updated_at=updated_at,
            size_bytes=source_stat.st_size,
            mtime_ns=source_stat.st_mtime_ns,
            ctime_ns=source_stat.st_ctime_ns,
            device=source_stat.st_dev,
            inode=source_stat.st_ino,
        )
        actual_identity = (
            source_stat.st_dev,
            source_stat.st_ino,
            source_stat.st_size,
            source_stat.st_mtime_ns,
            source_stat.st_ctime_ns,
        )
        with self._document_cache_lock:
            self._document_cache[cache_key] = (actual_identity, document)
            self._document_cache.move_to_end(cache_key)
            while len(self._document_cache) > self._document_cache_max_entries:
                self._document_cache.popitem(last=False)
        return document

    def read_owned_source_document(
        self,
        tenant_id: str,
        user_id: str,
        source_path: str,
        *,
        source_type: str,
    ) -> tuple[MemorySourceDocument, str]:
        """Read content, trusted mtime, and generation under the file lock."""

        target = self.resolve_owned_source(tenant_id, user_id, source_path)
        if target is None:
            raise MemorySourceSecurityError("memory_source_out_of_scope")
        with self._exclusive_path_lock(target):
            content_bytes, source_stat = self._read_source_snapshot(target)
            document = MemorySourceDocument(
                path=str(target),
                source_type=source_type,
                content=content_bytes.decode("utf-8", errors="ignore"),
                updated_at=datetime.fromtimestamp(source_stat.st_mtime, tz=timezone.utc),
            )
            root = self._lexical_path(self._user_root(tenant_id, user_id))
            relative = target.relative_to(root).as_posix()
            handle = self._source_handle(
                relative,
                self._source_generation_from_snapshot(content_bytes, source_stat),
            )
            return document, handle

    def read_legacy_source_document(
        self,
        tenant_id: str,
        user_id: str,
        source_path: str,
        *,
        source_type: str,
        owner_proven: bool,
    ) -> tuple[MemorySourceDocument, str]:
        """Read an owner-proven legacy source under its cross-process lock."""

        target = self.resolve_legacy_owned_source(
            tenant_id,
            user_id,
            source_path,
            owner_proven=owner_proven,
        )
        if target is None:
            raise MemorySourceSecurityError("legacy_memory_source_owner_unproven")
        with self._exclusive_path_lock(target):
            content_bytes, source_stat = self._read_source_snapshot(target)
            legacy_root = self._lexical_path(self._legacy_user_root(tenant_id, user_id))
            relative = target.relative_to(legacy_root).as_posix()
            document = MemorySourceDocument(
                path=str(target),
                source_type=source_type,
                content=content_bytes.decode("utf-8", errors="ignore"),
                updated_at=datetime.fromtimestamp(source_stat.st_mtime, tz=timezone.utc),
            )
            handle = self._source_handle(
                f"legacy/{relative}",
                self._source_generation_from_snapshot(content_bytes, source_stat),
            )
            return document, handle

    def inspect_user_tree(self, tenant_id: str, user_id: str) -> dict[str, Any]:
        """Return host-path-free source handles for inspection/deletion."""
        sources = self._owned_source_inventory(tenant_id, user_id)
        return {
            "tenant_id": tenant_id,
            "user_id": user_id,
            "scope": "tenant_user",
            "file_count": len(sources),
            "files": [source["label"] for source in sources],
            "sources": [
                {key: value for key, value in source.items() if not key.startswith("_")}
                for source in sources
            ],
            "source_types": sorted({str(source["source_type"]) for source in sources}),
        }

    def inspect_legacy_records(
        self,
        tenant_id: str,
        user_id: str,
        records: list[dict[str, Any]],
    ) -> tuple[list[dict[str, Any]], int]:
        """Return safe legacy handles plus a count of quarantined records."""

        inventory: list[dict[str, Any]] = []
        quarantined = 0
        seen_targets: set[Path] = set()
        legacy_root = self._lexical_path(self._legacy_user_root(tenant_id, user_id))
        for record in records:
            raw_source_path = str(record.get("source_path") or "")
            candidate = self._legacy_candidate_from_record(
                tenant_id,
                user_id,
                raw_source_path,
            )
            if candidate is None:
                continue
            if record.get("owner_proven") is not True:
                quarantined += 1
                continue
            if candidate in seen_targets:
                quarantined += 1
                continue
            seen_targets.add(candidate)
            staged_candidates = [
                *candidate.parent.glob(f".{candidate.name}.memsrc_*.deleting"),
                *candidate.parent.glob(f".{candidate.name}.memsrc_*.finalizing"),
            ]
            safe_markers = [
                item
                for item in staged_candidates
                if item.is_file()
                and not item.is_symlink()
                and (
                    self._STAGED_SOURCE_RE.fullmatch(item.name)
                    or self._FINALIZING_SOURCE_RE.fullmatch(item.name)
                )
            ]
            active = candidate.exists() and candidate.is_file() and not candidate.is_symlink()
            if len(safe_markers) > 1 or (active and safe_markers):
                quarantined += 1
                continue
            source_type = str(record.get("source_type") or self._source_type_for_path(candidate))
            if safe_markers:
                marker = safe_markers[0]
                match = self._STAGED_SOURCE_RE.fullmatch(
                    marker.name
                ) or self._FINALIZING_SOURCE_RE.fullmatch(marker.name)
                if match is None:
                    quarantined += 1
                    continue
                inventory.append(
                    {
                        "source_id": match.group("handle"),
                        "label": candidate.name,
                        "source_type": source_type,
                        "status": "deletion_pending",
                        "legacy": True,
                        "_path": str(candidate),
                        "_index_source_path": raw_source_path,
                        "_deletion_stage": (
                            "staged"
                            if self._STAGED_SOURCE_RE.fullmatch(marker.name)
                            else "finalizing"
                        ),
                    }
                )
                continue
            if not active:
                continue
            try:
                relative = candidate.relative_to(legacy_root).as_posix()
                source_handle = self._source_handle(
                    f"legacy/{relative}",
                    self._source_generation(candidate),
                )
            except (MemorySourceLimitError, MemorySourceSecurityError, OSError, ValueError):
                quarantined += 1
                continue
            inventory.append(
                {
                    "source_id": source_handle,
                    "label": candidate.name,
                    "source_type": source_type,
                    "status": "active",
                    "legacy": True,
                    "_path": str(candidate),
                    "_index_source_path": raw_source_path,
                }
            )
        inventory.sort(key=lambda item: (str(item["label"]), str(item["source_id"])))
        return inventory, quarantined

    def _source_generation(self, path: Path) -> str:
        """Bind a handle to one concrete file generation."""

        content, source_stat = self._read_source_snapshot(path)
        return self._source_generation_from_snapshot(content, source_stat)

    @staticmethod
    def _source_generation_from_snapshot(
        content: bytes,
        source_stat: os.stat_result,
    ) -> str:
        identity = (
            source_stat.st_dev,
            source_stat.st_ino,
            source_stat.st_size,
            source_stat.st_mtime_ns,
            source_stat.st_ctime_ns,
        )
        return ":".join(str(item) for item in identity) + (
            f":{hashlib.sha256(content).hexdigest()}"
        )

    @staticmethod
    def _source_handle(relative_path: str, generation: str) -> str:
        material = f"{relative_path}\0{generation}".encode()
        digest = hashlib.sha256(material).hexdigest()[:32]
        return f"memsrc_{digest}"

    def _owned_source_inventory(
        self,
        tenant_id: str,
        user_id: str,
    ) -> list[dict[str, Any]]:
        root = self._lexical_path(self._user_root(tenant_id, user_id))
        inventory: list[dict[str, Any]] = []
        for raw_path in self.list_markdown_sources(tenant_id, user_id):
            path = Path(raw_path)
            if path.is_symlink():
                continue
            try:
                resolved = self._validate_path_below(root, path)
                relative = resolved.relative_to(root).as_posix()
            except (OSError, ValueError):
                continue
            if not resolved.is_file():
                continue
            try:
                source_handle = self._source_handle(
                    relative,
                    self._source_generation(resolved),
                )
            except OSError:
                continue
            inventory.append(
                {
                    "source_id": source_handle,
                    "label": resolved.name,
                    "source_type": self._source_type_for_path(resolved),
                    "status": "active",
                    "_path": str(resolved),
                }
            )
        if root.exists() and not root.is_symlink():
            for staged in [*root.rglob("*.deleting"), *root.rglob("*.finalizing")]:
                match = self._STAGED_SOURCE_RE.fullmatch(
                    staged.name
                ) or self._FINALIZING_SOURCE_RE.fullmatch(staged.name)
                if not match or staged.is_symlink() or not staged.is_file():
                    continue
                try:
                    self._validate_path_below(root, staged)
                except (MemorySourceSecurityError, OSError, ValueError):
                    continue
                original = staged.with_name(match.group("name"))
                inventory.append(
                    {
                        "source_id": match.group("handle"),
                        "label": original.name,
                        "source_type": self._source_type_for_path(original),
                        "status": "deletion_pending",
                        "_path": str(original),
                        "_staged_path": str(staged),
                        "_deletion_stage": (
                            "staged"
                            if self._STAGED_SOURCE_RE.fullmatch(staged.name)
                            else "finalizing"
                        ),
                    }
                )
        inventory.sort(key=lambda item: (str(item["label"]), str(item["source_id"])))
        return inventory

    @staticmethod
    def _staged_source_path(target: Path, source_handle: str) -> Path:
        return target.with_name(f".{target.name}.{source_handle}.deleting")

    @staticmethod
    def _finalizing_source_path(target: Path, source_handle: str) -> Path:
        return target.with_name(f".{target.name}.{source_handle}.finalizing")

    def stage_source_for_deletion(
        self,
        tenant_id: str,
        user_id: str,
        source_path: str,
        *,
        expected_source_handle: str,
        legacy_owner_proven: bool = False,
    ) -> tuple[str, Path | None]:
        """Atomically move one exact generation out of the active ``*.md`` set."""

        target = self._resolve_source_target(
            tenant_id,
            user_id,
            source_path,
            legacy_owner_proven=legacy_owner_proven,
        )
        if target is None or not self.is_source_handle(expected_source_handle):
            return "out_of_scope", None
        staged = self._staged_source_path(target, expected_source_handle)
        finalizing = self._finalizing_source_path(target, expected_source_handle)
        with self._exclusive_path_lock(target):
            if finalizing.exists():
                if finalizing.is_symlink() or not finalizing.is_file():
                    return "unsafe", None
                return "finalizing", finalizing
            if staged.exists():
                if staged.is_symlink() or not staged.is_file():
                    return "unsafe", None
                return "staged", staged
            other_staged = [
                item
                for item in [
                    *target.parent.glob(f".{target.name}.memsrc_*.deleting"),
                    *target.parent.glob(f".{target.name}.memsrc_*.finalizing"),
                ]
                if item not in {staged, finalizing} and item.is_file() and not item.is_symlink()
            ]
            if other_staged:
                return "deletion_in_progress", None
            if not target.exists() or not target.is_file() or target.is_symlink():
                return "absent", None
            current_handle = self.source_handle_for_path(
                tenant_id,
                user_id,
                str(target),
                legacy_owner_proven=legacy_owner_proven,
            )
            if current_handle != expected_source_handle:
                return "generation_conflict", None
            os.replace(target, staged)
            staged.chmod(0o600)
            return "staged", staged

    def restore_staged_source(
        self,
        tenant_id: str,
        user_id: str,
        source_path: str,
        *,
        expected_source_handle: str,
        legacy_owner_proven: bool = False,
    ) -> str:
        """Restore an exact staged generation after a pre-delete fence conflict."""

        target = self._resolve_source_target(
            tenant_id,
            user_id,
            source_path,
            legacy_owner_proven=legacy_owner_proven,
        )
        if target is None or not self.is_source_handle(expected_source_handle):
            return "out_of_scope"
        staged = self._staged_source_path(target, expected_source_handle)
        finalizing = self._finalizing_source_path(target, expected_source_handle)
        with self._exclusive_path_lock(target):
            if os.path.lexists(finalizing):
                if finalizing.is_symlink() or not finalizing.is_file():
                    return "unsafe"
                return "finalizing"
            if os.path.lexists(target):
                return "target_present"
            if not os.path.lexists(staged):
                return "absent"
            if staged.is_symlink() or not staged.is_file():
                return "unsafe"
            os.replace(staged, target)
            target.chmod(0o600)
            self._fsync_directory(target.parent)
            if (
                os.path.lexists(staged)
                or os.path.lexists(finalizing)
                or target.is_symlink()
                or not target.is_file()
            ):
                return "restore_unverified"
            return "restored"

    def delete_staged_source(
        self,
        tenant_id: str,
        user_id: str,
        source_path: str,
        *,
        expected_source_handle: str,
        legacy_owner_proven: bool = False,
    ) -> str:
        """Unlink only the previously staged generation."""

        target = self._resolve_source_target(
            tenant_id,
            user_id,
            source_path,
            legacy_owner_proven=legacy_owner_proven,
        )
        if target is None:
            return "out_of_scope"
        staged = self._staged_source_path(target, expected_source_handle)
        finalizing = self._finalizing_source_path(target, expected_source_handle)
        with self._exclusive_path_lock(target):
            if finalizing.exists():
                if finalizing.is_symlink() or not finalizing.is_file():
                    return "unsafe"
                if staged.exists():
                    if staged.is_symlink() or not staged.is_file():
                        return "unsafe"
                    staged.unlink()
                    self._fsync_directory(target.parent)
                    return "deleted"
                return "absent"
            if not staged.exists():
                return "absent"
            if staged.is_symlink() or not staged.is_file():
                return "unsafe"
            flags = os.O_WRONLY | os.O_CREAT | os.O_EXCL
            if hasattr(os, "O_NOFOLLOW"):
                flags |= os.O_NOFOLLOW
            descriptor = os.open(finalizing, flags, 0o600)
            try:
                os.fchmod(descriptor, 0o600)
                os.fsync(descriptor)
            finally:
                os.close(descriptor)
            self._fsync_directory(target.parent)
            staged.unlink()
            self._fsync_directory(target.parent)
            return "deleted"

    @staticmethod
    def _fsync_directory(path: Path) -> None:
        flags = os.O_RDONLY
        if hasattr(os, "O_DIRECTORY"):
            flags |= os.O_DIRECTORY
        descriptor = os.open(path, flags)
        try:
            os.fsync(descriptor)
        finally:
            os.close(descriptor)

    def clear_deletion_marker(
        self,
        tenant_id: str,
        user_id: str,
        source_path: str,
        *,
        source_handle: str,
        legacy_owner_proven: bool = False,
    ) -> None:
        target = self._resolve_source_target(
            tenant_id,
            user_id,
            source_path,
            legacy_owner_proven=legacy_owner_proven,
        )
        if target is None:
            return
        marker = self._finalizing_source_path(target, source_handle)
        with self._exclusive_path_lock(target):
            if marker.exists() and marker.is_file() and not marker.is_symlink():
                marker.unlink()

    def staged_source_exists(
        self,
        tenant_id: str,
        user_id: str,
        source_path: str,
        *,
        source_handle: str,
        legacy_owner_proven: bool = False,
    ) -> bool:
        target = self._resolve_source_target(
            tenant_id,
            user_id,
            source_path,
            legacy_owner_proven=legacy_owner_proven,
        )
        if target is None:
            return False
        staged = self._staged_source_path(target, source_handle)
        return staged.exists() and staged.is_file() and not staged.is_symlink()

    def deletion_marker_exists(
        self,
        tenant_id: str,
        user_id: str,
        source_path: str,
        *,
        source_handle: str,
        legacy_owner_proven: bool = False,
    ) -> bool:
        target = self._resolve_source_target(
            tenant_id,
            user_id,
            source_path,
            legacy_owner_proven=legacy_owner_proven,
        )
        if target is None:
            return False
        marker = self._finalizing_source_path(target, source_handle)
        return marker.exists() and marker.is_file() and not marker.is_symlink()

    def resolve_source_handle(
        self,
        tenant_id: str,
        user_id: str,
        source_id: str,
    ) -> Path | None:
        """Resolve an opaque source handle only inside its active owner scope."""

        record = self.resolve_source_handle_record(tenant_id, user_id, source_id)
        return Path(str(record["_path"])) if record else None

    def resolve_source_handle_record(
        self,
        tenant_id: str,
        user_id: str,
        source_id: str,
    ) -> dict[str, Any] | None:
        """Resolve an active/staged handle and preserve its local state."""

        requested = str(source_id or "").strip()
        if not self.is_source_handle(requested):
            return None
        for item in self._owned_source_inventory(tenant_id, user_id):
            if item["source_id"] == requested:
                return dict(item)
        return None

    def resolve_legacy_source_handle_record(
        self,
        tenant_id: str,
        user_id: str,
        source_id: str,
        records: list[dict[str, Any]],
    ) -> dict[str, Any] | None:
        """Resolve a handle from owner-proven legacy records only."""

        requested = str(source_id or "").strip()
        if not self.is_source_handle(requested):
            return None
        inventory, _ = self.inspect_legacy_records(tenant_id, user_id, records)
        for item in inventory:
            if item["source_id"] == requested:
                return dict(item)
        return None

    @classmethod
    def is_source_handle(cls, source_id: str) -> bool:
        """Return whether a value has the opaque runtime-source handle shape."""

        return bool(cls._SOURCE_HANDLE_RE.fullmatch(str(source_id or "").strip()))

    def source_handle_for_path(
        self,
        tenant_id: str,
        user_id: str,
        source_path: str,
        *,
        legacy_owner_proven: bool = False,
    ) -> str | None:
        """Build the stable scoped handle for an owned path, present or absent."""

        target = self._resolve_source_target(
            tenant_id,
            user_id,
            source_path,
            legacy_owner_proven=legacy_owner_proven,
        )
        if target is None:
            return None
        root = self._lexical_path(
            self._legacy_user_root(tenant_id, user_id)
            if legacy_owner_proven
            else self._user_root(tenant_id, user_id)
        )
        try:
            relative = target.relative_to(root).as_posix()
        except ValueError:
            return None
        if not target.exists() or not target.is_file() or target.is_symlink():
            return None
        try:
            generation = self._source_generation(target)
        except OSError:
            return None
        identity_path = f"legacy/{relative}" if legacy_owner_proven else relative
        return self._source_handle(identity_path, generation)

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
        return (
            self.delete_source_if_generation(
                tenant_id,
                user_id,
                source_path,
                expected_source_handle=None,
            )
            == "deleted"
        )

    def delete_source_if_generation(
        self,
        tenant_id: str,
        user_id: str,
        source_path: str,
        *,
        expected_source_handle: str | None,
    ) -> str:
        """Atomically revalidate a generation under a cross-process file lock."""

        target = self.resolve_owned_source(tenant_id, user_id, source_path)
        if target is None:
            return "out_of_scope"

        if not target.exists() or not target.is_file():
            return "absent"

        with self._exclusive_path_lock(target):
            if not target.exists() or not target.is_file():
                return "absent"
            if target.is_symlink():
                return "unsafe"
            current_handle = self.source_handle_for_path(
                tenant_id,
                user_id,
                str(target),
            )
            if expected_source_handle and current_handle != expected_source_handle:
                return "generation_conflict"
            target.unlink()
        return "deleted"

    def resolve_owned_source(
        self,
        tenant_id: str,
        user_id: str,
        source_path: str,
    ) -> Path | None:
        """Resolve a markdown source only within the requested scope.

        The target may be absent so callers can perform idempotent deletion and
        still clean derived SQL/vector records for the exact internal path.
        """

        try:
            target = self._validate_path_below(
                self._user_root(tenant_id, user_id),
                source_path,
            )
        except MemorySourceSecurityError:
            return None
        if target.suffix.lower() != ".md":
            return None
        return target
