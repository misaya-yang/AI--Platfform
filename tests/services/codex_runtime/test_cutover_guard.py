from __future__ import annotations

import pytest

from src.services.codex_runtime.cutover_guard import (
    CutoverEvidence,
    LegacyLoopDeletionGuard,
    LegacyLoopUsageCounter,
    rehearse_assignment_rollback,
)


def test_legacy_usage_counter_and_deletion_guard_fail_closed() -> None:
    counter = LegacyLoopUsageCounter()
    counter.record("session-a")
    counter.record("session-a")
    assert counter.snapshot() == {"legacy_calls": 2, "legacy_sessions": 1}
    blocked = LegacyLoopDeletionGuard()
    with pytest.raises(RuntimeError, match="DELETION_BLOCKED"):
        blocked.require_delete_authorization()
    assert not blocked.can_delete()


def test_deletion_requires_full_window_zero_usage_and_rollback_evidence() -> None:
    evidence = CutoverEvidence(
        rollout_percent=100,
        canary_window_complete=True,
        legacy_calls=0,
        rollback_rehearsal_passed=True,
        runtime_assignment_rollback_passed=True,
        v1_compatibility_passed=True,
    )
    assert LegacyLoopDeletionGuard(evidence).can_delete()
    assert rehearse_assignment_rollback(
        existing_owner="codex_candidate", policy_owner_after_kill_switch="python_control"
    )
