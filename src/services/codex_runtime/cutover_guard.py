"""Fail-closed evidence controls for CHR-06 legacy-loop deletion."""

from __future__ import annotations

from dataclasses import dataclass, field
from threading import Lock

from ..assistant_runtime_assignment import RuntimeOwner


class LegacyLoopUsageCounter:
    """Process-local safety counter; a deployment can export it to telemetry."""

    def __init__(self) -> None:
        self._lock = Lock()
        self._calls = 0
        self._sessions: set[str] = set()

    def record(self, session_id: str | None = None) -> None:
        with self._lock:
            self._calls += 1
            if session_id:
                self._sessions.add(str(session_id))

    def snapshot(self) -> dict[str, int]:
        with self._lock:
            return {"legacy_calls": self._calls, "legacy_sessions": len(self._sessions)}


@dataclass(frozen=True, slots=True)
class CutoverEvidence:
    rollout_percent: int = 0
    canary_window_complete: bool = False
    legacy_calls: int = 0
    rollback_rehearsal_passed: bool = False
    v1_compatibility_passed: bool = False
    runtime_assignment_rollback_passed: bool = False


@dataclass(frozen=True, slots=True)
class LegacyLoopDeletionGuard:
    """Deletion is permitted only with explicit, independent release evidence."""

    evidence: CutoverEvidence = field(default_factory=CutoverEvidence)

    def can_delete(self) -> bool:
        e = self.evidence
        return (
            e.rollout_percent == 100
            and e.canary_window_complete
            and e.legacy_calls == 0
            and e.rollback_rehearsal_passed
            and e.runtime_assignment_rollback_passed
            and e.v1_compatibility_passed
        )

    def require_delete_authorization(self) -> None:
        if not self.can_delete():
            raise RuntimeError("CODEX_RUNTIME_LEGACY_LOOP_DELETION_BLOCKED")


def rehearse_assignment_rollback(
    *,
    existing_owner: RuntimeOwner,
    policy_owner_after_kill_switch: RuntimeOwner,
) -> bool:
    """Rollback changes only future assignments; an existing owner is immutable."""
    if policy_owner_after_kill_switch != "python_control":
        return False
    return existing_owner in {"python_control", "codex_candidate"}


__all__ = [
    "CutoverEvidence",
    "LegacyLoopDeletionGuard",
    "LegacyLoopUsageCounter",
    "rehearse_assignment_rollback",
]
