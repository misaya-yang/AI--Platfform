"""
Tenant-scoped workspace resolver for primitive filesystem tools.

Each `(tenant_id, session_id)` pair gets an isolated directory at
`{ASSISTANT_WORKSPACE_ROOT}/{tenant_id}/{session_id}/`. All fs_* primitive
tools resolve paths relative to that root and **reject any path that escapes
it** (symlinks, `..` traversal, absolute paths outside the root).

ASSISTANT_WORKSPACE_ROOT defaults to `/tmp/ai-gateway-workspace`. In
production set it to a dedicated volume with per-tenant quotas.
"""

from __future__ import annotations

import os
import re
from pathlib import Path

DEFAULT_WORKSPACE_ROOT = "/tmp/ai-gateway-workspace"

# Strict charset for opaque IDs coming from auth headers — alphanumerics,
# dash, underscore, dot (but not `..`), up to 128 chars. Rejects NULL bytes,
# whitespace, path separators, Unicode confusables, and anything that could
# cause surprises across filesystems.
_ID_CHARSET = re.compile(r"^[A-Za-z0-9_\-.]{1,128}$")


class WorkspaceEscapeError(ValueError):
    """Raised when a requested path resolves outside the tenant workspace."""


def workspace_root() -> Path:
    """Root of all tenant workspaces, honoring ASSISTANT_WORKSPACE_ROOT."""
    root = os.environ.get("ASSISTANT_WORKSPACE_ROOT") or DEFAULT_WORKSPACE_ROOT
    return Path(root).resolve()


def tenant_workspace(tenant_id: str, session_id: str) -> Path:
    """Return (and create if missing) the isolated workspace for this
    tenant+session. IDs are opaque but must pass `_ID_CHARSET` — NULL bytes,
    whitespace, `..`, and unexpected unicode are all rejected."""
    for part, label in ((tenant_id, "tenant_id"), (session_id, "session_id")):
        if not part or part in {".", ".."} or not _ID_CHARSET.fullmatch(part):
            raise WorkspaceEscapeError(f"invalid {label}: {part!r}")

    path = workspace_root() / tenant_id / session_id
    path.mkdir(parents=True, exist_ok=True)
    return path.resolve()


def resolve_under(root: Path, user_path: str) -> Path:
    """Resolve `user_path` relative to `root` and ensure the result is
    contained in `root`. Rejects absolute paths, `..` escapes, and paths
    that resolve through symlinks out of the sandbox.

    The returned path may not yet exist (caller may be about to create it).
    """
    if not user_path or user_path.strip() == "":
        raise WorkspaceEscapeError("path is required")

    raw = Path(user_path)
    if raw.is_absolute():
        raise WorkspaceEscapeError(
            f"absolute paths are not allowed: {user_path!r}"
        )

    candidate = (root / raw).resolve()
    # Path.is_relative_to is Python 3.9+; explicit comparison for clarity.
    root_resolved = root.resolve()
    try:
        candidate.relative_to(root_resolved)
    except ValueError as exc:
        raise WorkspaceEscapeError(
            f"path escapes workspace: {user_path!r}"
        ) from exc
    return candidate
