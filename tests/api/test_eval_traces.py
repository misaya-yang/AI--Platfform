from __future__ import annotations

import json
from datetime import datetime, timezone
from pathlib import Path
from types import SimpleNamespace
from typing import Any

import pytest
from ai_gateway_core.persistence.repositories.agent_trace_repository import (
    AgentTraceRepository,
)
from fastapi import HTTPException

from src.api.deps import AuthContext
from src.api.schemas.eval import (
    AgentTraceScoreCreate,
    EvalDatasetCreate,
    EvalEvaluatorCreate,
    EvalEvaluatorRunRequest,
    EvalExampleFromTraceCreate,
    EvalExamplesImportRequest,
    EvalExampleUpdate,
    EvalExperimentCreate,
    EvalExperimentRunCreate,
    EvalGateDryRunRequest,
)
from src.api.v1 import eval as eval_routes
from src.api.v1.eval import (
    compare_eval_experiment_runs,
    create_eval_dataset,
    create_eval_evaluator,
    create_eval_example_from_trace,
    create_eval_experiment,
    create_eval_trace_score,
    dry_run_eval_gate,
    export_eval_examples,
    export_eval_trace,
    get_eval_dashboard,
    get_eval_dataset,
    get_eval_evaluator,
    get_eval_experiment,
    get_eval_experiment_run,
    get_eval_summary,
    get_eval_trace_detail,
    get_eval_trace_thread,
    import_eval_examples,
    list_eval_datasets,
    list_eval_evaluators,
    list_eval_examples,
    list_eval_experiments,
    list_eval_traces,
    run_eval_evaluator_async,
    run_eval_experiment,
    update_eval_example,
)
from src.config.settings import Settings
from src.core.auth.permissions import Capability
from src.core.auth.rbac import RBAC
from src.main import create_app


def _request() -> SimpleNamespace:
    settings = Settings()
    request = SimpleNamespace()
    request.app = SimpleNamespace()
    request.app.state = SimpleNamespace()
    request.app.state.dispatcher = SimpleNamespace(rbac=RBAC(role_permissions=settings.rbac.roles))
    request.app.state.database = SimpleNamespace(enabled=True)
    request.state = SimpleNamespace(request_id="req-eval-trace")
    request.headers = {}
    request.url = SimpleNamespace(path="/api/v1/eval/traces")
    return request


def _auth(
    *,
    user_id: str = "user-a",
    tenant_id: str = "tenant-a",
    roles: list[str] | None = None,
    permissions: list[str] | None = None,
) -> AuthContext:
    return AuthContext(
        user_id=user_id,
        tenant_id=tenant_id,
        roles=roles or ["user"],
        permissions=permissions or ["console:eval:view"],
        is_authenticated=True,
    )


def _trace_row(**overrides: Any) -> dict[str, Any]:
    now = datetime.now(timezone.utc)
    row = {
        "trace_id": "11111111-1111-4111-8111-111111111111",
        "trace_family": "assistant",
        "workflow_kind": "ai_assistant_chat",
        "tenant_id": "tenant-a",
        "user_id": "user-a",
        "thread_id": "session-a",
        "session_id": "session-a",
        "run_id": "run-a",
        "request_id": "request-a",
        "model_id": "qwen3.6-plus",
        "provider": "qwen",
        "status": "succeeded",
        "started_at": now,
        "ended_at": now,
        "first_token_latency_ms": 120,
        "total_latency_ms": 980,
        "input_tokens": 10,
        "output_tokens": 20,
        "total_tokens": 30,
        "total_cost_cents": 0,
        "input_preview": "hello",
        "output_preview": "hi",
        "redaction_state": {"input": "preview"},
        "metadata": {"redacted": True},
        "metrics": {"total_latency_ms": 980},
        "privacy": {"payloads": "bounded_redacted_preview"},
        "source_adapter": "assistant-service",
        "scores_count": 0,
        "created_at": now,
        "updated_at": now,
    }
    row.update(overrides)
    return row


def _score_row(**overrides: Any) -> dict[str, Any]:
    row = {
        "score_id": "22222222-2222-4222-8222-222222222222",
        "trace_id": "11111111-1111-4111-8111-111111111111",
        "span_id": None,
        "score_name": "quality",
        "score_type": "numeric",
        "numeric_value": 0.9,
        "boolean_value": None,
        "categorical_value": None,
        "text_value": None,
        "label": "good",
        "explanation": "grounded answer",
        "scorer_type": "human",
        "evaluator_version": None,
        "target_type": "trace",
        "target_id": "11111111-1111-4111-8111-111111111111",
        "evaluator_id": None,
        "evaluator_name": None,
        "score_source": "human",
        "confidence": None,
        "created_by": "user-a",
        "metadata": {},
        "created_at": datetime.now(timezone.utc),
    }
    row.update(overrides)
    return row


