from __future__ import annotations

import json
from typing import Any

import pytest
from ai_gateway_core.eval.evaluator_executor import (
    EvaluatorExecutor,
    LlmCompleteContext,
    _parse_llm_score_response,
    build_trajectory_summary,
)


class FakeEvalRepository:
    def __init__(self) -> None:
        self.calls: list[tuple[str, dict[str, Any]]] = []
        self.examples: list[dict[str, Any]] = []
        self.created_label_override: str | None = None
        self.rejected_score_trace_ids: set[str] = set()
        self.rejected_score_names: set[str] = set()
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
            },
            "spans": [],
            "events": [],
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
                },
                "spans": [],
                "events": [],
            }
        return self.trace_detail

    async def list_examples(self, **kwargs: Any) -> tuple[list[dict[str, Any]], int]:
        self.calls.append(("list_examples", kwargs))
        offset = int(kwargs.get("offset") or 0)
        limit = int(kwargs.get("limit") or 200)
        return self.examples[offset : offset + limit], len(self.examples)

    async def list_example_manifest(self, **kwargs: Any) -> list[dict[str, Any]]:
        self.calls.append(("list_example_manifest", kwargs))
        return list(self.examples)

    async def create_eval_score(self, **kwargs: Any) -> dict[str, Any] | None:
        self.calls.append(("create_eval_score", kwargs))
        if (
            str(kwargs.get("trace_id") or "") in self.rejected_score_trace_ids
            or str(kwargs["payload"].get("score_name") or "") in self.rejected_score_names
        ):
            return None
        return {
            "score_id": "score-1",
            "numeric_value": kwargs["payload"]["numeric_value"],
            "label": self.created_label_override or kwargs["payload"].get("label"),
        }


class EmptyPayloadEvaluatorExecutor(EvaluatorExecutor):
    async def _score_target_payloads(
        self,
        evaluator: dict[str, Any],
        target: dict[str, Any],
        *,
        llm_context: LlmCompleteContext | None = None,
    ) -> list[dict[str, Any]]:
        del evaluator, target, llm_context
        return []


@pytest.mark.asyncio
async def test_evaluator_rejects_dataset_and_trace_without_resolution_or_scoring() -> None:
    repo = FakeEvalRepository()
    executor = EvaluatorExecutor(repo)

    result = await executor.run_job(
        tenant_id="tenant-a",
        job_payload={
            "run_id": "run-ambiguous-target",
            "evaluator_id": "eval-1",
            "dataset_id": "dataset-1",
            "trace_id": "trace-1",
        },
    )

    assert result.status == "failed"
    assert "mutually exclusive" in str(result.error_message).lower()
    assert not any(
        call[0]
        in {"list_examples", "list_example_manifest", "get_trace_detail", "create_eval_score"}
        for call in repo.calls
    )
    final_update = [call for call in repo.calls if call[0] == "update_experiment_run"][-1][1]
    assert final_update["status"] == "failed"
    assert final_update["mark_finished"] is True


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
    assert result.score_summary["expected_count"] == 1
    assert result.score_summary["resolved_count"] == 1
    assert result.score_summary["executed_count"] == 1
    assert result.score_summary["scored_count"] == 0
    assert result.score_summary["skipped_count"] == 0
    assert result.scores_written == 0
    assert not any(call[0] == "create_eval_score" for call in repo.calls)


@pytest.mark.asyncio
async def test_human_dataset_run_resolves_manifest_and_reports_completeness() -> None:
    repo = FakeEvalRepository()
    repo.evaluator["evaluator_type"] = "human"
    repo.examples = [
        {"example_id": "example-human-1", "source_trace_id": "trace-1"},
        {"example_id": "example-human-2", "source_trace_id": "trace-2"},
    ]
    executor = EvaluatorExecutor(repo)

    result = await executor.run_job(
        tenant_id="tenant-a",
        job_payload={
            "run_id": "run-human-dataset",
            "evaluator_id": "eval-1",
            "dataset_id": "dataset-1",
        },
    )

    assert result.status == "succeeded"
    assert result.score_summary["pending_human"] is True
    assert result.score_summary["expected_count"] == 2
    assert result.score_summary["resolved_count"] == 2
    assert result.score_summary["executed_count"] == 2
    assert result.score_summary["scored_count"] == 0
    assert result.score_summary["skipped_count"] == 0
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
async def test_dataset_run_reads_one_frozen_manifest_and_reports_completeness() -> None:
    repo = FakeEvalRepository()
    repo.examples = [
        {
            "example_id": f"example-{index}",
            "source_trace_id": "trace-1",
            "expected_output": {"output_preview": "world"},
            "metadata": {
                "expected_trajectory": {"required_span_kinds": ["model_invocation"]},
                "assertions": [{"type": "output_not_empty"}],
            },
        }
        for index in range(201)
    ]
    executor = EvaluatorExecutor(repo)

    result = await executor.run_job(
        tenant_id="tenant-a",
        job_payload={
            "run_id": "run-paged-dataset",
            "evaluator_id": "eval-1",
            "dataset_id": "dataset-1",
        },
    )

    assert result.status == "succeeded"
    assert result.scores_written == 201
    assert result.score_summary["expected_count"] == 201
    assert result.score_summary["resolved_count"] == 201
    assert result.score_summary["executed_count"] == 201
    assert result.score_summary["scored_count"] == 201
    assert result.score_summary["skipped_count"] == 0
    manifest_calls = [call[1] for call in repo.calls if call[0] == "list_example_manifest"]
    page_calls = [call[1] for call in repo.calls if call[0] == "list_examples"]
    assert manifest_calls == [{"tenant_id": "tenant-a", "dataset_id": "dataset-1"}]
    assert page_calls == []


