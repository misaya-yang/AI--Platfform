"""In-memory execution gateway record models.

These records remain re-exported by ``execution_gateway`` for compatibility.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from datetime import datetime, timezone
from typing import Any


@dataclass
class ApprovalRecord:
    """In-memory fallback approval record."""

    approval_id: str
    tenant_id: str
    user_id: str
    session_id: str
    run_id: str
    tool_name: str
    arguments: dict[str, Any]
    status: str = "pending"
    reason: str | None = None
    approved_by: str | None = None
    approved_at: datetime | None = None
    expires_at: datetime | None = None


@dataclass
class RunRecord:
    """In-memory fallback run record."""

    run_id: str
    tenant_id: str
    user_id: str
    session_id: str
    status: str
    engine: str
    execution_profile: str
    memory_mode: str
    os_agent_enabled: bool
    request_preview: str
    queue_mode: str | None = None
    runtime_mode: str | None = None
    agent_id: str | None = None
    agent_version_id: str | None = None
    agent_draft_revision: int | None = None
    publication_id: str | None = None
    channel: str | None = None
    runtime_fingerprint: str | None = None
    agent_spec_hash: str | None = None
    usage: dict[str, Any] = field(default_factory=dict)
    error: str | None = None
    started_at: datetime = field(default_factory=lambda: datetime.now(timezone.utc))
    finished_at: datetime | None = None


@dataclass
class RunCheckpointRecord:
    """In-memory fallback checkpoint record."""

    checkpoint_id: str
    run_id: str
    tenant_id: str
    user_id: str
    session_id: str
    phase: str
    iteration: int
    message_state_hash: str
    pending_tool: dict[str, Any] = field(default_factory=dict)
    approval_id: str | None = None
    idempotency_keys: dict[str, Any] = field(default_factory=dict)
    resume_payload: dict[str, Any] = field(default_factory=dict)
    status: str = "running"
    error: str | None = None
    agent_id: str | None = None
    agent_version_id: str | None = None
    agent_draft_revision: int | None = None
    publication_id: str | None = None
    channel: str | None = None
    runtime_fingerprint: str | None = None
    agent_spec_hash: str | None = None
    created_at: datetime = field(default_factory=lambda: datetime.now(timezone.utc))