class FakeTraceRepository:
    def __init__(self) -> None:
        self.calls: list[tuple[str, dict[str, Any]]] = []
        self.detail: dict[str, Any] | None = {
            "trace": _trace_row(scores_count=1),
            "spans": [],
            "events": [],
            "scores": [_score_row()],
        }
        self.score: dict[str, Any] | None = _score_row()
        self.dataset = {
            "dataset_id": "aaaaaaaa-aaaa-4aaa-8aaa-aaaaaaaaaaaa",
            "tenant_id": "tenant-a",
            "name": "assistant-regression",
            "description": "regression set",
            "version": "v1",
            "schema": {},
            "metadata": {},
            "created_by": "user-a",
            "created_at": datetime.now(timezone.utc),
            "updated_at": datetime.now(timezone.utc),
        }
        self.example = {
            "example_id": "bbbbbbbb-bbbb-4bbb-8bbb-bbbbbbbbbbbb",
            "dataset_id": self.dataset["dataset_id"],
            "tenant_id": "tenant-a",
            "split": "regression",
            "input": {"input_preview": "hello"},
            "expected_output": {"output_preview": "hi"},
            "metadata": {},
            "source_trace_id": "11111111-1111-4111-8111-111111111111",
            "source_span_id": None,
            "created_by": "user-a",
            "created_at": datetime.now(timezone.utc),
        }
        self.evaluator = {
            "evaluator_id": "cccccccc-cccc-4ccc-8ccc-cccccccccccc",
            "tenant_id": "tenant-a",
            "name": "quality",
            "evaluator_type": "human",
            "rubric": "score quality",
            "version": "v1",
            "sampling_config": {},
            "filter_config": {},
            "metadata": {},
            "created_by": "user-a",
            "created_at": datetime.now(timezone.utc),
            "updated_at": datetime.now(timezone.utc),
        }
        self.experiment = {
            "experiment_id": "dddddddd-dddd-4ddd-8ddd-dddddddddddd",
            "tenant_id": "tenant-a",
            "dataset_id": self.dataset["dataset_id"],
            "name": "baseline",
            "description": "",
            "target_config": {"model": "qwen"},
            "metadata": {},
            "created_by": "user-a",
            "created_at": datetime.now(timezone.utc),
            "updated_at": datetime.now(timezone.utc),
            "runs": [],
        }
        self.baseline_run = {
            "run_id": "12121212-1212-4212-8212-121212121212",
            "experiment_id": self.experiment["experiment_id"],
            "tenant_id": "tenant-a",
            "evaluator_id": self.evaluator["evaluator_id"],
            "dataset_id": self.dataset["dataset_id"],
            "status": "succeeded",
            "target_snapshot": {"candidate_label": "baseline"},
            "score_summary": {"overall_score": 0.9, "trajectory_pass_rate": 1.0, "critical_pass_rate": 1.0},
            "metrics": {"targets": 10},
            "error_message": None,
            "created_by": "user-a",
            "started_at": datetime.now(timezone.utc),
            "finished_at": datetime.now(timezone.utc),
            "created_at": datetime.now(timezone.utc),
            "updated_at": datetime.now(timezone.utc),
        }
        self.candidate_run = {
            **self.baseline_run,
            "run_id": "34343434-3434-4434-8434-343434343434",
            "target_snapshot": {"candidate_label": "candidate"},
            "score_summary": {"overall_score": 0.88, "trajectory_pass_rate": 0.96, "critical_pass_rate": 1.0},
        }

    async def list_traces(self, **kwargs: Any) -> tuple[list[dict[str, Any]], int]:
        self.calls.append(("list", kwargs))
        return [_trace_row()], 1

    async def get_trace_detail(self, **kwargs: Any) -> dict[str, Any] | None:
        self.calls.append(("detail", kwargs))
        return self.detail

    async def create_score(self, **kwargs: Any) -> dict[str, Any] | None:
        self.calls.append(("score", kwargs))
        return self.score

    async def ingest_trace(self, **kwargs: Any) -> dict[str, Any]:
        self.calls.append(("ingest", kwargs))
        return {"trace_id": "11111111-1111-4111-8111-111111111111", "status": "stored", "job_id": None}

    async def get_thread(self, **kwargs: Any) -> dict[str, Any]:
        self.calls.append(("thread", kwargs))
        return {
            "thread_id": kwargs["thread_id"],
            "traces": [_trace_row()],
            "total": 1,
            "metrics": {"trace_count": 1, "total_latency_ms": 980},
        }

    async def create_dataset(self, **kwargs: Any) -> dict[str, Any]:
        self.calls.append(("dataset", kwargs))
        return self.dataset

    async def get_dataset(self, **kwargs: Any) -> dict[str, Any] | None:
        self.calls.append(("get_dataset", kwargs))
        return self.dataset

    async def create_example_from_trace(self, **kwargs: Any) -> dict[str, Any] | None:
        self.calls.append(("example", kwargs))
        return self.example

    async def update_example(self, **kwargs: Any) -> dict[str, Any] | None:
        self.calls.append(("update_example", kwargs))
        payload = kwargs["payload"]
        metadata = {
            **self.example["metadata"],
            **payload.get("metadata", {}),
        }
        for key in ("expected_trajectory", "assertions", "tags", "difficulty", "owner", "review_status"):
            if payload.get(key) is not None:
                metadata[key] = payload[key]
        self.example = {
            **self.example,
            "split": payload.get("split") or self.example["split"],
            "input": payload.get("input") or self.example["input"],
            "expected_output": payload.get("expected_output") or self.example["expected_output"],
            "metadata": metadata,
        }
        return self.example

    async def import_examples(self, **kwargs: Any) -> dict[str, Any]:
        self.calls.append(("import_examples", kwargs))
        examples = []
        for item in kwargs["examples"]:
            examples.append(
                {
                    **self.example,
                    "example_id": f"bbbbbbbb-bbbb-4bbb-8bbb-bbbbbbbbbbb{len(examples)}",
                    "input": item.get("input") or {},
                    "expected_output": item.get("expected_output") or {},
                    "metadata": {
                        "case_id": item.get("case_id"),
                        "expected_trajectory": item.get("expected_trajectory") or {},
                        "assertions": item.get("assertions") or [],
                        **(item.get("metadata") or {}),
                    },
                }
            )
        return {"imported": len(examples), "skipped": 0, "examples": examples}

    async def create_evaluator(self, **kwargs: Any) -> dict[str, Any]:
        self.calls.append(("evaluator", kwargs))
        return self.evaluator

    async def create_experiment(self, **kwargs: Any) -> dict[str, Any]:
        self.calls.append(("experiment", kwargs))
        return self.experiment

    async def get_experiment(self, **kwargs: Any) -> dict[str, Any] | None:
        self.calls.append(("experiment_detail", kwargs))
        return self.experiment

    async def enqueue_evaluator_run(self, **kwargs: Any) -> dict[str, Any]:
        self.calls.append(("evaluator_run", kwargs))
        return {
            "job_id": "eeeeeeee-eeee-4eee-8eee-eeeeeeeeeeee",
            "status": "queued",
            "run_id": "ffffffff-ffff-4fff-8fff-ffffffffffff",
        }

    async def list_datasets(self, **kwargs: Any) -> tuple[list[dict[str, Any]], int]:
        self.calls.append(("list_datasets", kwargs))
        return [self.dataset], 1

    async def list_examples(self, **kwargs: Any) -> tuple[list[dict[str, Any]], int]:
        self.calls.append(("list_examples", kwargs))
        return [self.example], 1

    async def list_evaluators(self, **kwargs: Any) -> tuple[list[dict[str, Any]], int]:
        self.calls.append(("list_evaluators", kwargs))
        return [self.evaluator], 1

    async def list_experiments(self, **kwargs: Any) -> tuple[list[dict[str, Any]], int]:
        self.calls.append(("list_experiments", kwargs))
        return [self.experiment], 1

    async def get_evaluator(self, **kwargs: Any) -> dict[str, Any] | None:
        self.calls.append(("get_evaluator", kwargs))
        return self.evaluator

    async def get_summary(self, **kwargs: Any) -> dict[str, Any]:
        self.calls.append(("summary", kwargs))
        return {
            "total_traces": 12,
            "failed_traces": 1,
            "succeeded_traces": 11,
            "assistant_traces": 8,
            "langgraph_traces": 2,
            "rag_traces": 2,
            "avg_latency_ms": 420,
            "p95_latency_ms": 980,
            "total_tokens": 1500,
            "total_cost_cents": 12,
            "scored_traces": 3,
            "window_days": kwargs.get("days", 7),
        }

    async def get_dashboard(self, **kwargs: Any) -> dict[str, Any]:
        self.calls.append(("dashboard", kwargs))
        return {
            "metrics": {
                "total_traces": 12,
                "scored_traces": 3,
                "example_count": 10,
                "pass_rate": 0.9,
                "trajectory_pass_rate": 0.96,
                "critical_failures": 0,
                "judge_pending_count": 2,
            },
            "run_health": {"succeeded_runs": 2, "failed_runs": 0},
            "queue_health": {"queued_jobs": 0, "failed_jobs": 1},
            "latest_gate_status": {"status": "pass"},
        }

    async def get_experiment_run(self, **kwargs: Any) -> dict[str, Any] | None:
        self.calls.append(("get_experiment_run", kwargs))
        if kwargs.get("run_id") == self.baseline_run["run_id"]:
            return self.baseline_run
        if kwargs.get("run_id") == self.candidate_run["run_id"]:
            return self.candidate_run
        return {
            "run_id": "ffffffff-ffff-4fff-8fff-ffffffffffff",
            "experiment_id": self.experiment["experiment_id"],
            "tenant_id": "tenant-a",
            "evaluator_id": self.evaluator["evaluator_id"],
            "dataset_id": self.dataset["dataset_id"],
            "trace_id": "11111111-1111-4111-8111-111111111111",
            "status": "succeeded",
            "score_summary": {"average_score": 0.9},
            "metrics": {"targets": 1},
            "error_message": None,
            "created_by": "user-a",
            "started_at": datetime.now(timezone.utc),
            "finished_at": datetime.now(timezone.utc),
            "created_at": datetime.now(timezone.utc),
            "updated_at": datetime.now(timezone.utc),
        }

    async def compare_experiment_runs(self, **kwargs: Any) -> dict[str, Any] | None:
        self.calls.append(("compare_runs", kwargs))
        return {
            "baseline_run_id": kwargs["baseline_run_id"],
            "candidate_run_id": kwargs["candidate_run_id"],
            "baseline_summary": self.baseline_run["score_summary"],
            "candidate_summary": self.candidate_run["score_summary"],
            "deltas": {"overall_score": -0.02, "trajectory_pass_rate": -0.04},
            "regression_summary": {"regressed_metrics": ["overall_score", "trajectory_pass_rate"]},
            "case_diffs": [],
        }