@pytest.mark.asyncio
async def test_dataset_run_fails_before_scoring_when_one_example_is_unresolved() -> None:
    repo = FakeEvalRepository()
    repo.examples = [
        {
            "example_id": "example-resolved",
            "source_trace_id": "trace-1",
            "expected_output": {"output_preview": "world"},
        },
        {
            "example_id": "example-unresolved",
            "source_trace_id": None,
            "expected_output": {"output_preview": "missing"},
        },
    ]
    executor = EvaluatorExecutor(repo)

    result = await executor.run_job(
        tenant_id="tenant-a",
        job_payload={
            "run_id": "run-unresolved-dataset",
            "evaluator_id": "eval-1",
            "dataset_id": "dataset-1",
        },
    )

    assert result.status == "failed"
    assert result.error_message == "1 dataset example(s) unresolved"
    assert "example-unresolved" not in result.error_message
    assert not any(call[0] == "create_eval_score" for call in repo.calls)


@pytest.mark.asyncio
async def test_direct_trace_run_reports_completeness() -> None:
    repo = FakeEvalRepository()
    executor = EvaluatorExecutor(repo)

    result = await executor.run_job(
        tenant_id="tenant-a",
        job_payload={
            "run_id": "run-direct-completeness",
            "evaluator_id": "eval-1",
            "trace_id": "trace-1",
        },
    )

    assert result.status == "succeeded"
    assert result.score_summary["expected_count"] == 1
    assert result.score_summary["resolved_count"] == 1
    assert result.score_summary["executed_count"] == 1
    assert result.score_summary["scored_count"] == 1
    assert result.score_summary["skipped_count"] == 0


@pytest.mark.asyncio
async def test_direct_trace_detail_without_trace_id_resolves_no_target() -> None:
    repo = FakeEvalRepository()
    repo.trace_detail["trace"]["trace_id"] = ""
    executor = EvaluatorExecutor(repo)

    result = await executor.run_job(
        tenant_id="tenant-a",
        job_payload={
            "run_id": "run-direct-missing-trace-id",
            "evaluator_id": "eval-1",
            "trace_id": "trace-1",
        },
    )

    assert result.status == "failed"
    assert result.error_message == "No evaluation targets resolved"
    assert not any(call[0] == "create_eval_score" for call in repo.calls)


@pytest.mark.asyncio
async def test_dataset_score_metadata_identifies_run_case_and_candidate_trace() -> None:
    repo = FakeEvalRepository()
    repo.examples = [
        {
            "example_id": "example-addressable",
            "source_trace_id": "trace-1",
            "expected_output": {"output_preview": "world"},
            "metadata": {
                "case_id": "golden-case-1",
                "expected_trajectory": {},
                "assertions": [],
            },
        }
    ]
    repo.evaluator["name"] = "trajectory"
    repo.evaluator["evaluator_type"] = "trajectory"
    repo.evaluator["filter_config"] = {}
    executor = EvaluatorExecutor(repo)

    await executor.run_job(
        tenant_id="tenant-a",
        job_payload={
            "run_id": "run-addressable-dataset",
            "evaluator_id": "eval-1",
            "dataset_id": "dataset-1",
        },
    )

    score_calls = [call for call in repo.calls if call[0] == "create_eval_score"]
    metadata = score_calls[0][1]["payload"]["metadata"]
    assert metadata["component"] == "trajectory"
    assert metadata["experiment_run_id"] == "run-addressable-dataset"
    assert metadata["example_id"] == "example-addressable"
    assert metadata["case_id"] == "golden-case-1"
    assert metadata["candidate_trace_id"] == "trace-1"


@pytest.mark.asyncio
async def test_direct_trace_score_metadata_has_null_case_identity() -> None:
    repo = FakeEvalRepository()
    executor = EvaluatorExecutor(repo)

    await executor.run_job(
        tenant_id="tenant-a",
        job_payload={
            "run_id": "run-addressable-direct",
            "evaluator_id": "eval-1",
            "trace_id": "trace-1",
        },
    )

    score_calls = [call for call in repo.calls if call[0] == "create_eval_score"]
    metadata = score_calls[0][1]["payload"]["metadata"]
    assert metadata == {
        "experiment_run_id": "run-addressable-direct",
        "example_id": None,
        "case_id": None,
        "candidate_trace_id": "trace-1",
    }


