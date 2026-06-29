from __future__ import annotations

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
async def test_unknown_rule_type_is_skipped_for_compatibility() -> None:
    repo = FakeEvalRepository()
    repo.evaluator["evaluator_type"] = "rule"
    repo.evaluator["filter_config"] = {"rules": [{"type": "not_a_real_rule"}]}
    executor = EvaluatorExecutor(repo)

    await executor.run_job(
        tenant_id="tenant-a",
        job_payload={"run_id": "run-unknown", "evaluator_id": "eval-1", "trace_id": "trace-1"},
    )

    score_calls = [call for call in repo.calls if call[0] == "create_eval_score"]
    assert score_calls[0][1]["payload"]["label"] == "pass"
    assert score_calls[0][1]["payload"]["numeric_value"] == 1.0
    assert "skipped" in score_calls[0][1]["payload"]["explanation"]


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