@pytest.mark.asyncio
async def test_eval_list_rejects_legacy_usage_permission_only(monkeypatch) -> None:
    repo = FakeTraceRepository()
    monkeypatch.setattr(eval_routes, "_get_trace_repository", lambda _request: repo)

    with pytest.raises(HTTPException) as exc:
        await list_eval_traces(
            request=_request(),
            auth=_auth(permissions=["console:usage:view"]),
        )

    assert exc.value.status_code == 403
    assert repo.calls == []


@pytest.mark.asyncio
async def test_eval_trace_list_supports_score_and_dataset_filters(monkeypatch):
    repo = FakeTraceRepository()
    monkeypatch.setattr(eval_routes, "_get_trace_repository", lambda _request: repo)

    await list_eval_traces(
        request=_request(),
        score_name="quality",
        score_label="pass",
        min_score=0.8,
        max_score=1.0,
        dataset_id="aaaaaaaa-aaaa-4aaa-8aaa-aaaaaaaaaaaa",
        span_kind="retriever",
        auth=_auth(user_id="admin", tenant_id="tenant-a", roles=["admin"]),
    )

    call_name, kwargs = repo.calls[-1]
    assert call_name == "list"
    assert kwargs["score_name"] == "quality"
    assert kwargs["score_label"] == "pass"
    assert kwargs["min_score"] == 0.8
    assert kwargs["max_score"] == 1.0
    assert kwargs["dataset_id"] == "aaaaaaaa-aaaa-4aaa-8aaa-aaaaaaaaaaaa"
    assert kwargs["span_kind"] == "retriever"


