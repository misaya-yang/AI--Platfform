"""Persistent, prompt-agnostic Assistant runtime ownership.

The assignment is written once per session.  It is intentionally separate
from request text, model selection, and tool discovery so one active session
can never switch kernels mid-turn.
"""

from __future__ import annotations

import hashlib
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
class RuntimeAssignmentPolicy:
    """Stable new-session canary policy; prompt and model data are not inputs."""

    default_owner: RuntimeOwner
    kernel_revision: str | None
    canary_percent: int
    e2e_tenants: frozenset[str]
    kill_switch: bool
    salt: str

    @classmethod
    def from_env(cls) -> RuntimeAssignmentPolicy:
        owner, _ = runtime_assignment_policy_from_env()
        revision = os.getenv("CODEX_RUNTIME_KERNEL_REVISION", "").strip() or None
        raw_percent = os.getenv("ASSISTANT_RUNTIME_CANARY_PERCENT", "").strip()
        percent = 100 if owner == "codex_candidate" and not raw_percent else int(raw_percent or 0)
        if percent not in {0, 1, 10, 25, 50, 100}:
            raise ValueError("ASSISTANT_RUNTIME_CANARY_PERCENT must be one of 0,1,10,25,50,100")
        tenants = frozenset(
            item.strip()
            for item in os.getenv("ASSISTANT_RUNTIME_CANARY_E2E_TENANTS", "").split(",")
            if item.strip()
        )
        kill_switch = os.getenv("ASSISTANT_RUNTIME_CANARY_KILL_SWITCH", "false").strip().lower() in {
            "1", "true", "yes", "on"
        }
        salt = os.getenv("ASSISTANT_RUNTIME_CANARY_SALT", "").strip()
        if percent not in {0, 100} and not salt:
            salt = os.getenv("GATEWAY_ASSISTANT_SHARED_SECRET", "").strip()
        if percent not in {0, 100} and not salt:
            raise ValueError("Canary rollout requires ASSISTANT_RUNTIME_CANARY_SALT or a server secret")
        return cls(
            default_owner=owner,
            kernel_revision=revision,
            canary_percent=percent,
            e2e_tenants=tenants,
            kill_switch=kill_switch,
            salt=salt,
        )

    def choose(self, *, tenant_id: str, session_id: str) -> tuple[RuntimeOwner, str]:
        """Choose once for a new session using only stable rollout inputs."""
        if self.kill_switch:
            return "python_control", "canary_kill_switch"
        if tenant_id in self.e2e_tenants:
            self._require_candidate_revision()
            return "codex_candidate", "e2e_tenant_override"
        if self.canary_percent == 100:
            self._require_candidate_revision()
            return "codex_candidate", "canary_100"
        digest = hashlib.sha256(f"{self.salt}:{tenant_id}:{session_id}".encode()).digest()
        bucket = int.from_bytes(digest[:8], "big") % 100
        if bucket < self.canary_percent:
            self._require_candidate_revision()
            return "codex_candidate", f"canary_{self.canary_percent}"
        return "python_control", f"canary_{self.canary_percent}_control"

    def _require_candidate_revision(self) -> None:
        if self.kernel_revision is None:
            raise ValueError("Codex candidate assignments require a pinned kernel revision")


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

    async def bind_new_session(
        self,
        *,
        tenant_id: str,
        user_id: str,
        session_id: str,
        policy: RuntimeAssignmentPolicy,
    ) -> RuntimeAssignment:
        existing = await self.resolve(
            tenant_id=tenant_id, user_id=user_id, session_id=session_id
        )
        if existing is not None:
            return existing
        owner, reason = policy.choose(tenant_id=tenant_id, session_id=session_id)
        return await self.bind(
            tenant_id=tenant_id,
            user_id=user_id,
            session_id=session_id,
            runtime_owner=owner,
            kernel_revision=policy.kernel_revision if owner == "codex_candidate" else None,
            assignment_reason=reason,
        )

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
    if owner not in _VALID_RUNTIME_OWNERS:
        raise ValueError("unsupported Assistant runtime owner")
    if owner == "codex_candidate" and revision is None:
        raise ValueError("Codex candidate assignments require a pinned kernel revision")
    return cast(RuntimeOwner, owner), revision if owner == "codex_candidate" else None


def _validate_assignment(runtime_owner: str, kernel_revision: str | None) -> None:
    if runtime_owner not in _VALID_RUNTIME_OWNERS:
        raise ValueError("unsupported Assistant runtime owner")
    if runtime_owner == "python_control" and kernel_revision is not None:
        raise ValueError("Python control assignments cannot carry a kernel revision")
    if runtime_owner == "codex_candidate" and kernel_revision is None:
        raise ValueError("Codex candidate assignments require a pinned kernel revision")
