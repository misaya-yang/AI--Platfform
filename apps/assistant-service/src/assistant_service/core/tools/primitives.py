"""
General-agent primitive filesystem tools.

Exposes `fs_read`, `fs_write`, `fs_glob`, `fs_grep` — the minimum kit a
model-driven agent needs for longer, file-based tasks. All tools operate
inside a tenant-scoped workspace (see `workspace.py`). Paths that escape the
workspace are rejected before any I/O.

Tools register only when `ASSISTANT_ENABLE_PRIMITIVES=true` at startup.
`bash` and `web_fetch` are intentionally not shipped here — bash needs the
docker sandbox in `code_executor`, web_fetch needs an SSRF allowlist.
"""

from __future__ import annotations

import asyncio
import re
import time
from pathlib import Path

from ai_gateway_core.logging import get_logger

from .tool_registry import (
    ToolCallRequest,
    ToolCallResult,
    ToolCategory,
    ToolDefinition,
    ToolExecutor,
    ToolParameter,
    ToolRiskLevel,
    register_tool,
)
from .workspace import WorkspaceEscapeError, resolve_under, tenant_workspace

logger = get_logger(__name__)


# Per-call content caps. Tuned conservatively; tools return truncation markers
# when exceeded so the model can issue a follow-up with offset/limit.
_MAX_READ_BYTES = 200_000
_MAX_WRITE_BYTES = 500_000
_MAX_GLOB_RESULTS = 500
_MAX_GREP_MATCHES = 200
_MAX_GREP_BYTES_PER_FILE = 2_000_000  # skip files larger than this
_GREP_TIMEOUT_SECONDS = 5.0

# Heuristic block for catastrophic-backtracking regexes. Not a full ReDoS
# detector — a real solution is running the regex in a subprocess or using
# RE2 — but cheap and catches the common foot-guns the model might emit.
_REDOS_HEURISTIC = re.compile(r"(\(.+[+*]\)[+*])|(\([^)]*\|[^)]*\)[+*])")


def _workspace_for(request: ToolCallRequest) -> Path:
    """Extract tenant_id + session_id from the request metadata and return
    the tenant workspace. Raises with a clear message if missing."""
    tenant_id = str(request.metadata.get("tenant_id") or "").strip()
    session_id = str(request.metadata.get("session_id") or "").strip()
    if not tenant_id or not session_id:
        raise WorkspaceEscapeError(
            "tenant_id and session_id are required for primitive tools"
        )
    return tenant_workspace(tenant_id, session_id)


# ---------------------------------------------------------------------------
# fs_read
# ---------------------------------------------------------------------------

FS_READ_DEFINITION = ToolDefinition(
    name="fs_read",
    description=(
        "Read a text file from the current workspace. Returns the content "
        "plus line count. Use `offset` and `limit` for partial reads of "
        "large files."
    ),
    parameters=[
        ToolParameter(
            name="path",
            type="string",
            description="Workspace-relative path (e.g. 'notes/todo.md').",
            required=True,
        ),
        ToolParameter(
            name="offset",
            type="number",
            description="0-based start line. Default 0.",
            required=False,
            default=0,
        ),
        ToolParameter(
            name="limit",
            type="number",
            description="Max number of lines to return. Default: all.",
            required=False,
        ),
    ],
    category=ToolCategory.UTILITY,
    risk_level=ToolRiskLevel.LOW,
    when_to_use="Read a previously-written file or a file the user placed in the workspace.",
)


class FsReadExecutor(ToolExecutor):
    async def execute(self, request: ToolCallRequest) -> ToolCallResult:
        start = time.time()
        try:
            root = _workspace_for(request)
            rel_path = str(request.arguments.get("path") or "")
            target = resolve_under(root, rel_path)
            if not target.exists():
                return _err(request, f"file not found: {rel_path}", start)
            if target.is_dir():
                return _err(request, f"path is a directory, not a file: {rel_path}", start)

            data = target.read_bytes()
            truncated = False
            if len(data) > _MAX_READ_BYTES:
                data = data[:_MAX_READ_BYTES]
                truncated = True
            text = data.decode("utf-8", errors="replace")

            offset = max(0, int(request.arguments.get("offset") or 0))
            limit_raw = request.arguments.get("limit")
            lines = text.splitlines()
            total_lines = len(lines)
            selected = lines[offset:]
            if limit_raw is not None:
                selected = selected[: max(0, int(limit_raw))]

            return ToolCallResult(
                call_id=request.call_id,
                tool_name=request.tool_name,
                success=True,
                result="\n".join(selected),
                duration_ms=(time.time() - start) * 1000,
                metadata={
                    "path": rel_path,
                    "total_lines": total_lines,
                    "returned_lines": len(selected),
                    "byte_truncated": truncated,
                },
            )
        except WorkspaceEscapeError as exc:
            return _err(request, str(exc), start)
        except Exception as exc:  # pragma: no cover — unexpected I/O errors
            logger.exception("fs_read failed")
            return _err(request, f"{type(exc).__name__}: {exc}", start)