@pytest.mark.asyncio
async def test_get_eval_summary_scopes_tenant_and_user(monkeypatch):
    repo = FakeTraceRepository()
    monkeypatch.setattr(eval_routes, "_get_trace_repository", lambda _request: repo)

    result = await get_eval_summary(
        request=_request(),
        days=14,
        user_id="user-b",
        auth=_auth(user_id="user-a", tenant_id="tenant-a"),
    )

    assert result.total_traces == 12
    assert result.langgraph_traces == 2
    assert result.window_days == 14
    call_name, kwargs = repo.calls[-1]
    assert call_name == "summary"
    assert kwargs["tenant_id"] == "tenant-a"
    assert kwargs["user_id"] == "user-a"
    assert kwargs["days"] == 14


@pytest.mark.asyncio
async def test_list_eval_traces_uses_server_tenant_and_non_admin_user_scope(monkeypatch):
    repo = FakeTraceRepository()
    monkeypatch.setattr(eval_routes, "_get_trace_repository", lambda _request: repo)

    result = await list_eval_traces(
        request=_request(),
        user_id="user-b",
        auth=_auth(user_id="user-a", tenant_id="tenant-a"),
    )

    assert result.total == 1
    assert result.traces[0].tenant_id == "tenant-a"
    call_name, kwargs = repo.calls[-1]
    assert call_name == "list"
    assert kwargs["tenant_id"] == "tenant-a"
    assert kwargs["user_id"] == "user-a"


@pytest.mark.asyncio
async def test_admin_eval_trace_list_can_filter_user_within_server_tenant(monkeypatch):
    repo = FakeTraceRepository()
    monkeypatch.setattr(eval_routes, "_get_trace_repository", lambda _request: repo)

    await list_eval_traces(
        request=_request(),
        user_id="user-b",
        auth=_auth(user_id="admin", tenant_id="tenant-a", roles=["admin"]),
    )

    call_name, kwargs = repo.calls[-1]
    assert call_name == "list"
    assert kwargs["tenant_id"] == "tenant-a"
    assert kwargs["user_id"] == "user-b"


@pytest.mark.asyncio
async def test_eval_trace_list_accepts_transcript_locator_filters(monkeypatch):
    repo = FakeTraceRepository()
    monkeypatch.setattr(eval_routes, "_get_trace_repository", lambda _request: repo)

    await list_eval_traces(
        request=_request(),
        session_id="session-a",
        request_id="request-c",
        transcript_query="  refund transcript turn  ",
        turn_index=3,
        auth=_auth(user_id="admin", tenant_id="tenant-a", roles=["admin"]),
    )

    call_name, kwargs = repo.calls[-1]
    assert call_name == "list"
    assert kwargs["tenant_id"] == "tenant-a"
    assert kwargs["session_id"] == "session-a"
    assert kwargs["request_id"] == "request-c"
    assert kwargs["transcript_query"] == "refund transcript turn"
    assert kwargs["turn_index"] == 3