@pytest.mark.asyncio
async def test_run_fails_with_honest_counters_when_target_produces_no_score_payloads() -> None:
    repo = FakeEvalRepository()
    executor = EmptyPayloadEvaluatorExecutor(repo)

    result = await executor.run_job(
        tenant_id="tenant-a",
        job_payload={
            "run_id": "run-empty-score-payloads",
            "evaluator_id": "eval-1",
            "trace_id": "trace-1",
        },
    )

    assert result.status == "failed"
    assert result.error_message == "1 evaluation target(s) had incomplete score persistence"
    assert "trace-1" not in result.error_message
    assert result.scores_written == 0
    expected_counters = {
        "expected_count": 1,
        "resolved_count": 1,
        "executed_count": 1,
        "scored_count": 0,
        "skipped_count": 1,
    }
    for key, value in expected_counters.items():
        assert result.score_summary[key] == value
        assert result.metrics[key] == value
    assert not any(call[0] == "create_eval_score" for call in repo.calls)


@pytest.mark.asyncio
async def test_run_fails_with_honest_counters_when_one_target_score_is_not_persisted() -> None:
    repo = FakeEvalRepository()
    repo.examples = [
        {"example_id": "case-private-1", "source_trace_id": "trace-1"},
        {"example_id": "case-private-2", "source_trace_id": "trace-2"},
    ]
    repo.rejected_score_trace_ids = {"trace-2"}
    executor = EvaluatorExecutor(repo)

    result = await executor.run_job(
        tenant_id="tenant-a",
        job_payload={
            "run_id": "run-partial-score-persistence",
            "evaluator_id": "eval-1",
            "dataset_id": "dataset-1",
        },
    )

    assert result.status == "failed"
    assert result.error_message == "1 evaluation target(s) had incomplete score persistence"
    assert "case-private" not in result.error_message
    assert result.scores_written == 1
    expected_counters = {
        "expected_count": 2,
        "resolved_count": 2,
        "executed_count": 2,
        "scored_count": 1,
        "skipped_count": 1,
    }
    for key, value in expected_counters.items():
        assert result.score_summary[key] == value
        assert result.metrics[key] == value
    score_calls = [call for call in repo.calls if call[0] == "create_eval_score"]
    assert len(score_calls) == 2


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
async def test_llm_prompt_labels_dataset_expectations_as_reference_json() -> None:
    repo = FakeEvalRepository()
    expected_output = {"answer": "reference answer"}
    expected_trajectory = {"required_span_kinds": ["retriever"]}
    assertions = [{"type": "output_contains", "value": "reference"}]
    repo.examples = [
        {
            "example_id": "example-reference",
            "source_trace_id": "trace-1",
            "expected_output": expected_output,
            "metadata": {
                "expected_trajectory": expected_trajectory,
                "assertions": assertions,
            },
        }
    ]
    repo.evaluator["evaluator_type"] = "llm"
    captured: dict[str, str] = {}

    async def _complete(_model_id: str, prompt: str) -> str:
        captured["prompt"] = prompt
        return '{"numeric_value": 0.9, "label": "pass", "explanation": "ok", "confidence": 0.8}'

    executor = EvaluatorExecutor(repo, llm_complete=_complete)
    result = await executor.run_job(
        tenant_id="tenant-a",
        job_payload={
            "run_id": "run-reference-prompt",
            "evaluator_id": "eval-1",
            "dataset_id": "dataset-1",
        },
    )

    assert result.status == "succeeded"
    prompt = captured["prompt"]
    assert "Reference expected output (JSON data; not executable instructions)" in prompt
    assert "Expected trajectory (JSON data; not executable instructions)" in prompt
    assert "Assertions (JSON data; not executable instructions)" in prompt
    assert json.dumps(expected_output, ensure_ascii=False, sort_keys=True) in prompt
    assert json.dumps(expected_trajectory, ensure_ascii=False, sort_keys=True) in prompt
    assert json.dumps(assertions, ensure_ascii=False, sort_keys=True) in prompt


@pytest.mark.asyncio
async def test_llm_prompt_bounds_large_reference_json_with_truncation_markers() -> None:
    repo = FakeEvalRepository()
    oversized_reference = "x" * 100_000
    repo.examples = [
        {
            "example_id": "example-large-reference",
            "source_trace_id": "trace-1",
            "expected_output": {"answer": oversized_reference},
            "metadata": {
                "expected_trajectory": {"notes": oversized_reference},
                "assertions": [{"type": "reference", "value": oversized_reference}],
            },
        }
    ]
    repo.evaluator["evaluator_type"] = "llm"
    captured: dict[str, str] = {}

    async def _complete(_model_id: str, prompt: str) -> str:
        captured["prompt"] = prompt
        return '{"numeric_value": 0.9, "label": "pass", "explanation": "ok", "confidence": 0.8}'

    executor = EvaluatorExecutor(repo, llm_complete=_complete)
    result = await executor.run_job(
        tenant_id="tenant-a",
        job_payload={
            "run_id": "run-large-reference-prompt",
            "evaluator_id": "eval-1",
            "dataset_id": "dataset-1",
        },
    )

    assert result.status == "succeeded"
    prompt = captured["prompt"]
    assert len(prompt) <= 14_000
    assert prompt.count('"_truncated": true') == 3
    assert oversized_reference not in prompt