# ---------------------------------------------------------------------------
# fs_write
# ---------------------------------------------------------------------------

FS_WRITE_DEFINITION = ToolDefinition(
    name="fs_write",
    description=(
        "Write text content to a file in the current workspace. Creates "
        "parent directories as needed. Overwrites existing files."
    ),
    parameters=[
        ToolParameter(
            name="path",
            type="string",
            description="Workspace-relative path (e.g. 'notes/draft.md').",
            required=True,
        ),
        ToolParameter(
            name="content",
            type="string",
            description="Text content to write (UTF-8).",
            required=True,
        ),
    ],
    category=ToolCategory.UTILITY,
    risk_level=ToolRiskLevel.MEDIUM,
    when_to_use="Persist intermediate artifacts, drafts, or generated files.",
)


class FsWriteExecutor(ToolExecutor):
    async def execute(self, request: ToolCallRequest) -> ToolCallResult:
        start = time.time()
        try:
            content = str(request.arguments.get("content") or "")
            data = content.encode("utf-8")
            # Size check precedes path resolution so oversize payloads fail
            # before we touch the filesystem.
            if len(data) > _MAX_WRITE_BYTES:
                return _err(
                    request,
                    f"content too large ({len(data)} bytes > {_MAX_WRITE_BYTES})",
                    start,
                )
            root = _workspace_for(request)
            rel_path = str(request.arguments.get("path") or "")
            target = resolve_under(root, rel_path)
            target.parent.mkdir(parents=True, exist_ok=True)
            target.write_bytes(data)

            return ToolCallResult(
                call_id=request.call_id,
                tool_name=request.tool_name,
                success=True,
                result=f"wrote {len(data)} bytes to {rel_path}",
                duration_ms=(time.time() - start) * 1000,
                metadata={"path": rel_path, "bytes_written": len(data)},
            )
        except WorkspaceEscapeError as exc:
            return _err(request, str(exc), start)
        except Exception as exc:  # pragma: no cover
            logger.exception("fs_write failed")
            return _err(request, f"{type(exc).__name__}: {exc}", start)


# ---------------------------------------------------------------------------
# fs_glob
# ---------------------------------------------------------------------------

FS_GLOB_DEFINITION = ToolDefinition(
    name="fs_glob",
    description=(
        "List files in the current workspace matching a glob pattern "
        "(e.g. '**/*.py', 'notes/*.md'). Returns paths relative to the "
        "workspace root."
    ),
    parameters=[
        ToolParameter(
            name="pattern",
            type="string",
            description="Glob pattern. '**' is recursive.",
            required=True,
        ),
    ],
    category=ToolCategory.UTILITY,
    risk_level=ToolRiskLevel.LOW,
    when_to_use="Find files matching a pattern before reading or processing them.",
)


class FsGlobExecutor(ToolExecutor):
    async def execute(self, request: ToolCallRequest) -> ToolCallResult:
        start = time.time()
        try:
            root = _workspace_for(request)
            pattern = str(request.arguments.get("pattern") or "")
            if not pattern:
                return _err(request, "pattern is required", start)

            matches: list[str] = []
            truncated = False
            for path in root.glob(pattern):
                # Guard against symlinks pointing outside the workspace.
                try:
                    resolved = path.resolve()
                    resolved.relative_to(root.resolve())
                except ValueError:
                    continue
                if path.is_file():
                    matches.append(str(path.relative_to(root)))
                    if len(matches) >= _MAX_GLOB_RESULTS:
                        truncated = True
                        break
            matches.sort()

            return ToolCallResult(
                call_id=request.call_id,
                tool_name=request.tool_name,
                success=True,
                result="\n".join(matches) if matches else "(no matches)",
                duration_ms=(time.time() - start) * 1000,
                metadata={"match_count": len(matches), "truncated": truncated},
            )
        except WorkspaceEscapeError as exc:
            return _err(request, str(exc), start)
        except Exception as exc:  # pragma: no cover
            logger.exception("fs_glob failed")
            return _err(request, f"{type(exc).__name__}: {exc}", start)


# ---------------------------------------------------------------------------
# fs_grep
# ---------------------------------------------------------------------------