@pytest.mark.asyncio
async def test_non_operator_eval_trace_list_rejects_missing_authenticated_user(monkeypatch):
    repo = FakeTraceRepository()
    monkeypatch.setattr(eval_routes, "_get_trace_repository", lambda _request: repo)

    with pytest.raises(HTTPException) as exc:
        await list_eval_traces(
            request=_request(),
            user_id="user-b",
            auth=_auth(user_id="", tenant_id="tenant-a"),
        )

    assert exc.value.status_code == 403
    assert repo.calls == []


@pytest.mark.asyncio
async def test_eval_trace_detail_missing_cross_scope_trace_returns_404(monkeypatch):
    repo = FakeTraceRepository()
    repo.detail = None
    monkeypatch.setattr(eval_routes, "_get_trace_repository", lambda _request: repo)

    with pytest.raises(HTTPException) as exc:
        await get_eval_trace_detail(
            trace_id="other-tenant-trace",
            request=_request(),
            auth=_auth(user_id="user-a", tenant_id="tenant-a"),
        )

    assert exc.value.status_code == 404
    call_name, kwargs = repo.calls[-1]
    assert call_name == "detail"
    assert kwargs["tenant_id"] == "tenant-a"
    assert kwargs["user_id"] == "user-a"


@pytest.mark.asyncio
async def test_create_eval_trace_score_uses_authenticated_evaluator(monkeypatch):
    repo = FakeTraceRepository()
    monkeypatch.setattr(eval_routes, "_get_trace_repository", lambda _request: repo)

    body = AgentTraceScoreCreate(
        score_name="quality",
        numeric_value=0.9,
        label="good",
        explanation="grounded answer",
    )
    result = await create_eval_trace_score(
        trace_id="11111111-1111-4111-8111-111111111111",
        body=body,
        request=_request(),
        auth=_auth(
            user_id="user-a",
            tenant_id="tenant-a",
            permissions=["console:eval:view", "console:eval:run"],
        ),
    )

    assert result.created_by == "user-a"
    call_name, kwargs = repo.calls[-1]
    assert call_name == "score"
    assert kwargs["tenant_id"] == "tenant-a"
    assert kwargs["user_id"] == "user-a"
    assert kwargs["created_by"] == "user-a"
    assert "tenant_id" not in kwargs["payload"]


@pytest.mark.asyncio
async def test_eval_api_allows_future_trace_families_and_rejects_unknown(monkeypatch):
    repo = FakeTraceRepository()
    monkeypatch.setattr(eval_routes, "_get_trace_repository", lambda _request: repo)

    await list_eval_traces(
        request=_request(),
        trace_family="rag",
        auth=_auth(user_id="user-a", tenant_id="tenant-a"),
    )
    assert repo.calls[-1][1]["trace_family"] == "rag"

    with pytest.raises(HTTPException) as exc:
        await list_eval_traces(
            request=_request(),
            trace_family="unknown",
            auth=_auth(user_id="user-a", tenant_id="tenant-a"),
        )

    assert exc.value.status_code == 400


@pytest.mark.asyncio
async def test_eval_trace_thread_uses_scoped_user(monkeypatch):
    repo = FakeTraceRepository()
    monkeypatch.setattr(eval_routes, "_get_trace_repository", lambda _request: repo)

    result = await get_eval_trace_thread(
        thread_id="session-a",
        request=_request(),
        auth=_auth(user_id="user-a", tenant_id="tenant-a"),
    )

    assert result.total == 1
    call_name, kwargs = repo.calls[-1]
    assert call_name == "thread"
    assert kwargs["tenant_id"] == "tenant-a"
    assert kwargs["thread_id"] == "session-a"
    assert kwargs["user_id"] == "user-a"


@pytest.mark.asyncio
async def test_eval_trace_export_returns_semantic_payload(monkeypatch):
    repo = FakeTraceRepository()
    monkeypatch.setattr(eval_routes, "_get_trace_repository", lambda _request: repo)

    result = await export_eval_trace(
        trace_id="11111111-1111-4111-8111-111111111111",
        request=_request(),
        format="otel",
        auth=_auth(user_id="user-a", tenant_id="tenant-a"),
    )

    assert result.format == "otel"
    assert result.redaction_policy
    assert isinstance(result.payload, dict)
    assert result.payload["trace"]["attributes"]["gen_ai.request.model"] == "qwen3.6-plus"
    assert result.payload["trace"]["attributes"]["gen_ai.usage.total_tokens"] == 30


