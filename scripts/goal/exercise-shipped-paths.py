#!/usr/bin/env python3
"""Direct import+call exercise for workstream A shipped paths (plan step 4)."""

from __future__ import annotations

import asyncio
import json
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(ROOT))
sys.path.insert(0, str(ROOT / "apps" / "assistant-service" / "src"))

from assistant_service.core.gateway.execution_gateway import (  # noqa: E402
    AssistantExecutionGateway,
)

from src.services.eval.trace_feedback import (  # noqa: E402
    FAILURE_MODE_TOOL_ERROR,
    classify_trace_failure,
)


class _NoopInvoker:
    async def invoke(self, **_kwargs):  # type: ignore[no-untyped-def]
        return {"ok": True}


def exercise_classify_trace_failure() -> dict[str, str]:
    failed = classify_trace_failure(
        {
            "trace": {
                "trace_id": "11111111-1111-1111-1111-111111111111",
                "trace_family": "assistant",
                "status": "failed",
            },
            "events": [{"event_type": "tool_error", "payload": {"tool_name": "search"}}],
            "spans": [],
        }
    )
    succeeded = classify_trace_failure(
        {
            "trace": {
                "trace_id": "77777777-7777-7777-7777-777777777777",
                "trace_family": "assistant",
                "status": "succeeded",
            },
            "events": [{"event_type": "tool_error", "payload": {"tool_name": "search"}}],
            "spans": [],
        }
    )
    return {
        "failed_mode": failed.failure_mode,
        "succeeded_mode": succeeded.failure_mode,
        "failed_is_tool_error": str(failed.failure_mode == FAILURE_MODE_TOOL_ERROR),
        "succeeded_not_tool_error": str(succeeded.failure_mode != FAILURE_MODE_TOOL_ERROR),
    }


async def exercise_prepare_run_resume() -> dict[str, str]:
    gw = AssistantExecutionGateway(tool_invoker=_NoopInvoker(), database=None)
    run_id = "aaaaaaaa-aaaa-4aaa-8aaa-aaaaaaaaaaaa"
    tenant_id = "tenant-exercise"
    user_id = "user-exercise"
    session_id = "session-exercise"

    await gw.start_run(
        run_id=run_id,
        tenant_id=tenant_id,
        user_id=user_id,
        session_id=session_id,
        engine="agent_loop",
        execution_profile="safe",
        memory_mode="auto",
        os_agent_enabled=False,
        request_preview="redacted",
    )
    await gw.save_run_checkpoint(
        run_id=run_id,
        tenant_id=tenant_id,
        user_id=user_id,
        session_id=session_id,
        phase="approval_pending",
        pending_tool={"tool_id": "tc1", "tool_name": "execute_python_code"},
        approval_id="22222222-2222-4222-8222-222222222222",
        status="blocked",
    )

    blocked = await gw.prepare_run_resume(
        run_id=run_id,
        tenant_id=tenant_id,
        user_id=user_id,
    )
    terminal_run_id = "bbbbbbbb-bbbb-4bbb-8bbb-bbbbbbbbbbbb"
    await gw.start_run(
        run_id=terminal_run_id,
        tenant_id=tenant_id,
        user_id=user_id,
        session_id=session_id,
        engine="agent_loop",
        execution_profile="safe",
        memory_mode="auto",
        os_agent_enabled=False,
        request_preview="redacted",
    )
    await gw.finish_run(
        run_id=terminal_run_id,
        status="succeeded",
        tenant_id=tenant_id,
        user_id=user_id,
    )
    terminal_blocked = await gw.prepare_run_resume(
        run_id=terminal_run_id,
        tenant_id=tenant_id,
        user_id=user_id,
    )

    return {
        "pending_status": str(blocked.get("status") if blocked else None),
        "pending_reason": str(blocked.get("reason") if blocked else None),
        "pending_recoverable": str(blocked.get("recoverable") if blocked else None),
        "terminal_status": str(terminal_blocked.get("status") if terminal_blocked else None),
        "terminal_reason": str(terminal_blocked.get("reason") if terminal_blocked else None),
        "terminal_recoverable": str(
            terminal_blocked.get("recoverable") if terminal_blocked else None
        ),
    }


def main() -> int:
    results = {
        "classify_trace_failure": exercise_classify_trace_failure(),
        "prepare_run_resume": asyncio.run(exercise_prepare_run_resume()),
    }
    print(json.dumps(results, indent=2, sort_keys=True))

    assert results["classify_trace_failure"]["failed_is_tool_error"] == "True"
    assert results["classify_trace_failure"]["succeeded_not_tool_error"] == "True"
    assert results["prepare_run_resume"]["pending_status"] == "blocked"
    assert results["prepare_run_resume"]["pending_reason"] == "approval_required"
    assert results["prepare_run_resume"]["pending_recoverable"] == "True"
    assert results["prepare_run_resume"]["terminal_reason"] == "run_already_terminal"
    assert results["prepare_run_resume"]["terminal_recoverable"] == "False"
    print("exercise-shipped-paths: ok")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