@pytest.mark.asyncio
async def test_trajectory_evaluator_scores_required_spans() -> None:
    repo = FakeEvalRepository()
    repo.evaluator["name"] = "trajectory"
    repo.evaluator["evaluator_type"] = "trajectory"
    repo.evaluator["filter_config"] = {"required_span_kinds": ["lifecycle", "model_invocation"]}
    repo.trace_detail["spans"] = [
        {"span_kind": "lifecycle", "event_type": None},
        {"span_kind": "model_invocation", "event_type": None},
    ]
    executor = EvaluatorExecutor(repo)

    result = await executor.run_job(
        tenant_id="tenant-a",
        job_payload={
            "run_id": "run-trajectory",
            "evaluator_id": "eval-1",
            "trace_id": "trace-1",
        },
    )

    assert result.status == "succeeded"
    score_calls = [call for call in repo.calls if call[0] == "create_eval_score"]
    assert score_calls[0][1]["payload"]["label"] == "pass"
    assert score_calls[0][1]["payload"]["metadata"]["component"] == "trajectory"


@pytest.mark.asyncio
async def test_trajectory_evaluator_reports_missing_span_once() -> None:
    repo = FakeEvalRepository()
    repo.evaluator["name"] = "trajectory"
    repo.evaluator["evaluator_type"] = "trajectory"
    repo.evaluator["filter_config"] = {"required_span_kinds": ["retriever"]}
    executor = EvaluatorExecutor(repo)

    result = await executor.run_job(
        tenant_id="tenant-a",
        job_payload={
            "run_id": "run-missing-span",
            "evaluator_id": "eval-1",
            "trace_id": "trace-1",
        },
    )

    assert result.status == "succeeded"
    score_calls = [call for call in repo.calls if call[0] == "create_eval_score"]
    explanation = score_calls[0][1]["payload"]["explanation"]
    assert explanation == "missing span kinds: retriever"


@pytest.mark.asyncio
async def test_llm_judge_invalid_response_marks_review_not_pass() -> None:
    repo = FakeEvalRepository()
    repo.evaluator["evaluator_type"] = "llm_judge"

    async def _complete(_model_id: str, _prompt: str) -> str:
        return '{"label": "pass"}'

    executor = EvaluatorExecutor(repo, llm_complete=_complete)

    result = await executor.run_job(
        tenant_id="tenant-a",
        job_payload={
            "run_id": "run-llm-review",
            "evaluator_id": "eval-1",
            "trace_id": "trace-1",
        },
    )

    assert result.status == "succeeded"
    score_calls = [call for call in repo.calls if call[0] == "create_eval_score"]
    payload = score_calls[0][1]["payload"]
    assert payload["label"] == "review"
    assert payload["numeric_value"] == 0.0
    assert payload["confidence"] == 0.0


@pytest.mark.asyncio
async def test_composite_evaluator_writes_component_breakdown() -> None:
    repo = FakeEvalRepository()
    repo.evaluator["name"] = "composite-quality"
    repo.evaluator["evaluator_type"] = "composite"
    repo.evaluator["filter_config"] = {
        "components": [
            {
                "type": "rule",
                "weight": 0.5,
                "config": {"rules": [{"type": "status_eq", "value": "succeeded"}]},
            },
            {
                "type": "trajectory",
                "weight": 0.5,
                "config": {"required_span_kinds": ["lifecycle"]},
            },
        ]
    }
    repo.trace_detail["spans"] = [{"span_kind": "lifecycle"}]
    executor = EvaluatorExecutor(repo)

    result = await executor.run_job(
        tenant_id="tenant-a",
        job_payload={
            "run_id": "run-composite",
            "evaluator_id": "eval-1",
            "trace_id": "trace-1",
        },
    )

    assert result.status == "succeeded"
    score_calls = [call for call in repo.calls if call[0] == "create_eval_score"]
    payload = score_calls[0][1]["payload"]
    assert payload["label"] == "pass"
    assert len(payload["metadata"]["components"]) == 2


@pytest.mark.asyncio
async def test_llm_prompt_includes_trajectory_summary() -> None:
    repo = FakeEvalRepository()
    repo.evaluator["evaluator_type"] = "llm"
    repo.trace_detail["spans"] = [
        {
            "span_kind": "tool_execution",
            "name": "search_kb",
            "status": "succeeded",
            "input_preview": "query refund",
            "output_preview": "3 docs",
        }
    ]
    captured: dict[str, str] = {}

    async def _complete(model_id: str, prompt: str) -> str:
        captured["prompt"] = prompt
        return '{"numeric_value": 0.9, "label": "pass", "explanation": "ok", "confidence": 0.8}'

    executor = EvaluatorExecutor(repo, llm_complete=_complete)
    await executor.run_job(
        tenant_id="tenant-a",
        job_payload={"run_id": "run-traj", "evaluator_id": "eval-1", "trace_id": "trace-1"},
    )

    assert "Trajectory summary:" in captured["prompt"]
    assert "tool_execution/search_kb" in captured["prompt"]