@pytest.mark.asyncio
async def test_eval_trace_export_defensively_redacts_sensitive_payload(monkeypatch) -> None:
    repo = FakeTraceRepository()
    repo.detail = {
        "trace": _trace_row(
            input_preview="hello Authorization: Bearer raw-trace-token",
            output_preview="safe",
            metadata={
                "headers": {
                    "Authorization": "Bearer raw-header-token",
                    "Cookie": "sid=raw-cookie",
                },
                "callback_url": "https://user:raw-password@example.test/path",
            },
        ),
        "spans": [
            {
                "span_id": "33333333-3333-4333-8333-333333333333",
                "trace_id": "11111111-1111-4111-8111-111111111111",
                "parent_span_id": None,
                "span_kind": "tool_execution",
                "name": "tool",
                "status": "succeeded",
                "started_at": datetime.now(timezone.utc),
                "ended_at": datetime.now(timezone.utc),
                "duration_ms": 10,
                "attributes": {
                    "api_key": "raw-api-key",
                    "note": "access_token=raw-access-token",
                },
            }
        ],
        "events": [
            {
                "event_type": "tool_call",
                "occurred_at": datetime.now(timezone.utc),
                "payload": {
                    "headers": {"Authorization": "Bearer raw-event-token"},
                    "cookie": "raw-event-cookie",
                    "body": "password=raw-password",
                },
            }
        ],
        "scores": [],
    }
    monkeypatch.setattr(eval_routes, "_get_trace_repository", lambda _request: repo)

    result = await export_eval_trace(
        trace_id="11111111-1111-4111-8111-111111111111",
        request=_request(),
        format="otel",
        auth=_auth(user_id="user-a", tenant_id="tenant-a"),
    )

    serialized = json.dumps(result.payload, default=str)
    assert "raw-trace-token" not in serialized
    assert "raw-header-token" not in serialized
    assert "raw-event-token" not in serialized
    assert "raw-cookie" not in serialized
    assert "raw-event-cookie" not in serialized
    assert "raw-api-key" not in serialized
    assert "raw-access-token" not in serialized
    assert "raw-password" not in serialized
    assert "[redacted]" in serialized


@pytest.mark.asyncio
async def test_eval_dataset_evaluator_experiment_workflow(monkeypatch):
    repo = FakeTraceRepository()
    monkeypatch.setattr(eval_routes, "_get_trace_repository", lambda _request: repo)
    request = _request()
    auth = _auth(
        user_id="user-a",
        tenant_id="tenant-a",
        permissions=["console:eval:view", "console:eval:run"],
    )

    dataset = await create_eval_dataset(
        body=EvalDatasetCreate(name="assistant-regression", description="regression set"),
        request=request,
        auth=auth,
    )
    example = await create_eval_example_from_trace(
        dataset_id=dataset.dataset_id,
        body=EvalExampleFromTraceCreate(
            source_trace_id="11111111-1111-4111-8111-111111111111"
        ),
        request=request,
        auth=auth,
    )
    evaluator = await create_eval_evaluator(
        body=EvalEvaluatorCreate(name="quality", rubric="score quality"),
        request=request,
        auth=auth,
    )
    experiment = await create_eval_experiment(
        body=EvalExperimentCreate(name="baseline", dataset_id=dataset.dataset_id),
        request=request,
        auth=auth,
    )
    job = await run_eval_evaluator_async(
        evaluator_id=evaluator.evaluator_id,
        body=EvalEvaluatorRunRequest(experiment_id=experiment.experiment_id, dataset_id=dataset.dataset_id),
        request=request,
        auth=auth,
    )

    assert example.source_trace_id == "11111111-1111-4111-8111-111111111111"
    assert evaluator.evaluator_type == "human"
    assert experiment.dataset_id == dataset.dataset_id
    assert job.status == "queued"
    assert [call[0] for call in repo.calls[-5:]] == [
        "dataset",
        "example",
        "evaluator",
        "experiment",
        "evaluator_run",
    ]


@pytest.mark.asyncio
async def test_eval_example_from_trace_preserves_requested_trace_family(monkeypatch) -> None:
    repo = FakeTraceRepository()
    monkeypatch.setattr(eval_routes, "_get_trace_repository", lambda _request: repo)

    await create_eval_example_from_trace(
        dataset_id="aaaaaaaa-aaaa-4aaa-8aaa-aaaaaaaaaaaa",
        body=EvalExampleFromTraceCreate(
            source_trace_id="11111111-1111-4111-8111-111111111111",
            trace_family="rag",
        ),
        request=_request(),
        auth=_auth(
            user_id="user-a",
            tenant_id="tenant-a",
            permissions=["console:eval:view", "console:eval:run"],
        ),
    )

    assert repo.calls[-1][0] == "example"
    assert repo.calls[-1][1]["trace_family"] == "rag"


@pytest.mark.asyncio
async def test_eval_list_get_endpoints_use_trace_read_capability(monkeypatch) -> None:
    repo = FakeTraceRepository()
    monkeypatch.setattr(eval_routes, "_get_trace_repository", lambda _request: repo)
    request = _request()
    auth = _auth(user_id="user-a", tenant_id="tenant-a", permissions=["console:eval:view"])

    datasets = await list_eval_datasets(request=request, auth=auth)
    examples = await list_eval_examples(
        dataset_id=datasets.datasets[0].dataset_id,
        request=request,
        auth=auth,
    )
    evaluators = await list_eval_evaluators(request=request, auth=auth)
    experiments = await list_eval_experiments(request=request, auth=auth)
    dataset = await get_eval_dataset(
        dataset_id=datasets.datasets[0].dataset_id,
        request=request,
        auth=auth,
    )
    evaluator = await get_eval_evaluator(
        evaluator_id=evaluators.evaluators[0].evaluator_id,
        request=request,
        auth=auth,
    )
    experiment = await get_eval_experiment(
        experiment_id=experiments.experiments[0].experiment_id,
        request=request,
        auth=auth,
    )
    run = await get_eval_experiment_run(
        run_id="ffffffff-ffff-4fff-8fff-ffffffffffff",
        request=request,
        auth=auth,
    )

    assert datasets.total == 1
    assert examples.total == 1
    assert evaluators.total == 1
    assert experiments.total == 1
    assert dataset.dataset_id == datasets.datasets[0].dataset_id
    assert evaluator.evaluator_id == evaluators.evaluators[0].evaluator_id
    assert experiment.experiment_id == experiments.experiments[0].experiment_id
    assert run.status == "succeeded"


