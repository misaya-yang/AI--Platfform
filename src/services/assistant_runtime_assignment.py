"""Persistent, prompt-agnostic Assistant runtime ownership.

The assignment is written once per session.  It is intentionally separate
from request text, model selection, and tool discovery so one active session
can never switch kernels mid-turn.
"""

from __future__ import annotations

import os
from dataclasses import dataclass
from typing import Literal, Protocol, cast

RuntimeOwner = Literal["python_control", "codex_candidate"]
_VALID_RUNTIME_OWNERS = frozenset({"python_control", "codex_candidate"})


class RuntimeAssignmentDatabase(Protocol):
    async def fetchrow(self, query: str, *args): ...


class RuntimeAssignmentConflict(RuntimeError):
    """The session already belongs to another runtime or identity scope."""


@dataclass(frozen=True, slots=True)
class RuntimeAssignment:
    tenant_id: str
    user_id: str
    session_id: str
    runtime_owner: RuntimeOwner
    kernel_revision: str | None
    assignment_reason: str


class AssistantRuntimeAssignmentStore:
    def __init__(self, database: RuntimeAssignmentDatabase) -> None:
        self._database = database

    async def bind(
        self,
        *,
        tenant_id: str,
        user_id: str,
        session_id: str,
        runtime_owner: RuntimeOwner,
        kernel_revision: str | None,
        assignment_reason: str = "default_policy",
    ) -> RuntimeAssignment:
        _validate_assignment(runtime_owner, kernel_revision)
        await self._database.fetchrow(
            """
            INSERT INTO assistant_session_runtime_assignments (
                tenant_id, user_id, session_id, runtime_owner,
                kernel_revision, assignment_reason
            ) VALUES ($1, $2, $3, $4, $5, $6)
            ON CONFLICT (tenant_id, user_id, session_id) DO NOTHING
            RETURNING tenant_id, user_id, session_id, runtime_owner,
                      kernel_revision, assignment_reason
            """,
            tenant_id,
            user_id,
            session_id,
            runtime_owner,
            kernel_revision,
            assignment_reason,
        )
        existing = await self.resolve(
            tenant_id=tenant_id,
            user_id=user_id,
            session_id=session_id,
        )
        if existing is None or (
            existing.runtime_owner != runtime_owner
            or existing.kernel_revision != kernel_revision
        ):
            raise RuntimeAssignmentConflict(
                "session is already bound to another runtime assignment"
            )
        return existing

    async def resolve(
        self,
        *,
        tenant_id: str,
        user_id: str,
        session_id: str,
    ) -> RuntimeAssignment | None:
        row = await self._database.fetchrow(
            """
            SELECT tenant_id, user_id, session_id, runtime_owner,
                   kernel_revision, assignment_reason
            FROM assistant_session_runtime_assignments
            WHERE tenant_id = $1 AND user_id = $2 AND session_id = $3
            """,
            tenant_id,
            user_id,
            session_id,
        )
        if row is None:
            return None
        return RuntimeAssignment(
            tenant_id=str(row["tenant_id"]),
            user_id=str(row["user_id"]),
            session_id=str(row["session_id"]),
            runtime_owner=cast(RuntimeOwner, str(row["runtime_owner"])),
            kernel_revision=(
                str(row["kernel_revision"])
                if row["kernel_revision"] is not None
                else None
            ),
            assignment_reason=str(row["assignment_reason"]),
        )


def runtime_assignment_policy_from_env() -> tuple[RuntimeOwner, str | None]:
    owner = os.getenv("ASSISTANT_RUNTIME_DEFAULT_OWNER", "python_control").strip()
    revision = os.getenv("CODEX_RUNTIME_KERNEL_REVISION", "").strip() or None
    _validate_assignment(owner, revision)
    return cast(RuntimeOwner, owner), revision


def _validate_assignment(runtime_owner: str, kernel_revision: str | None) -> None:
    if runtime_owner not in _VALID_RUNTIME_OWNERS:
        raise ValueError("unsupported Assistant runtime owner")
    if runtime_owner == "python_control" and kernel_revision is not None:
        raise ValueError("Python control assignments cannot carry a kernel revision")
    if runtime_owner == "codex_candidate" and kernel_revision is None:
        raise ValueError("Codex candidate assignments require a pinned kernel revision")