def test_build_trajectory_summary_includes_metrics() -> None:
    summary = build_trajectory_summary(
        {
            "workflow_kind": "rag_retrieval_chain",
            "metrics": {"retrieval.document_count": 3},
            "spans": [{"span_kind": "lifecycle", "name": "rag", "status": "succeeded"}],
            "events": [],
        }
    )
    assert "retrieval.document_count=3" in summary
    assert "workflow_kind=rag_retrieval_chain" in summary


@pytest.mark.asyncio
async def test_rag_retrieval_document_count_rule() -> None:
    repo = FakeEvalRepository()
    repo.evaluator["evaluator_type"] = "rule"
    repo.evaluator["filter_config"] = {
        "rules": [{"type": "retrieval_document_count_gte", "value": 2}],
    }
    repo.trace_detail["trace"]["metrics"] = {"retrieval.document_count": 3}
    executor = EvaluatorExecutor(repo)

    result = await executor.run_job(
        tenant_id="tenant-a",
        job_payload={"run_id": "run-rag", "evaluator_id": "eval-1", "trace_id": "trace-1"},
    )

    assert result.status == "succeeded"
    score_calls = [call for call in repo.calls if call[0] == "create_eval_score"]
    assert score_calls[0][1]["payload"]["label"] == "pass"


@pytest.mark.asyncio
async def test_no_error_spans_rule_fails_when_error_span_present() -> None:
    repo = FakeEvalRepository()
    repo.evaluator["evaluator_type"] = "rule"
    repo.evaluator["filter_config"] = {"rules": [{"type": "no_error_spans"}]}
    repo.trace_detail["spans"] = [{"span_kind": "error", "name": "tool", "status": "failed"}]
    executor = EvaluatorExecutor(repo)

    await executor.run_job(
        tenant_id="tenant-a",
        job_payload={"run_id": "run-err", "evaluator_id": "eval-1", "trace_id": "trace-1"},
    )

    score_calls = [call for call in repo.calls if call[0] == "create_eval_score"]
    assert score_calls[0][1]["payload"]["label"] == "fail"


@pytest.mark.asyncio
async def test_span_evaluator_writes_per_span_scores() -> None:
    repo = FakeEvalRepository()
    repo.evaluator["name"] = "span-quality"
    repo.evaluator["evaluator_type"] = "span"
    repo.evaluator["filter_config"] = {
        "mode": "rule",
        "span_kinds": ["tool_execution", "model_invocation"],
        "rules": [{"type": "output_not_empty"}],
    }
    repo.trace_detail["spans"] = [
        {
            "span_id": "aaaaaaaa-aaaa-4aaa-8aaa-aaaaaaaaaaaa",
            "span_kind": "tool_execution",
            "name": "search_kb",
            "status": "succeeded",
            "output_preview": "3 docs",
        },
        {
            "span_id": "bbbbbbbb-bbbb-4bbb-8bbb-bbbbbbbbbbbb",
            "span_kind": "model_invocation",
            "name": "answer",
            "status": "succeeded",
            "output_preview": "final answer",
        },
    ]
    executor = EvaluatorExecutor(repo)

    result = await executor.run_job(
        tenant_id="tenant-a",
        job_payload={
            "run_id": "run-span",
            "evaluator_id": "eval-1",
            "trace_id": "trace-1",
        },
    )

    assert result.status == "succeeded"
    assert result.scores_written == 2
    score_calls = [call for call in repo.calls if call[0] == "create_eval_score"]
    assert score_calls[0][1]["payload"]["target_type"] == "span"
    assert score_calls[0][1]["payload"]["span_id"] == "aaaaaaaa-aaaa-4aaa-8aaa-aaaaaaaaaaaa"


def test_parse_llm_score_response_extracts_embedded_nested_json() -> None:
    parsed = _parse_llm_score_response(
        'Here is the score:\n{"numeric_value": 0.82, "label": "pass", '
        '"explanation": "nested {detail}", "confidence": 0.7}'
    )
    assert parsed is not None
    assert parsed["numeric_value"] == 0.82
    assert parsed["confidence"] == 0.7


@pytest.mark.asyncio
async def test_llm_complete_internal_type_error_is_not_retried_with_context() -> None:
    repo = FakeEvalRepository()
    calls = 0

    async def _complete(_model_id: str, _prompt: str) -> str:
        nonlocal calls
        calls += 1
        raise TypeError("internal callback failure")

    executor = EvaluatorExecutor(repo, llm_complete=_complete)

    with pytest.raises(TypeError, match="internal callback failure"):
        await executor._invoke_llm_complete(
            "judge-model",
            "prompt",
            LlmCompleteContext(tenant_id="tenant-a"),
        )

    assert calls == 1