@pytest.mark.asyncio
async def test_eval_run_endpoint_requires_eval_run_permission(monkeypatch) -> None:
    repo = FakeTraceRepository()
    monkeypatch.setattr(eval_routes, "_get_trace_repository", lambda _request: repo)
    request = _request()

    with pytest.raises(HTTPException) as exc_info:
        await run_eval_evaluator_async(
            evaluator_id="cccccccc-cccc-4ccc-8ccc-cccccccccccc",
            body=EvalEvaluatorRunRequest(trace_id="11111111-1111-4111-8111-111111111111"),
            request=request,
            auth=_auth(permissions=["console:eval:view"]),
        )

    assert exc_info.value.status_code == 403
    detail = exc_info.value.detail
    assert detail["required_capability"] == Capability.GATEWAY_EVAL_RUN.value


@pytest.mark.asyncio
async def test_eval_dashboard_returns_platform_health(monkeypatch) -> None:
    repo = FakeTraceRepository()
    monkeypatch.setattr(eval_routes, "_get_trace_repository", lambda _request: repo)

    result = await get_eval_dashboard(
        request=_request(),
        days=14,
        auth=_auth(user_id="admin", tenant_id="tenant-a", roles=["admin"]),
    )

    assert result.metrics["example_count"] == 10
    assert result.queue_health["failed_jobs"] == 1
    assert result.latest_gate_status["status"] == "pass"
    assert repo.calls[-1] == ("dashboard", {"tenant_id": "tenant-a", "days": 14})


@pytest.mark.asyncio
async def test_eval_example_review_import_and_export(monkeypatch) -> None:
    repo = FakeTraceRepository()
    monkeypatch.setattr(eval_routes, "_get_trace_repository", lambda _request: repo)
    auth = _auth(permissions=["console:eval:view", "console:eval:run"])

    updated = await update_eval_example(
        dataset_id=repo.dataset["dataset_id"],
        example_id=repo.example["example_id"],
        body=EvalExampleUpdate(
            expected_trajectory={"required_span_kinds": ["model_invocation"]},
            assertions=[{"type": "output_contains", "value": "hi"}],
            review_status="approved",
            tags=["golden"],
        ),
        request=_request(),
        auth=auth,
    )
    assert updated.metadata["review_status"] == "approved"
    assert updated.metadata["expected_trajectory"]["required_span_kinds"] == ["model_invocation"]

    imported = await import_eval_examples(
        dataset_id=repo.dataset["dataset_id"],
        body=EvalExamplesImportRequest(
            examples=[
                {
                    "case_id": "assistant.case.one",
                    "input": {"input_preview": "hello"},
                    "expected_output": {"contains": "hi"},
                    "expected_trajectory": {"required_span_kinds": ["model_invocation"]},
                    "assertions": [{"type": "output_contains", "value": "hi"}],
                    "metadata": {"review_status": "approved"},
                }
            ]
        ),
        request=_request(),
        auth=auth,
    )
    assert imported.imported == 1
    assert imported.examples[0].metadata["case_id"] == "assistant.case.one"

    exported = await export_eval_examples(
        dataset_id=repo.dataset["dataset_id"],
        request=_request(),
        auth=_auth(),
    )
    assert exported.dataset.dataset_id == repo.dataset["dataset_id"]
    assert exported.examples[0].case_id in {"assistant.case.one", repo.example["example_id"]}


@pytest.mark.asyncio
async def test_eval_experiment_batch_compare_and_gate(monkeypatch) -> None:
    repo = FakeTraceRepository()
    monkeypatch.setattr(eval_routes, "_get_trace_repository", lambda _request: repo)
    auth = _auth(permissions=["console:eval:view", "console:eval:run"])

    batch = await run_eval_experiment(
        experiment_id=repo.experiment["experiment_id"],
        body=EvalExperimentRunCreate(
            dataset_id=repo.dataset["dataset_id"],
            evaluator_ids=[repo.evaluator["evaluator_id"]],
            target_snapshot={"trace_family": "assistant", "candidate_label": "candidate"},
        ),
        request=_request(),
        auth=auth,
    )
    assert batch.jobs[0].status == "queued"

    comparison = await compare_eval_experiment_runs(
        request=_request(),
        baseline_run_id=repo.baseline_run["run_id"],
        candidate_run_id=repo.candidate_run["run_id"],
        auth=_auth(),
    )
    assert comparison.deltas["overall_score"] == -0.02
    assert "overall_score" in comparison.regression_summary["regressed_metrics"]

    gate = await dry_run_eval_gate(
        body=EvalGateDryRunRequest(
            result_payload={
                "metrics": {
                    "overall_score": 0.88,
                    "trajectory_pass_rate": 0.96,
                    "critical_pass_rate": 1.0,
                }
            }
        ),
        request=_request(),
        auth=auth,
    )
    assert gate.status == "pass"


