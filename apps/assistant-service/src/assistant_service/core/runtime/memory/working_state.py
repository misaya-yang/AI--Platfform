"""Bounded persistence helpers for session-scoped Working Memory."""

from __future__ import annotations

import hashlib
from typing import Any

from ai_gateway_core.working_memory import WorkingMemory

WORKING_MEMORY_KEY_PREFIX = "working_memory"
WORKING_MEMORY_SCHEMA_VERSION = "assistant-working-memory/v2"
LEGACY_WORKING_MEMORY_KEY = "working_memory"
LEGACY_WORKING_MEMORY_SCHEMA_VERSION = "assistant-working-memory/legacy-compat"


def _scope_digest(*, tenant_id: str, user_id: str, session_id: str) -> str:
    digest = hashlib.sha256()
    for value in (tenant_id, user_id, session_id):
        encoded = str(value).encode("utf-8")
        digest.update(len(encoded).to_bytes(8, "big"))
        digest.update(encoded)
    return digest.hexdigest()


def working_memory_key(*, tenant_id: str, user_id: str, session_id: str) -> str:
    """Return a collision-resistant key bound to tenant, user, and session."""

    return (
        f"{WORKING_MEMORY_KEY_PREFIX}:"
        f"{_scope_digest(tenant_id=tenant_id, user_id=user_id, session_id=session_id)}"
    )


async def restore_working_memory(
    memory_service: Any,
    *,
    tenant_id: str,
    user_id: str,
    session_id: str,
    legacy_owner_verified: bool = False,
) -> WorkingMemory | None:
    """Load owner-bound v2 state, then an owner-proven legacy fallback.

    A malformed v2 envelope fails closed instead of silently downgrading to the
    legacy key.  The legacy row has no user binding of its own, so callers must
    first prove that the durable session belongs to the requesting tenant/user.
    """

    payload = await memory_service.get_session_memory(
        tenant_id=tenant_id,
        session_id=session_id,
        key=working_memory_key(
            tenant_id=tenant_id,
            user_id=user_id,
            session_id=session_id,
        ),
    )
    if payload is not None:
        if not isinstance(payload, dict):
            raise ValueError("Working memory envelope is invalid")
        expected_scope = _scope_digest(
            tenant_id=tenant_id,
            user_id=user_id,
            session_id=session_id,
        )
        if payload.get("schema_version") != WORKING_MEMORY_SCHEMA_VERSION:
            raise ValueError("Working memory schema is unsupported")
        if payload.get("owner_scope") != expected_scope:
            raise ValueError("Working memory owner scope mismatch")
        return WorkingMemory.from_persisted_dict(
            payload.get("working_memory"),
            expected_session_id=session_id,
        )

    if not legacy_owner_verified:
        return None
    legacy_payload = await memory_service.get_session_memory(
        tenant_id=tenant_id,
        session_id=session_id,
        key=LEGACY_WORKING_MEMORY_KEY,
    )
    if legacy_payload is None:
        return None
    if not isinstance(legacy_payload, dict):
        raise ValueError("Legacy working memory payload is invalid")
    # Accept the historical raw WorkingMemory dictionary.  A compatibility
    # envelope is also understood for deployments that adopted it early.
    if legacy_payload.get("schema_version") == LEGACY_WORKING_MEMORY_SCHEMA_VERSION:
        expected_scope = _scope_digest(
            tenant_id=tenant_id,
            user_id=user_id,
            session_id=session_id,
        )
        if legacy_payload.get("owner_scope") != expected_scope:
            raise ValueError("Legacy working memory owner scope mismatch")
        legacy_payload = legacy_payload.get("working_memory")
    return WorkingMemory.from_persisted_dict(
        legacy_payload,
        expected_session_id=session_id,
    )


async def persist_working_memory(
    memory_service: Any,
    *,
    tenant_id: str,
    user_id: str,
    session_id: str,
    memory: WorkingMemory,
    write_legacy_compat: bool = True,
) -> bool:
    """Persist v2 state and, by default, the legacy consumer projection."""

    if memory.session_id != session_id:
        raise ValueError("Working memory session mismatch")
    owner_scope = _scope_digest(
        tenant_id=tenant_id,
        user_id=user_id,
        session_id=session_id,
    )
    serialized = memory.to_dict()
    result = await memory_service.set_session_memory(
        tenant_id=tenant_id,
        session_id=session_id,
        key=working_memory_key(
            tenant_id=tenant_id,
            user_id=user_id,
            session_id=session_id,
        ),
        value={
            "schema_version": WORKING_MEMORY_SCHEMA_VERSION,
            "owner_scope": owner_scope,
            "working_memory": serialized,
        },
        metadata={
            "schema_version": WORKING_MEMORY_SCHEMA_VERSION,
            "scope": "tenant_user_session",
            "owner_scope": owner_scope,
            "source": "assistant_working_memory",
        },
    )
    if result is False:
        return False
    if not write_legacy_compat:
        return True

    # Existing session-context consumers still read the historical raw key.
    # Keep that projection until they have all moved to the v2 owner-bound key.
    legacy_result = await memory_service.set_session_memory(
        tenant_id=tenant_id,
        session_id=session_id,
        key=LEGACY_WORKING_MEMORY_KEY,
        value=serialized,
        metadata={
            "schema_version": LEGACY_WORKING_MEMORY_SCHEMA_VERSION,
            "scope": "tenant_user_session",
            "owner_scope": owner_scope,
            "source": "assistant_working_memory_compat",
        },
    )
    return legacy_result is not False


def bounded_working_memory_context(
    memory: WorkingMemory | None,
    *,
    max_chars: int = 2_000,
) -> str | None:
    """Render Working Memory as lower-priority, bounded task-state data."""

    if memory is None or not (memory.goal or memory.tasks or memory.collected_info or memory.notes):
        return None
    return memory.to_markdown()[: max(0, max_chars)] or None