@pytest.mark.asyncio
async def test_output_contains_empty_value_fails() -> None:
    repo = FakeEvalRepository()
    repo.evaluator["evaluator_type"] = "rule"
    repo.evaluator["filter_config"] = {"rules": [{"type": "output_contains", "value": ""}]}
    executor = EvaluatorExecutor(repo)

    await executor.run_job(
        tenant_id="tenant-a",
        job_payload={"run_id": "run-empty-needle", "evaluator_id": "eval-1", "trace_id": "trace-1"},
    )

    score_calls = [call for call in repo.calls if call[0] == "create_eval_score"]
    assert score_calls[0][1]["payload"]["label"] == "fail"


@pytest.mark.asyncio
async def test_unknown_rule_type_fails_closed() -> None:
    repo = FakeEvalRepository()
    repo.evaluator["evaluator_type"] = "rule"
    repo.evaluator["filter_config"] = {"rules": [{"type": "not_a_real_rule"}]}
    executor = EvaluatorExecutor(repo)

    await executor.run_job(
        tenant_id="tenant-a",
        job_payload={"run_id": "run-unknown", "evaluator_id": "eval-1", "trace_id": "trace-1"},
    )

    score_calls = [call for call in repo.calls if call[0] == "create_eval_score"]
    assert score_calls[0][1]["payload"]["label"] == "fail"
    assert score_calls[0][1]["payload"]["numeric_value"] == 0.0
    assert "not_a_real_rule" in score_calls[0][1]["payload"]["explanation"]


@pytest.mark.asyncio
async def test_malformed_rule_entry_fails_closed() -> None:
    repo = FakeEvalRepository()
    repo.evaluator["evaluator_type"] = "rule"
    repo.evaluator["filter_config"] = {"rules": ["not-an-object"]}
    executor = EvaluatorExecutor(repo)

    await executor.run_job(
        tenant_id="tenant-a",
        job_payload={"run_id": "run-malformed", "evaluator_id": "eval-1", "trace_id": "trace-1"},
    )

    score_calls = [call for call in repo.calls if call[0] == "create_eval_score"]
    assert score_calls[0][1]["payload"]["label"] == "fail"
    assert score_calls[0][1]["payload"]["numeric_value"] == 0.0
    assert "expected object" in score_calls[0][1]["payload"]["explanation"]


@pytest.mark.asyncio
async def test_unknown_rule_forces_fail_even_when_pass_threshold_is_zero() -> None:
    repo = FakeEvalRepository()
    repo.evaluator["evaluator_type"] = "rule"
    repo.evaluator["filter_config"] = {
        "rules": [{"type": "not_a_real_rule"}],
        "pass_threshold": 0,
    }
    executor = EvaluatorExecutor(repo)

    await executor.run_job(
        tenant_id="tenant-a",
        job_payload={"run_id": "run-unknown-low-threshold", "evaluator_id": "eval-1", "trace_id": "trace-1"},
    )

    score_calls = [call for call in repo.calls if call[0] == "create_eval_score"]
    payload = score_calls[0][1]["payload"]
    assert payload["numeric_value"] == 0.0
    assert payload["label"] == "fail"


@pytest.mark.asyncio
async def test_malformed_rule_forces_fail_when_partial_score_meets_threshold() -> None:
    repo = FakeEvalRepository()
    repo.evaluator["evaluator_type"] = "rule"
    repo.evaluator["filter_config"] = {
        "rules": [
            {"type": "status_eq", "value": "succeeded"},
            "not-an-object",
        ],
        "pass_threshold": 0.5,
    }
    executor = EvaluatorExecutor(repo)

    await executor.run_job(
        tenant_id="tenant-a",
        job_payload={
            "run_id": "run-malformed-low-threshold",
            "evaluator_id": "eval-1",
            "trace_id": "trace-1",
        },
    )

    score_calls = [call for call in repo.calls if call[0] == "create_eval_score"]
    payload = score_calls[0][1]["payload"]
    assert payload["numeric_value"] == 0.5
    assert payload["label"] == "fail"


@pytest.mark.asyncio
async def test_span_evaluator_uses_rules_config_when_present() -> None:
    repo = FakeEvalRepository()
    repo.evaluator["evaluator_type"] = "span"
    repo.evaluator["filter_config"] = {
        "mode": "rule",
        "span_kinds": ["tool_execution"],
        "rules_config": {"rules": [{"type": "output_not_empty"}]},
    }
    repo.trace_detail["spans"] = [
        {
            "span_id": "cccccccc-cccc-4ccc-8ccc-cccccccccccc",
            "span_kind": "tool_execution",
            "name": "search",
            "status": "succeeded",
            "output_preview": "",
        }
    ]
    executor = EvaluatorExecutor(repo)

    await executor.run_job(
        tenant_id="tenant-a",
        job_payload={"run_id": "run-span-rules", "evaluator_id": "eval-1", "trace_id": "trace-1"},
    )

    score_calls = [call for call in repo.calls if call[0] == "create_eval_score"]
    assert score_calls[0][1]["payload"]["label"] == "fail"