def test_eval_openapi_paths_are_registered() -> None:
    paths = create_app().openapi()["paths"]

    assert "/api/v1/eval/summary" in paths
    assert "/api/v1/eval/dashboard" in paths
    assert "/api/v1/eval/traces" in paths
    assert "/api/v1/eval/traces/{trace_id}" in paths
    assert "/api/v1/eval/traces/{trace_id}/scores" in paths
    assert "/api/v1/eval/ingest/traces" in paths
    assert "/api/v1/eval/threads/{thread_id}" in paths
    assert "/api/v1/eval/traces/{trace_id}/export" in paths
    assert "/api/v1/eval/datasets" in paths
    assert "/api/v1/eval/datasets/{dataset_id}/examples" in paths
    assert "/api/v1/eval/datasets/{dataset_id}/examples/{example_id}" in paths
    assert "/api/v1/eval/datasets/{dataset_id}/examples:import" in paths
    assert "/api/v1/eval/datasets/{dataset_id}/examples:export" in paths
    assert "/api/v1/eval/datasets/{dataset_id}/examples:from-trace" in paths
    assert "/api/v1/eval/evaluators" in paths
    assert "/api/v1/eval/evaluators/{evaluator_id}:run-async" in paths
    assert "/api/v1/eval/experiments" in paths
    assert "/api/v1/eval/experiments/{experiment_id}:run" in paths
    assert "/api/v1/eval/experiment-runs/{run_id}" in paths
    assert "/api/v1/eval/experiment-runs:compare" in paths
    assert "/api/v1/eval/gates:dry-run" in paths


def test_eval_openapi_and_web_types_expose_shared_trace_contract() -> None:
    schemas = create_app().openapi()["components"]["schemas"]

    assert {
        "thread_id",
        "metrics",
        "privacy",
        "source_adapter",
    }.issubset(schemas["AgentTraceSummary"]["properties"])
    assert {
        "target_type",
        "target_id",
        "evaluator_id",
        "evaluator_name",
        "score_source",
        "confidence",
    }.issubset(schemas["AgentTraceScore"]["properties"])
    assert "redaction_policy" in schemas["EvalTraceExportResponse"]["properties"]

    web_types = Path("web/src/api/eval.ts").read_text(encoding="utf-8")
    for token in (
        "thread_id?: string | null",
        "metrics: Record<string, unknown>",
        "privacy: Record<string, unknown>",
        "source_adapter?: string | null",
        "target_type: ScoreTargetType",
        "target_id?: string | null",
        "evaluator_id?: string | null",
        "evaluator_name?: string | null",
        "score_source: string",
        "confidence?: number | null",
        "redaction_policy: Record<string, unknown>",
        "runs: EvalExperimentRun[]",
        "EvalDashboardResponse",
        "updateEvalExample",
        "importEvalExamples",
        "exportEvalExamples",
        "compareEvalExperimentRuns",
        "dryRunEvalGate",
    ):
        assert token in web_types


class CapturingTraceRepository(AgentTraceRepository):
    def __init__(self) -> None:
        self.fetchrow_calls: list[tuple[str, tuple[Any, ...]]] = []
        self.fetch_calls: list[tuple[str, tuple[Any, ...]]] = []

    async def fetchrow(self, query: str, *args: Any) -> dict[str, Any] | None:
        self.fetchrow_calls.append((query, args))
        return {"total": 0}

    async def fetch(self, query: str, *args: Any) -> list[dict[str, Any]]:
        self.fetch_calls.append((query, args))
        return []


@pytest.mark.asyncio
async def test_agent_trace_repository_filters_by_transcript_locator() -> None:
    repo = CapturingTraceRepository()

    rows, total = await repo.list_traces(
        tenant_id="tenant-a",
        trace_family="assistant",
        session_id="session-a",
        request_id="request-c",
        transcript_query="refund transcript turn",
        turn_index=3,
    )

    assert rows == []
    assert total == 0
    count_query, count_args = repo.fetchrow_calls[-1]
    page_query, page_args = repo.fetch_calls[-1]
    assert "t.request_id" in count_query
    assert "t.metadata->'transcript_locator'->>'turn_index'" in count_query
    assert "current_message_preview" in count_query
    assert "transcript_excerpt" in count_query
    assert "turn_id" in count_query
    assert "request-c" in count_args
    assert "3" in count_args
    assert "%refund transcript turn%" in count_args
    assert page_args[-2:] == (50, 0)
    assert "ORDER BY t.created_at DESC" in page_query
