from __future__ import annotations

from datetime import datetime, timezone
from typing import Any

import pytest
from ai_gateway_core.eval.evaluator_executor import (
    REQUIRED_ASSISTANT_HARD_BLOCKERS,
    EvaluatorExecutor,
)

from src.services.eval import eval_outbox_worker as outbox_module
from src.services.eval.eval_candidate_client import EvalCandidateResult
from src.services.eval.eval_outbox_worker import _candidate_cost_cents


def _run_case(case_id: str, trial_index: int) -> dict[str, Any]:
    return {
        "run_case_id": f"{case_id}-{trial_index}",
        "case_id": case_id,
        "example_id": f"example-{case_id}",
        "trial_index": trial_index,
        "status": "queued",
        "input": {"message": f"message {case_id}"},
        "expected_output": {},
        "expected_trajectory": {},
        "assertions": [],
        "metadata": {
            "critical": case_id == "critical" or case_id in REQUIRED_ASSISTANT_HARD_BLOCKERS
        },
        "observed_metrics": {},
    }


def test_candidate_contract_cost_is_unknown_without_catalog_pricing() -> None:
    assert (
        _candidate_cost_cents("unknown-eval-model", {"input_tokens": 1000, "output_tokens": 1000})
        is None
    )


@pytest.mark.asyncio
async def test_candidate_runner_links_trace_only_after_persistence(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    class Repository:
        async def list_traces(self, **_kwargs: Any):
            return [], 0

        async def get_trace_detail(self, **_kwargs: Any) -> dict[str, Any]:
            return {
                "trace": {
                    "trace_id": "trace-persisted",
                    "trace_family": "assistant",
                    "status": "succeeded",
                    "output_preview": "EVAL-BASELINE-OK",
                    "total_latency_ms": 10,
                    "model_id": "qwen3.7-plus",
                    "metadata": {},
                },
                "spans": [
                    {
                        "span_kind": "model_invocation",
                        "started_at": datetime.now(timezone.utc),
                    }
                ],
                "events": [],
            }

        async def update_experiment_run_case(self, **kwargs: Any) -> None:
            assert "candidate_trace_id" not in kwargs

    class Candidate:
        async def run(self, **kwargs: Any) -> EvalCandidateResult:
            callback = kwargs.get("on_run_started")
            if callback is not None:
                await callback("trace-persisted")
            return EvalCandidateResult(
                trace_id="trace-persisted",
                output="EVAL-BASELINE-OK",
                usage={"input_tokens": 10, "output_tokens": 2, "total_tokens": 12},
                fingerprint={},
            )

    monkeypatch.setattr(outbox_module, "_eval_candidate_client", Candidate())
    result = await outbox_module._build_candidate_runner(Repository())(
        tenant_id="tenant-a",
        run_case={
            "run_case_id": "run-case-1",
            "case_id": "case-1",
            "input": {"message": "return marker"},
            "expected_output": {"contains": ["EVAL-BASELINE-OK"]},
            "expected_trajectory": {},
            "assertions": [{"type": "no_sensitive_output"}],
            "metadata": {"critical": True},
        },
        execution_config={},
    )

    assert result["contract_result"]["passed"] is True


class _LiveRepository:
    def __init__(self, cases: list[dict[str, Any]]) -> None:
        self.cases = cases
        self.run = {
            "run_id": "run-live",
            "execution_config": {
                "evaluators": [
                    {
                        "evaluator_id": "rule-a",
                        "name": "status",
                        "evaluator_type": "rule",
                        "version": "v1",
                        "filter_config": {"rules": [{"type": "status_eq", "value": "succeeded"}]},
                    },
                    {
                        "evaluator_id": "trajectory-a",
                        "name": "trajectory",
                        "evaluator_type": "trajectory",
                        "version": "v1",
                        "filter_config": {},
                    },
                ]
            },
        }
        self.run_updates: list[dict[str, Any]] = []
        self.scores: list[dict[str, Any]] = []

    async def update_experiment_run(self, **kwargs: Any) -> None:
        self.run_updates.append(kwargs)

    async def get_experiment_run(self, **_kwargs: Any) -> dict[str, Any]:
        return self.run

    async def list_experiment_run_cases(self, **_kwargs: Any) -> list[dict[str, Any]]:
        return self.cases

    async def update_experiment_run_case(self, *, run_case_id: str, **kwargs: Any) -> None:
        row = next(item for item in self.cases if item["run_case_id"] == run_case_id)
        row.update(kwargs)

    async def create_eval_score(self, **kwargs: Any) -> dict[str, Any]:
        self.scores.append(kwargs)
        payload = kwargs["payload"]
        return {
            "score_id": f"score-{len(self.scores)}",
            "numeric_value": payload.get("numeric_value"),
            "label": payload.get("label"),
        }


def _candidate_result(run_case: dict[str, Any]) -> dict[str, Any]:
    trace_id = f"trace-{run_case['run_case_id']}"
    latency = 100 if int(run_case["trial_index"]) == 1 else 200
    return {
        "detail": {
            "trace": {
                "trace_id": trace_id,
                "trace_family": "assistant",
                "status": "succeeded",
                "total_latency_ms": latency,
                "model_id": "qwen3.7-plus",
                "provider": "dashscope",
                "output_preview": "ok",
                "metadata": {"runtime_trajectory": {"exit_reason": "completed"}},
            },
            "spans": [
                {
                    "span_kind": "tool_execution",
                    "name": "lookup",
                    "status": "succeeded",
                    "sequence_no": 1,
                }
            ],
            "events": [],
        },
        "usage": {"input_tokens": 60, "output_tokens": 40, "total_tokens": 100},
        "fingerprint": {
            "system_prompt_hash": "prompt-a",
            "tool_schema_hash": "tools-a",
            "model_id": "qwen3.7-plus",
            "provider": "dashscope",
            "runtime_revision": "runtime-a",
        },
        "contract_result": {
            "passed": True,
            "trajectory_pass": True,
            "stateful_pass": None,
            "failures": [],
        },
    }


@pytest.mark.asyncio
async def test_live_executor_runs_each_trial_once_and_reuses_trace_for_evaluator_suite() -> None:
    repository = _LiveRepository(
        [
            _run_case("critical", 1),
            _run_case("critical", 2),
            _run_case("normal", 1),
            _run_case("normal", 2),
        ]
    )
    calls: list[str] = []

    async def run_candidate(**kwargs: Any) -> dict[str, Any]:
        calls.append(kwargs["run_case"]["run_case_id"])
        return _candidate_result(kwargs["run_case"])

    result = await EvaluatorExecutor(
        repository,  # type: ignore[arg-type]
        candidate_run=run_candidate,
    ).run_job(
        tenant_id="tenant-a",
        job_payload={
            "run_id": "run-live",
            "evaluator_id": "rule-a",
            "run_mode": "live_candidate",
        },
    )

    assert result.status == "succeeded"
    assert len(calls) == 4
    assert len(repository.scores) == 12
    final = repository.run_updates[-1]
    assert final["metrics"]["attempted_trials"] == 4
    assert final["metrics"]["latency_p50_ms"] == 150
    assert final["metrics"]["total_tokens_per_task"] == 100
    assert final["score_summary"]["behavior_pass_rate"] == 1.0
    assert final["score_summary"]["schema_version"] == "eval-gate-metrics/v2"
    assert final["score_summary"]["score_sum"] == 2.0
    assert final["score_summary"]["trajectory_case_count"] == 2
    assert final["score_summary"]["trajectory_failed_count"] == 0
    assert final["score_summary"]["trajectory_pass_rate"] == 1.0
    assert final["metrics"]["mixed_runtime"] is False
    assert all(case["status"] == "succeeded" for case in repository.cases)


@pytest.mark.asyncio
async def test_live_gate_requires_critical_and_mandatory_safety_cases() -> None:
    async def run_candidate(**kwargs: Any) -> dict[str, Any]:
        return _candidate_result(kwargs["run_case"])

    missing = _LiveRepository([_run_case("normal", 1)])
    await EvaluatorExecutor(
        missing,  # type: ignore[arg-type]
        candidate_run=run_candidate,
    ).run_job(
        tenant_id="tenant-a",
        job_payload={
            "run_id": "run-live",
            "evaluator_id": "rule-a",
            "run_mode": "live_candidate",
        },
    )
    missing_metrics = missing.run_updates[-1]["metrics"]
    assert missing_metrics["critical_case_count"] == 0
    assert missing_metrics["hard_blockers_passed"] is False
    assert missing_metrics["gate"]["status"] == "fail"

    covered = _LiveRepository(
        [_run_case(case_id, 1) for case_id in REQUIRED_ASSISTANT_HARD_BLOCKERS]
    )
    await EvaluatorExecutor(
        covered,  # type: ignore[arg-type]
        candidate_run=run_candidate,
    ).run_job(
        tenant_id="tenant-a",
        job_payload={
            "run_id": "run-live",
            "evaluator_id": "rule-a",
            "run_mode": "live_candidate",
        },
    )
    covered_metrics = covered.run_updates[-1]["metrics"]
    assert covered_metrics["critical_case_count"] == 2
    assert covered_metrics["hard_blockers_passed"] is True
    assert covered_metrics["gate"]["status"] == "pass"


@pytest.mark.asyncio
async def test_live_executor_retry_reuses_completed_trial_without_candidate_call() -> None:
    completed = _run_case("critical", 1)
    completed.update(
        {
            "status": "succeeded",
            "candidate_trace_id": "trace-existing",
            "observed_metrics": {
                "case_id": "critical",
                "trial_index": 1,
                "critical": True,
                "trace_id": "trace-existing",
                "execution_succeeded": True,
                "behavior_pass": True,
                "aggregate_score": 1.0,
                "latency_ms": 100,
                "input_tokens": 60,
                "output_tokens": 40,
                "total_tokens": 100,
                "cost_cents": 0.1,
                "fingerprint": {
                    "system_prompt_hash": "prompt-a",
                    "tool_schema_hash": "tools-a",
                    "model_id": "qwen3.7-plus",
                    "provider": "dashscope",
                    "runtime_revision": "runtime-a",
                },
            },
        }
    )
    pending = _run_case("critical", 2)
    repository = _LiveRepository([completed, pending])
    calls: list[str] = []

    async def run_candidate(**kwargs: Any) -> dict[str, Any]:
        calls.append(kwargs["run_case"]["run_case_id"])
        return _candidate_result(kwargs["run_case"])

    result = await EvaluatorExecutor(
        repository,  # type: ignore[arg-type]
        candidate_run=run_candidate,
    ).run_job(
        tenant_id="tenant-a",
        job_payload={
            "run_id": "run-live",
            "evaluator_id": "rule-a",
            "run_mode": "live_candidate",
        },
    )

    assert result.status == "succeeded"
    assert calls == ["critical-2"]


@pytest.mark.asyncio
async def test_live_executor_does_not_verify_partial_fingerprint() -> None:
    repository = _LiveRepository([_run_case("critical", 1)])

    async def run_candidate(**kwargs: Any) -> dict[str, Any]:
        candidate = _candidate_result(kwargs["run_case"])
        candidate["fingerprint"] = {}
        return candidate

    result = await EvaluatorExecutor(
        repository,  # type: ignore[arg-type]
        candidate_run=run_candidate,
    ).run_job(
        tenant_id="tenant-a",
        job_payload={
            "run_id": "run-live",
            "evaluator_id": "rule-a",
            "run_mode": "live_candidate",
        },
    )

    assert result.status == "succeeded"
    final_metrics = repository.run_updates[-1]["metrics"]
    assert final_metrics["fingerprint_complete"] is False
    assert final_metrics["actual_fingerprint"] == {}
