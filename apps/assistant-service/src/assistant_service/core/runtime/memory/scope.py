"""Stable, collision-resistant identifiers for scoped runtime memory."""

from __future__ import annotations

import hashlib
import re

_COLLECTION_COMPONENT_RE = re.compile(r"[^a-zA-Z0-9_-]+")
_VALID_LEGACY_COLLECTION_RE = re.compile(r"^[a-zA-Z0-9_.-]+$")


def scoped_collection_name(prefix: str, tenant_id: str, user_id: str) -> str:
    """Return a bounded vector collection name unique to a tenant/user scope.

    Replacing punctuation is lossy (for example ``tenant-a`` and ``tenant_a``),
    so the collection identity is derived from length-delimited raw values.  A
    readable, bounded prefix remains only for operators; it is not the scope
    identity.
    """

    raw_prefix = str(prefix or "assistant_memory")
    safe_prefix = _COLLECTION_COMPONENT_RE.sub("_", raw_prefix).strip("_")
    safe_prefix = (safe_prefix or "assistant_memory")[:48]
    prefix_digest = hashlib.sha256(raw_prefix.encode()).hexdigest()[:12]

    tenant = str(tenant_id or "")
    user = str(user_id or "")
    scope_material = f"{len(tenant)}:{tenant}{len(user)}:{user}"
    scope_digest = hashlib.sha256(scope_material.encode()).hexdigest()
    return f"{safe_prefix}_{prefix_digest}_scope_{scope_digest}"


def legacy_collection_name(prefix: str, tenant_id: str, user_id: str) -> str | None:
    """Return the pre-UAO collection name only when it could have existed."""

    legacy = (
        f"{str(prefix or 'assistant_memory')}_"
        f"{str(tenant_id or '').replace('-', '_')}_"
        f"{str(user_id or '').replace('-', '_')}"
    )
    if len(legacy) > 255 or not _VALID_LEGACY_COLLECTION_RE.fullmatch(legacy):
        return None
    return legacy


def scoped_collection_names(prefix: str, tenant_id: str, user_id: str) -> tuple[str, ...]:
    """Return current then legacy collection names without duplicates."""

    current = scoped_collection_name(prefix, tenant_id, user_id)
    legacy = legacy_collection_name(prefix, tenant_id, user_id)
    if not legacy or legacy == current:
        return (current,)
    return (current, legacy)


def scoped_collection_candidates(
    prefix: str,
    tenant_id: str,
    user_id: str,
    *,
    dimension: int | None = None,
    persisted: list[str] | tuple[str, ...] | None = None,
) -> tuple[str, ...]:
    """Enumerate persisted/current/legacy and historical dimension variants."""

    candidates = [
        str(item)
        for item in (persisted or ())
        if item and _VALID_LEGACY_COLLECTION_RE.fullmatch(str(item))
    ]
    base_names = scoped_collection_names(prefix, tenant_id, user_id)
    candidates.extend(base_names)
    if dimension is not None and dimension > 0:
        candidates.extend(f"{name}_d{dimension}" for name in base_names)
    return tuple(dict.fromkeys(candidates))


def public_source_label(source_path: object) -> str:
    """Return a host-path-free source label suitable for traces and API output."""

    normalized = str(source_path or "").replace("\\", "/").rstrip("/")
    return normalized.rsplit("/", 1)[-1] if normalized else ""
