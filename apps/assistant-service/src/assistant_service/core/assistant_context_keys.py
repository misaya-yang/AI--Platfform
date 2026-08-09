"""Stable owner/session keys for Assistant process-local context state."""

from __future__ import annotations

import hashlib


def _context_receipt_scope(
    *,
    tenant_id: str,
    user_id: str,
    session_id: str,
) -> str:
    """Hash a length-delimited owner/session tuple without delimiter collisions."""

    digest = hashlib.sha256()
    for value in (tenant_id, user_id, session_id):
        encoded = str(value).encode("utf-8")
        digest.update(len(encoded).to_bytes(8, "big"))
        digest.update(encoded)
    return f"ctxscope_{digest.hexdigest()}"


def _context_receipt_key(*, scope: str, model_id: str) -> str:
    model_digest = hashlib.sha256(str(model_id).encode("utf-8")).hexdigest()
    return f"{scope}:{model_digest}"


def _working_memory_scope(
    *,
    tenant_id: str,
    user_id: str,
    session_id: str,
) -> str:
    """Hash a length-delimited owner/session tuple for process-local state."""

    digest = hashlib.sha256()
    for value in (tenant_id, user_id, session_id):
        encoded = str(value).encode("utf-8")
        digest.update(len(encoded).to_bytes(8, "big"))
        digest.update(encoded)
    return f"wmscope_{digest.hexdigest()}"