FS_GREP_DEFINITION = ToolDefinition(
    name="fs_grep",
    description=(
        "Search workspace files for a regex pattern. Returns matching lines "
        "with file path and line number. Pure Python (no ripgrep dependency)."
    ),
    parameters=[
        ToolParameter(
            name="pattern",
            type="string",
            description="Python regex. Case-insensitive by default.",
            required=True,
        ),
        ToolParameter(
            name="glob",
            type="string",
            description="Optional file glob to narrow the search (e.g. '**/*.py').",
            required=False,
            default="**/*",
        ),
        ToolParameter(
            name="case_sensitive",
            type="boolean",
            description="Match case exactly. Default false.",
            required=False,
            default=False,
        ),
    ],
    category=ToolCategory.UTILITY,
    risk_level=ToolRiskLevel.LOW,
    when_to_use="Find occurrences of text or regex patterns in workspace files.",
)


def _grep_scan(
    root: Path,
    regex: re.Pattern[str],
    glob_expr: str,
) -> tuple[list[str], int, bool]:
    """Synchronous grep body — walks glob, reads files, runs regex. Offloaded
    to a worker thread by the executor so the event loop isn't blocked."""
    matches: list[str] = []
    truncated = False
    files_scanned = 0
    root_resolved = root.resolve()
    for path in root.glob(glob_expr):
        if not path.is_file():
            continue
        try:
            resolved = path.resolve()
            resolved.relative_to(root_resolved)
        except ValueError:
            continue
        try:
            size = path.stat().st_size
        except OSError:
            continue
        if size > _MAX_GREP_BYTES_PER_FILE:
            continue
        files_scanned += 1
        try:
            text = path.read_text(encoding="utf-8", errors="replace")
        except OSError:
            continue
        rel = str(path.relative_to(root))
        for lineno, line in enumerate(text.splitlines(), start=1):
            if regex.search(line):
                matches.append(f"{rel}:{lineno}:{line.rstrip()}"[:500])
                if len(matches) >= _MAX_GREP_MATCHES:
                    truncated = True
                    break
        if truncated:
            break
    return matches, files_scanned, truncated


class FsGrepExecutor(ToolExecutor):
    async def execute(self, request: ToolCallRequest) -> ToolCallResult:
        start = time.time()
        try:
            root = _workspace_for(request)
            pattern = str(request.arguments.get("pattern") or "")
            if not pattern:
                return _err(request, "pattern is required", start)
            # Cheap heuristic for catastrophic-backtracking patterns. A real
            # solution needs RE2 or a regex subprocess; reject the obvious
            # footguns upfront.
            if _REDOS_HEURISTIC.search(pattern):
                return _err(
                    request,
                    "pattern rejected: contains nested quantifiers (ReDoS risk)",
                    start,
                )
            glob_expr = str(request.arguments.get("glob") or "**/*")
            flags = 0 if request.arguments.get("case_sensitive") else re.IGNORECASE

            try:
                regex = re.compile(pattern, flags)
            except re.error as exc:
                return _err(request, f"invalid regex: {exc}", start)

            try:
                matches, files_scanned, truncated = await asyncio.wait_for(
                    asyncio.to_thread(_grep_scan, root, regex, glob_expr),
                    timeout=_GREP_TIMEOUT_SECONDS,
                )
            except asyncio.TimeoutError:
                return _err(
                    request,
                    f"grep timed out after {_GREP_TIMEOUT_SECONDS}s",
                    start,
                )

            return ToolCallResult(
                call_id=request.call_id,
                tool_name=request.tool_name,
                success=True,
                result="\n".join(matches) if matches else "(no matches)",
                duration_ms=(time.time() - start) * 1000,
                metadata={
                    "match_count": len(matches),
                    "files_scanned": files_scanned,
                    "truncated": truncated,
                },
            )
        except WorkspaceEscapeError as exc:
            return _err(request, str(exc), start)
        except Exception as exc:  # pragma: no cover
            logger.exception("fs_grep failed")
            return _err(request, f"{type(exc).__name__}: {exc}", start)


# ---------------------------------------------------------------------------
# Helpers + registration
# ---------------------------------------------------------------------------


def _err(request: ToolCallRequest, message: str, start: float) -> ToolCallResult:
    return ToolCallResult(
        call_id=request.call_id,
        tool_name=request.tool_name,
        success=False,
        error=message,
        duration_ms=(time.time() - start) * 1000,
    )


_PRIMITIVE_DEFS: list[tuple[ToolDefinition, ToolExecutor]] = [
    (FS_READ_DEFINITION, FsReadExecutor()),
    (FS_WRITE_DEFINITION, FsWriteExecutor()),
    (FS_GLOB_DEFINITION, FsGlobExecutor()),
    (FS_GREP_DEFINITION, FsGrepExecutor()),
]


def register_primitive_tools() -> None:
    """Register fs_* primitives with the global tool registry.

    Call from startup. The tools remain inactive at request time unless the
    caller includes them in `AssistantConfig.tools_enabled` (or a future
    per-tenant allowlist).
    """
    for definition, executor in _PRIMITIVE_DEFS:
        register_tool(definition, executor)
    logger.info("Registered primitive tools: %s", [d.name for d, _ in _PRIMITIVE_DEFS])
