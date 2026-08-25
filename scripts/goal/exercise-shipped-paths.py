#!/usr/bin/env python3
"""Direct import+call exercise for workstream A shipped paths (plan step 4)."""

from __future__ import annotations

import json
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(ROOT))
from src.services.eval.trace_feedback import (  # noqa: E402
    FAILURE_MODE_TOOL_ERROR,
    classify_trace_failure,
)


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

def main() -> int:
    results = {"classify_trace_failure": exercise_classify_trace_failure()}
    print(json.dumps(results, indent=2, sort_keys=True))

    assert results["classify_trace_failure"]["failed_is_tool_error"] == "True"
    assert results["classify_trace_failure"]["succeeded_not_tool_error"] == "True"
    print("exercise-shipped-paths: ok")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