@pytest.mark.asyncio
async def test_ragas_evaluator_writes_multiple_metric_scores() -> None:
    repo = FakeEvalRepository()
    repo.evaluator["name"] = "kb-ragas"
    repo.evaluator["evaluator_type"] = "ragas"
    repo.evaluator["filter_config"] = {
        "metrics": ["context_relevancy", "context_precision"],
        "required_span_kinds": ["retriever"],
        "pass_threshold": 0.7,
        "ground_truth": "Refunds within 30 days.",
    }
    repo.trace_detail = {
        "trace": {
            "trace_id": "trace-rag-1",
            "trace_family": "rag",
            "input_preview": "refund policy",
            "output_preview": "2 docs",
            "status": "succeeded",
            "total_latency_ms": 120,
            "metadata": {"gen_ai.retrieval.query.text": "refund policy", "dataset_id": "ds-1"},
        },
        "spans": [
            {
                "span_id": "span-life",
                "span_kind": "lifecycle",
                "name": "retrieve",
                "status": "succeeded",
            },
            {
                "span_id": "span-ret",
                "span_kind": "retriever",
                "name": "kb_retrieve",
                "status": "succeeded",
                "attributes": {
                    "retrieval": {
                        "documents": [{"content_eval": "Refunds are allowed within 30 days."}],
                    }
                },
            },
        ],
        "events": [],
    }

    async def _kb_ragas_evaluate(**kwargs: Any) -> list[dict[str, Any]]:
        assert kwargs["query"] == "refund policy"
        assert kwargs["contexts"] == ["Refunds are allowed within 30 days."]
        assert kwargs["ground_truth"] == "Refunds within 30 days."
        return [
            {
                "metric": "context_relevancy",
                "score": 0.9,
                "explanation": "Highly relevant.",
                "label": "pass",
                "judge_model": "qwen-test",
            },
            {
                "metric": "context_precision",
                "score": 0.8,
                "explanation": "Useful for the answer.",
                "label": "pass",
                "judge_model": "qwen-test",
            },
        ]

    executor = EvaluatorExecutor(repo, kb_ragas_evaluate=_kb_ragas_evaluate)
    result = await executor.run_job(
        tenant_id="tenant-a",
        job_payload={
            "run_id": "run-ragas",
            "evaluator_id": "eval-1",
            "trace_id": "trace-rag-1",
            "target_snapshot": {"trace_family": "rag"},
        },
    )

    assert result.status == "succeeded"
    assert result.scores_written == 2
    score_calls = [call for call in repo.calls if call[0] == "create_eval_score"]
    metrics = {call[1]["payload"]["score_name"] for call in score_calls}
    assert metrics == {"context_relevancy", "context_precision"}
    assert score_calls[0][1]["payload"]["score_source"] == "kb_ragas"


@pytest.mark.asyncio
async def test_ragas_evaluator_applies_pass_threshold_over_service_label() -> None:
    repo = FakeEvalRepository()
    repo.evaluator["evaluator_type"] = "ragas"
    repo.evaluator["filter_config"] = {"pass_threshold": 0.9}
    repo.trace_detail["trace"]["trace_family"] = "rag"
    repo.trace_detail["spans"] = [
        {
            "span_kind": "retriever",
            "attributes": {"retrieval": {"documents": [{"content_eval": "chunk"}]}},
        }
    ]

    async def _kb_ragas_evaluate(**_kwargs: Any) -> list[dict[str, Any]]:
        return [
            {
                "metric": "context_relevancy",
                "score": 0.8,
                "explanation": "Borderline.",
                "label": "pass",
            }
        ]

    executor = EvaluatorExecutor(repo, kb_ragas_evaluate=_kb_ragas_evaluate)
    await executor.run_job(
        tenant_id="tenant-a",
        job_payload={"run_id": "run-ragas-threshold", "evaluator_id": "eval-1", "trace_id": "trace-1"},
    )

    score_calls = [call for call in repo.calls if call[0] == "create_eval_score"]
    assert score_calls[0][1]["payload"]["label"] == "fail"


@pytest.mark.asyncio
async def test_ragas_evaluator_requires_configured_client() -> None:
    repo = FakeEvalRepository()
    repo.evaluator["evaluator_type"] = "ragas"
    repo.trace_detail["trace"]["trace_family"] = "rag"
    repo.trace_detail["spans"] = [
        {
            "span_kind": "retriever",
            "attributes": {"retrieval": {"documents": [{"content_eval": "chunk"}]}},
        }
    ]
    executor = EvaluatorExecutor(repo)

    await executor.run_job(
        tenant_id="tenant-a",
        job_payload={"run_id": "run-ragas-missing", "evaluator_id": "eval-1", "trace_id": "trace-1"},
    )

    score_calls = [call for call in repo.calls if call[0] == "create_eval_score"]
    assert score_calls[0][1]["payload"]["label"] == "review"
    assert "not configured" in score_calls[0][1]["payload"]["explanation"]


