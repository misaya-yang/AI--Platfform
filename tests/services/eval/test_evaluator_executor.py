from __future__ import annotations

from typing import Any

import pytest
from ai_gateway_core.eval.evaluator_executor import EvaluatorExecutor


class FakeEvalRepository:
    def __init__(self) -> None:
        self.calls: list[tuple[str, dict[str, Any]]] = []
        self.examples: list[dict[str, Any]] = []
        self.evaluator = {
            "evaluator_id": "eval-1",
            "name": "latency",
            "evaluator_type": "rule",
            "version": "v1",
            "filter_config": {
                "rules": [
                    {"type": "status_eq", "value": "succeeded"},
                    {"type": "latency_ms_lt", "value": 2000},
                ]
            },
        }
        self.trace_detail = {
            "trace": {
                "trace_id": "trace-1",
                "input_preview": "hello",
                "output_preview": "world",
                "status": "succeeded",
                "total_latency_ms": 900,
                "model_id": "test-model",
                "metadata": {},
            }
        }

    async def update_experiment_run(self, **kwargs: Any) -> None:
        self.calls.append(("update_experiment_run", kwargs))

    async def get_evaluator(self, **kwargs: Any) -> dict[str, Any] | None:
        self.calls.append(("get_evaluator", kwargs))
        return self.evaluator

    async def get_trace_detail(self, **kwargs: Any) -> dict[str, Any] | None:
        self.calls.append(("get_trace_detail", kwargs))
        trace_id = str(kwargs.get("trace_id") or "")
        if trace_id == "trace-2":
            return {
                "trace": {
                    "trace_id": "trace-2",
                    "input_preview": "hello",
                    "output_preview": "",
                    "status": "failed",
                    "total_latency_ms": 5000,
                    "model_id": "test-model",
                    "metadata": {},
                }
            }
        return self.trace_detail

    async def list_examples(self, **kwargs: Any) -> tuple[list[dict[str, Any]], int]:
        self.calls.append(("list_examples", kwargs))
        return self.examples, len(self.examples)

    async def create_eval_score(self, **kwargs: Any) -> dict[str, Any] | None:
        self.calls.append(("create_eval_score", kwargs))
        return {
            "score_id": "score-1",
            "numeric_value": kwargs["payload"]["numeric_value"],
        }


@pytest.mark.asyncio
async def test_rule_evaluator_scores_trace_target() -> None:
    repo = FakeEvalRepository()
    executor = EvaluatorExecutor(repo)

    result = await executor.run_job(
        tenant_id="tenant-a",
        job_payload={
            "run_id": "run-1",
            "evaluator_id": "eval-1",
            "trace_id": "trace-1",
        },
    )

    assert result.status == "succeeded"
    assert result.scores_written == 1
    assert result.score_summary["scored_count"] == 1
    assert repo.calls[0][0] == "update_experiment_run"
    assert any(call[0] == "create_eval_score" for call in repo.calls)


@pytest.mark.asyncio
async def test_human_evaluator_marks_pending_without_scores() -> None:
    repo = FakeEvalRepository()
    repo.evaluator["evaluator_type"] = "human"
    executor = EvaluatorExecutor(repo)

    result = await executor.run_job(
        tenant_id="tenant-a",
        job_payload={
            "run_id": "run-2",
            "evaluator_id": "eval-1",
            "trace_id": "trace-1",
        },
    )

    assert result.status == "succeeded"
    assert result.score_summary["pending_human"] is True
    assert result.scores_written == 0
    assert not any(call[0] == "create_eval_score" for call in repo.calls)


@pytest.mark.asyncio
async def test_rule_evaluator_scores_dataset_examples_with_example_target() -> None:
    repo = FakeEvalRepository()
    repo.examples = [
        {
            "example_id": "example-1",
            "source_trace_id": "trace-1",
            "expected_output": {"output_preview": "world"},
        },
        {
            "example_id": "example-2",
            "source_trace_id": "trace-2",
            "expected_output": {"output_preview": "missing"},
        },
    ]
    repo.trace_detail = {
        "trace": {
            "trace_id": "trace-1",
            "input_preview": "hello",
            "output_preview": "world",
            "status": "succeeded",
            "total_latency_ms": 900,
            "model_id": "test-model",
            "metadata": {},
        }
    }
    executor = EvaluatorExecutor(repo)

    result = await executor.run_job(
        tenant_id="tenant-a",
        job_payload={
            "run_id": "run-3",
            "evaluator_id": "eval-1",
            "dataset_id": "dataset-1",
            "trace_family": "assistant",
        },
    )

    assert result.status == "succeeded"
    assert result.scores_written == 2
    assert result.score_summary["target_count"] == 2
    score_calls = [call for call in repo.calls if call[0] == "create_eval_score"]
    assert score_calls[0][1]["trace_family"] == "assistant"
    assert score_calls[0][1]["payload"]["target_type"] == "example"


@pytest.mark.asyncio
async def test_llm_evaluator_uses_injected_complete_and_writes_scores() -> None:
    repo = FakeEvalRepository()
    repo.evaluator["evaluator_type"] = "llm"
    repo.evaluator["metadata"] = {"judge_model_id": "judge-model"}

    async def _complete(model_id: str, prompt: str) -> str:
        assert model_id == "judge-model"
        assert "hello" in prompt
        return '{"numeric_value": 0.91, "label": "pass", "explanation": "grounded", "confidence": 0.88}'

    executor = EvaluatorExecutor(repo, llm_complete=_complete)

    result = await executor.run_job(
        tenant_id="tenant-a",
        job_payload={
            "run_id": "run-4",
            "evaluator_id": "eval-1",
            "trace_id": "trace-1",
            "target_snapshot": {"trace_family": "rag"},
        },
    )

    assert result.status == "succeeded"
    assert result.scores_written == 1
    assert result.score_summary["average_score"] == 0.91
    detail_calls = [call for call in repo.calls if call[0] == "get_trace_detail"]
    assert detail_calls[0][1]["trace_family"] == "rag"
    score_calls = [call for call in repo.calls if call[0] == "create_eval_score"]
    assert score_calls[0][1]["trace_family"] == "rag"
    assert score_calls[0][1]["payload"]["scorer_type"] == "llm"


@pytest.mark.asyncio
async def test_evaluator_run_transitions_queued_running_succeeded() -> None:
    repo = FakeEvalRepository()
    executor = EvaluatorExecutor(repo)

    await executor.run_job(
        tenant_id="tenant-a",
        job_payload={
            "run_id": "run-5",
            "evaluator_id": "eval-1",
            "trace_id": "trace-1",
        },
    )

    statuses = [
        call[1]["status"]
        for call in repo.calls
        if call[0] == "update_experiment_run"
    ]
    assert statuses[0] == "running"
    assert statuses[-1] == "succeeded"