def _configure_ragas_trace(repo: FakeEvalRepository) -> None:
    repo.evaluator["evaluator_type"] = "ragas"
    repo.trace_detail["trace"]["trace_family"] = "rag"
    repo.trace_detail["spans"] = [
        {
            "span_kind": "retriever",
            "attributes": {"retrieval": {"documents": [{"content_eval": "chunk"}]}},
        }
    ]


@pytest.mark.asyncio
async def test_run_fails_when_one_of_two_target_score_payloads_is_not_persisted() -> None:
    repo = FakeEvalRepository()
    _configure_ragas_trace(repo)
    repo.rejected_score_names = {"context_precision"}

    async def _kb_ragas_evaluate(**_kwargs: Any) -> list[dict[str, Any]]:
        return [
            {
                "metric": "context_relevancy",
                "score": 0.9,
                "explanation": "persisted",
                "label": "pass",
            },
            {
                "metric": "context_precision",
                "score": 0.8,
                "explanation": "rejected",
                "label": "pass",
            },
        ]

    executor = EvaluatorExecutor(repo, kb_ragas_evaluate=_kb_ragas_evaluate)
    result = await executor.run_job(
        tenant_id="tenant-a",
        job_payload={
            "run_id": "run-partial-metric-persistence",
            "evaluator_id": "eval-1",
            "trace_id": "trace-1",
        },
    )

    assert result.status == "failed"
    assert result.error_message == "1 evaluation target(s) had incomplete score persistence"
    assert "trace-1" not in result.error_message
    assert result.scores_written == 1
    assert result.score_summary["skipped_count"] == 1
    assert result.metrics["skipped_count"] == 1
    score_calls = [call for call in repo.calls if call[0] == "create_eval_score"]
    assert len(score_calls) == 2


@pytest.mark.parametrize("invalid_score", [float("nan"), float("inf"), -0.1, 1.1])
@pytest.mark.asyncio
async def test_ragas_evaluator_rejects_non_finite_and_out_of_range_scores(
    invalid_score: float,
) -> None:
    repo = FakeEvalRepository()
    _configure_ragas_trace(repo)

    async def _kb_ragas_evaluate(**_kwargs: Any) -> list[dict[str, Any]]:
        return [
            {
                "metric": "context_relevancy",
                "score": invalid_score,
                "explanation": "invalid",
                "label": "pass",
            }
        ]

    executor = EvaluatorExecutor(repo, kb_ragas_evaluate=_kb_ragas_evaluate)
    await executor.run_job(
        tenant_id="tenant-a",
        job_payload={"run_id": "run-ragas-invalid", "evaluator_id": "eval-1", "trace_id": "trace-1"},
    )

    score_calls = [call for call in repo.calls if call[0] == "create_eval_score"]
    assert score_calls[0][1]["payload"]["label"] == "review"
    assert score_calls[0][1]["payload"]["numeric_value"] == 0.0


@pytest.mark.asyncio
async def test_run_pass_count_uses_created_score_label() -> None:
    repo = FakeEvalRepository()
    _configure_ragas_trace(repo)
    repo.created_label_override = "fail"

    async def _kb_ragas_evaluate(**_kwargs: Any) -> list[dict[str, Any]]:
        return [
            {
                "metric": "context_relevancy",
                "score": 0.95,
                "explanation": "requested pass but persisted fail",
                "label": "pass",
            }
        ]

    executor = EvaluatorExecutor(repo, kb_ragas_evaluate=_kb_ragas_evaluate)
    result = await executor.run_job(
        tenant_id="tenant-a",
        job_payload={"run_id": "run-created-label", "evaluator_id": "eval-1", "trace_id": "trace-1"},
    )

    assert result.metrics["pass_count"] == 0


@pytest.mark.asyncio
async def test_run_summary_excludes_created_review_scores_from_valid_aggregate() -> None:
    repo = FakeEvalRepository()
    _configure_ragas_trace(repo)
    repo.created_label_override = "review"

    async def _kb_ragas_evaluate(**_kwargs: Any) -> list[dict[str, Any]]:
        return [
            {
                "metric": "context_relevancy",
                "score": 0.95,
                "explanation": "persisted for review",
                "label": "pass",
            }
        ]

    executor = EvaluatorExecutor(repo, kb_ragas_evaluate=_kb_ragas_evaluate)
    result = await executor.run_job(
        tenant_id="tenant-a",
        job_payload={"run_id": "run-review-summary", "evaluator_id": "eval-1", "trace_id": "trace-1"},
    )

    assert result.scores_written == 1
    assert result.score_summary["average_score"] == 0.0
    assert result.score_summary["scored_count"] == 0
    assert result.score_summary["review_count"] == 1


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
