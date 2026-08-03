from __future__ import annotations

import hashlib
from typing import Annotated, Any

from ai_gateway_core.eval.evaluator_executor import REQUIRED_ASSISTANT_HARD_BLOCKERS
from ai_gateway_core.persistence.repositories.agent_trace_repository import (
    AgentTraceRepository,
)
from fastapi import APIRouter, Depends, HTTPException, Query, Request
from pydantic import ValidationError

from ...api.deps import AuthContext, get_auth_context, require_gateway_capability
from ...core.auth.permissions import Capability, build_permission_denied_detail
from ...persistence.database import DatabaseStorage
from ...services.eval.golden import apply_gate, validate_case
from ...services.eval.kb_ragas_service import (
    batch_score_kb_ragas_traces,
    get_kb_ragas_knowledge_summary,
    score_retrieval_with_kb_ragas,
)
from ...services.eval.trace_feedback import (
    build_harness_profile_proposal,
    build_redacted_dataset_case,
    classify_trace_failure,
    cluster_failure_patterns,
)
from ..eval_export import EXPORT_REDACTION_POLICY, export_trace
from ..schemas.eval import (
    AgentTraceDetailResponse,
    AgentTraceIngestRequest,
    AgentTraceIngestResponse,
    AgentTraceListResponse,
    AgentTraceScore,
    AgentTraceScoreCreate,
    AgentTraceSummary,
    EvalAsyncJobResponse,
    EvalBaselinePromotionRequest,
    EvalBaselinePromotionResponse,
    EvalDashboardResponse,
    EvalDataset,
    EvalDatasetCreate,
    EvalDatasetListResponse,
    EvalEvaluator,
    EvalEvaluatorCreate,
    EvalEvaluatorListResponse,
    EvalEvaluatorRunRequest,
    EvalExample,
    EvalExampleFromTraceCreate,
    EvalExampleListResponse,
    EvalExamplesExportResponse,
    EvalExamplesImportRequest,
    EvalExamplesImportResponse,
    EvalExampleUpdate,
    EvalExperiment,
    EvalExperimentCreate,
    EvalExperimentListResponse,
    EvalExperimentRun,
    EvalExperimentRunBatchResponse,
    EvalExperimentRunComparisonResponse,
    EvalExperimentRunCreate,
    EvalExperimentRunResultsResponse,
    EvalGateDryRunRequest,
    EvalGateDryRunResponse,
    EvalGateMetricsV2,
    EvalTraceExportResponse,
    EvalTraceFailurePattern,
    EvalTraceFeedbackRequest,
    EvalTraceFeedbackResponse,
    EvalTraceMonitoringSummary,
    EvalTraceThreadResponse,
    KbRagasBatchScoreRequest,
    KbRagasBatchScoreResponse,
    KbRagasKnowledgeSummaryResponse,
    KbRagasMetricSummary,
    KbRagasScoreRetrievalRequest,
    KbRagasScoreRetrievalResponse,
    KbRagasScoreRetrievalResult,
    TraceExportFormat,
)

router = APIRouter(prefix="/eval", tags=["eval"])


def _get_database(request: Request) -> DatabaseStorage:
    db = getattr(request.app.state, "database", None)
    if not db:
        raise HTTPException(status_code=503, detail="Database not available")
    return db


def _get_trace_repository(request: Request) -> AgentTraceRepository:
    return AgentTraceRepository(_get_database(request))


def _is_eval_operator(auth: AuthContext) -> bool:
    return any(role in {"admin", "operator"} for role in auth.roles)


def _require_eval_trace_access(request: Request, auth: AuthContext) -> None:
    require_gateway_capability(request, auth, Capability.GATEWAY_EVAL_TRACE_READ)
    if not auth.tenant_id:
        raise HTTPException(
            status_code=403,
            detail={
                **build_permission_denied_detail(
                    capability=Capability.GATEWAY_EVAL_TRACE_READ,
                    trace_id=str(getattr(request.state, "request_id", "") or ""),
                    message="Permission denied: tenant scope required for Eval trace queries",
                ),
            },
        )
    if not _is_eval_operator(auth) and not auth.user_id:
        raise HTTPException(
            status_code=403,
            detail={
                **build_permission_denied_detail(
                    capability=Capability.GATEWAY_EVAL_TRACE_READ,
                    trace_id=str(getattr(request.state, "request_id", "") or ""),
                    message="Permission denied: authenticated user scope required for Eval trace queries",
                ),
            },
        )


def _require_eval_run_access(request: Request, auth: AuthContext) -> None:
    _require_eval_trace_access(request, auth)
    require_gateway_capability(request, auth, Capability.GATEWAY_EVAL_RUN)


def _scoped_user_id(auth: AuthContext, requested_user_id: str | None = None) -> str | None:
    if _is_eval_operator(auth):
        return requested_user_id
    return auth.user_id or requested_user_id


def _require_supported_family(trace_family: str) -> None:
    if trace_family not in {"assistant", "langgraph_proxy", "rag"}:
        raise HTTPException(
            status_code=400,
            detail="Unsupported trace_family.",
        )


def _golden_case_from_example(example: dict[str, Any]) -> dict[str, Any]:
    metadata = example.get("metadata") if isinstance(example.get("metadata"), dict) else {}
    return {
        "case_id": metadata.get("case_id") or example.get("example_id"),
        "split": example.get("split") or "regression",
        "input": example.get("input") or {},
        "expected_output": example.get("expected_output") or {},
        "expected_trajectory": metadata.get("expected_trajectory") or {},
        "assertions": metadata.get("assertions") or [],
        "metadata": {
            key: value
            for key, value in metadata.items()
            if key not in {"case_id", "expected_trajectory", "assertions"}
        },
    }


async def _hydrate_live_run(
    repository: AgentTraceRepository,
    *,
    tenant_id: str,
    run: dict[str, Any],
) -> dict[str, Any]:
    if run.get("run_mode") != "live_candidate":
        return run
    progress_loader = getattr(repository, "get_experiment_run_progress", None)
    if callable(progress_loader):
        run["progress"] = await progress_loader(
            tenant_id=tenant_id,
            run_id=str(run.get("run_id") or ""),
        )
    metrics = run.get("metrics") if isinstance(run.get("metrics"), dict) else {}
    run["runtime_fingerprint"] = metrics.get("actual_fingerprint") or {}
    gate = metrics.get("gate") if isinstance(metrics.get("gate"), dict) else {}
    run["gate_status"] = gate.get("status")
    return run


@router.get("/summary", response_model=EvalTraceMonitoringSummary)
async def get_eval_summary(
    request: Request,
    days: Annotated[int, Query(ge=1, le=90)] = 7,
    user_id: Annotated[str | None, Query()] = None,
    auth: AuthContext = Depends(get_auth_context),
) -> EvalTraceMonitoringSummary:
    _require_eval_trace_access(request, auth)
    summary = await _get_trace_repository(request).get_summary(
        tenant_id=auth.tenant_id,
        user_id=_scoped_user_id(auth, user_id),
        days=days,
    )
    return EvalTraceMonitoringSummary(**summary)


@router.get("/dashboard", response_model=EvalDashboardResponse)
async def get_eval_dashboard(
    request: Request,
    days: Annotated[int, Query(ge=1, le=90)] = 7,
    auth: AuthContext = Depends(get_auth_context),
) -> EvalDashboardResponse:
    _require_eval_trace_access(request, auth)
    dashboard = await _get_trace_repository(request).get_dashboard(
        tenant_id=auth.tenant_id,
        days=days,
    )
    return EvalDashboardResponse(**dashboard)


@router.get("/traces", response_model=AgentTraceListResponse)
async def list_eval_traces(
    request: Request,
    trace_family: Annotated[
        str, Query(description="assistant, langgraph_proxy, or rag")
    ] = "assistant",
    status: Annotated[str | None, Query()] = None,
    model_id: Annotated[str | None, Query()] = None,
    user_id: Annotated[str | None, Query()] = None,
    session_id: Annotated[str | None, Query()] = None,
    run_id: Annotated[str | None, Query()] = None,
    request_id: Annotated[str | None, Query()] = None,
    agent_id: Annotated[str | None, Query()] = None,
    agent_version_id: Annotated[str | None, Query()] = None,
    publication_id: Annotated[str | None, Query()] = None,
    channel: Annotated[
        str | None, Query(pattern=r"^(preview|hosted|embed|api|builtin)$")
    ] = None,
    transcript_query: Annotated[str | None, Query(min_length=1, max_length=500)] = None,
    turn_index: Annotated[int | None, Query(ge=1)] = None,
    span_kind: Annotated[str | None, Query()] = None,
    score_name: Annotated[str | None, Query()] = None,
    score_label: Annotated[str | None, Query()] = None,
    min_score: Annotated[float | None, Query()] = None,
    max_score: Annotated[float | None, Query()] = None,
    min_latency_ms: Annotated[int | None, Query(ge=0)] = None,
    max_latency_ms: Annotated[int | None, Query(ge=0)] = None,
    dataset_id: Annotated[str | None, Query()] = None,
    metadata_dataset_id: Annotated[str | None, Query()] = None,
    started_after: Annotated[str | None, Query()] = None,
    started_before: Annotated[str | None, Query()] = None,
    limit: Annotated[int, Query(ge=1, le=200)] = 50,
    offset: Annotated[int, Query(ge=0)] = 0,
    auth: AuthContext = Depends(get_auth_context),
) -> AgentTraceListResponse:
    _require_supported_family(trace_family)
    _require_eval_trace_access(request, auth)

    repo = _get_trace_repository(request)
    rows, total = await repo.list_traces(
        tenant_id=auth.tenant_id,
        user_id=_scoped_user_id(auth, user_id),
        trace_family=trace_family,
        status=status,
        model_id=model_id,
        session_id=session_id,
        run_id=run_id,
        request_id=request_id,
        agent_id=agent_id,
        agent_version_id=agent_version_id,
        publication_id=publication_id,
        channel=channel,
        transcript_query=transcript_query.strip() if transcript_query else None,
        turn_index=turn_index,
        span_kind=span_kind,
        score_name=score_name,
        score_label=score_label,
        min_score=min_score,
        max_score=max_score,
        min_latency_ms=min_latency_ms,
        max_latency_ms=max_latency_ms,
        dataset_id=dataset_id,
        metadata_dataset_id=metadata_dataset_id,
        started_after=started_after,
        started_before=started_before,
        limit=limit,
        offset=offset,
    )
    return AgentTraceListResponse(
        traces=[AgentTraceSummary(**row) for row in rows],
        total=total,
        limit=limit,
        offset=offset,
    )


@router.get("/traces/{trace_id}", response_model=AgentTraceDetailResponse)
async def get_eval_trace_detail(
    trace_id: str,
    request: Request,
    trace_family: Annotated[
        str, Query(description="Trace family")
    ] = "assistant",
    auth: AuthContext = Depends(get_auth_context),
) -> AgentTraceDetailResponse:
    _require_supported_family(trace_family)
    _require_eval_trace_access(request, auth)

    detail = await _get_trace_repository(request).get_trace_detail(
        tenant_id=auth.tenant_id,
        trace_id=trace_id,
        user_id=_scoped_user_id(auth),
        trace_family=trace_family,
    )
    if not detail:
        raise HTTPException(status_code=404, detail="Trace not found")
    return AgentTraceDetailResponse(**detail)


@router.post("/traces/{trace_id}/scores", response_model=AgentTraceScore, status_code=201)
async def create_eval_trace_score(
    trace_id: str,
    body: AgentTraceScoreCreate,
    request: Request,
    trace_family: Annotated[
        str, Query(description="Trace family")
    ] = "assistant",
    auth: AuthContext = Depends(get_auth_context),
) -> AgentTraceScore:
    _require_supported_family(trace_family)
    _require_eval_run_access(request, auth)

    score = await _get_trace_repository(request).create_score(
        tenant_id=auth.tenant_id,
        trace_id=trace_id,
        user_id=_scoped_user_id(auth),
        created_by=auth.user_id,
        trace_family=trace_family,
        payload=body.model_dump(),
    )
    if not score:
        raise HTTPException(status_code=404, detail="Trace not found")
    return AgentTraceScore(**score)


@router.post("/ingest/traces", response_model=AgentTraceIngestResponse, status_code=202)
async def ingest_eval_trace(
    body: AgentTraceIngestRequest,
    request: Request,
    auth: AuthContext = Depends(get_auth_context),
) -> AgentTraceIngestResponse:
    _require_supported_family(body.trace.trace_family)
    _require_eval_run_access(request, auth)

    result = await _get_trace_repository(request).ingest_trace(
        tenant_id=auth.tenant_id,
        created_by=auth.user_id,
        payload=body.model_dump(),
        enqueue=body.enqueue,
    )
    return AgentTraceIngestResponse(**result)


@router.get("/threads/{thread_id}", response_model=EvalTraceThreadResponse)
async def get_eval_trace_thread(
    thread_id: str,
    request: Request,
    limit: Annotated[int, Query(ge=1, le=500)] = 100,
    auth: AuthContext = Depends(get_auth_context),
) -> EvalTraceThreadResponse:
    _require_eval_trace_access(request, auth)
    result = await _get_trace_repository(request).get_thread(
        tenant_id=auth.tenant_id,
        thread_id=thread_id,
        user_id=_scoped_user_id(auth),
        limit=limit,
    )
    return EvalTraceThreadResponse(
        thread_id=result["thread_id"],
        traces=[AgentTraceSummary(**row) for row in result["traces"]],
        total=result["total"],
        metrics=result["metrics"],
    )


@router.get("/traces/{trace_id}/export", response_model=EvalTraceExportResponse)
async def export_eval_trace(
    trace_id: str,
    request: Request,
    format: Annotated[TraceExportFormat, Query()] = "openinference",
    trace_family: Annotated[str, Query(description="Trace family")] = "assistant",
    auth: AuthContext = Depends(get_auth_context),
) -> EvalTraceExportResponse:
    _require_supported_family(trace_family)
    _require_eval_trace_access(request, auth)
    detail = await _get_trace_repository(request).get_trace_detail(
        tenant_id=auth.tenant_id,
        trace_id=trace_id,
        user_id=_scoped_user_id(auth),
        trace_family=trace_family,
    )
    if not detail:
        raise HTTPException(status_code=404, detail="Trace not found")
    return EvalTraceExportResponse(
        trace_id=trace_id,
        format=format,
        redaction_policy=EXPORT_REDACTION_POLICY,
        payload=export_trace(detail, format),
    )


@router.get("/datasets", response_model=EvalDatasetListResponse)
async def list_eval_datasets(
    request: Request,
    limit: Annotated[int, Query(ge=1, le=200)] = 50,
    offset: Annotated[int, Query(ge=0)] = 0,
    auth: AuthContext = Depends(get_auth_context),
) -> EvalDatasetListResponse:
    _require_eval_trace_access(request, auth)
    rows, total = await _get_trace_repository(request).list_datasets(
        tenant_id=auth.tenant_id,
        limit=limit,
        offset=offset,
    )
    return EvalDatasetListResponse(
        datasets=[EvalDataset(**row) for row in rows],
        total=total,
        limit=limit,
        offset=offset,
    )


@router.get("/datasets/{dataset_id}", response_model=EvalDataset)
async def get_eval_dataset(
    dataset_id: str,
    request: Request,
    auth: AuthContext = Depends(get_auth_context),
) -> EvalDataset:
    _require_eval_trace_access(request, auth)
    dataset = await _get_trace_repository(request).get_dataset(
        tenant_id=auth.tenant_id,
        dataset_id=dataset_id,
    )
    if not dataset:
        raise HTTPException(status_code=404, detail="Dataset not found")
    return EvalDataset(**dataset)


@router.get("/datasets/{dataset_id}/examples", response_model=EvalExampleListResponse)
async def list_eval_examples(
    dataset_id: str,
    request: Request,
    split: Annotated[str | None, Query()] = None,
    limit: Annotated[int, Query(ge=1, le=500)] = 200,
    offset: Annotated[int, Query(ge=0)] = 0,
    auth: AuthContext = Depends(get_auth_context),
) -> EvalExampleListResponse:
    _require_eval_trace_access(request, auth)
    rows, total = await _get_trace_repository(request).list_examples(
        tenant_id=auth.tenant_id,
        dataset_id=dataset_id,
        split=split,
        limit=limit,
        offset=offset,
    )
    return EvalExampleListResponse(
        examples=[EvalExample(**row) for row in rows],
        total=total,
        limit=limit,
        offset=offset,
    )


@router.patch("/datasets/{dataset_id}/examples/{example_id}", response_model=EvalExample)
async def update_eval_example(
    dataset_id: str,
    example_id: str,
    body: EvalExampleUpdate,
    request: Request,
    auth: AuthContext = Depends(get_auth_context),
) -> EvalExample:
    _require_eval_run_access(request, auth)
    patch = body.model_dump(exclude_unset=True)
    patch_metadata = dict(patch.get("metadata") or {})
    for key in ("tags", "difficulty", "owner", "review_status"):
        if key in patch:
            patch_metadata[key] = patch[key]
    validation_case = {
        "case_id": example_id,
        "split": patch.get("split") or "regression",
        "input": patch.get("input") or {},
        "expected_output": patch.get("expected_output") or {},
        "expected_trajectory": patch.get("expected_trajectory") or {},
        "assertions": patch.get("assertions") or [],
        "metadata": patch_metadata,
    }
    errors = validate_case(validation_case)
    if errors:
        raise HTTPException(status_code=422, detail={"case_id": example_id, "errors": errors})
    example = await _get_trace_repository(request).update_example(
        tenant_id=auth.tenant_id,
        dataset_id=dataset_id,
        example_id=example_id,
        payload=patch,
    )
    if not example:
        raise HTTPException(status_code=404, detail="Example not found")
    return EvalExample(**example)


@router.post(
    "/datasets/{dataset_id}/examples:import",
    response_model=EvalExamplesImportResponse,
    status_code=201,
)
async def import_eval_examples(
    dataset_id: str,
    body: EvalExamplesImportRequest,
    request: Request,
    auth: AuthContext = Depends(get_auth_context),
) -> EvalExamplesImportResponse:
    _require_eval_run_access(request, auth)
    examples = [example.model_dump() for example in body.examples]
    validation_errors = [
        {"case_id": example.get("case_id"), "errors": errors}
        for example in examples
        if (errors := validate_case(example))
    ]
    if validation_errors:
        raise HTTPException(status_code=422, detail={"cases": validation_errors})
    result = await _get_trace_repository(request).import_examples(
        tenant_id=auth.tenant_id,
        dataset_id=dataset_id,
        created_by=auth.user_id,
        examples=examples,
        mode=body.mode,
    )
    return EvalExamplesImportResponse(
        imported=result.get("imported", 0),
        skipped=result.get("skipped", 0),
        examples=[EvalExample(**example) for example in result.get("examples", [])],
    )


@router.get("/datasets/{dataset_id}/examples:export", response_model=EvalExamplesExportResponse)
async def export_eval_examples(
    dataset_id: str,
    request: Request,
    split: Annotated[str | None, Query()] = None,
    auth: AuthContext = Depends(get_auth_context),
) -> EvalExamplesExportResponse:
    _require_eval_trace_access(request, auth)
    repo = _get_trace_repository(request)
    dataset = await repo.get_dataset(tenant_id=auth.tenant_id, dataset_id=dataset_id)
    if not dataset:
        raise HTTPException(status_code=404, detail="Dataset not found")
    rows, _total = await repo.list_examples(
        tenant_id=auth.tenant_id,
        dataset_id=dataset_id,
        split=split,
        limit=500,
        offset=0,
    )
    export_items = []
    for row in rows:
        metadata = row.get("metadata") or {}
        export_items.append(
            {
                "case_id": metadata.get("case_id") or row.get("example_id"),
                "split": row.get("split") or "regression",
                "input": row.get("input") or {},
                "expected_output": row.get("expected_output") or {},
                "expected_trajectory": metadata.get("expected_trajectory") or {},
                "assertions": metadata.get("assertions") or [],
                "metadata": {
                    key: value
                    for key, value in metadata.items()
                    if key not in {"case_id", "expected_trajectory", "assertions"}
                },
                "source_trace_id": row.get("source_trace_id"),
                "source_span_id": row.get("source_span_id"),
            }
        )
    return EvalExamplesExportResponse(
        dataset=EvalDataset(**dataset),
        examples=export_items,
    )


@router.post("/datasets", response_model=EvalDataset, status_code=201)
async def create_eval_dataset(
    body: EvalDatasetCreate,
    request: Request,
    auth: AuthContext = Depends(get_auth_context),
) -> EvalDataset:
    _require_eval_run_access(request, auth)
    dataset = await _get_trace_repository(request).create_dataset(
        tenant_id=auth.tenant_id,
        created_by=auth.user_id,
        payload=body.model_dump(by_alias=True),
    )
    return EvalDataset(**dataset)


@router.post("/datasets/{dataset_id}/examples:from-trace", response_model=EvalExample, status_code=201)
async def create_eval_example_from_trace(
    dataset_id: str,
    body: EvalExampleFromTraceCreate,
    request: Request,
    auth: AuthContext = Depends(get_auth_context),
) -> EvalExample:
    _require_supported_family(body.trace_family)
    _require_eval_run_access(request, auth)
    example = await _get_trace_repository(request).create_example_from_trace(
        tenant_id=auth.tenant_id,
        dataset_id=dataset_id,
        created_by=auth.user_id,
        user_id=_scoped_user_id(auth),
        trace_family=body.trace_family,
        payload=body.model_dump(),
    )
    if not example:
        raise HTTPException(status_code=404, detail="Trace not found")
    return EvalExample(**example)


@router.post("/trace-feedback:preview", response_model=EvalTraceFeedbackResponse)
async def preview_eval_trace_feedback(
    body: EvalTraceFeedbackRequest,
    request: Request,
    auth: AuthContext = Depends(get_auth_context),
) -> EvalTraceFeedbackResponse:
    _require_supported_family(body.trace_family)
    _require_eval_run_access(request, auth)
    repo = _get_trace_repository(request)
    if body.dataset_id:
        dataset = await repo.get_dataset(
            tenant_id=auth.tenant_id,
            dataset_id=body.dataset_id,
        )
        if not dataset:
            raise HTTPException(status_code=404, detail=f"Dataset not found: {body.dataset_id}")
    proposed_by = body.proposed_by or f"eval-feedback:{auth.user_id or 'system'}"
    patterns = []
    dataset_cases = []
    seen_trace_ids: set[str] = set()
    for trace_id in body.trace_ids:
        if trace_id in seen_trace_ids:
            continue
        seen_trace_ids.add(trace_id)
        detail = await repo.get_trace_detail(
            tenant_id=auth.tenant_id,
            trace_id=trace_id,
            user_id=_scoped_user_id(auth),
            trace_family=body.trace_family,
        )
        if not detail:
            raise HTTPException(status_code=404, detail=f"Trace not found: {trace_id}")
        pattern = classify_trace_failure(
            detail,
            low_score_threshold=body.low_score_threshold,
            latency_threshold_ms=body.latency_threshold_ms,
        )
        patterns.append(pattern)
        dataset_cases.append(
            build_redacted_dataset_case(
                detail,
                pattern,
                split=body.split,
            )
        )

    clusters = cluster_failure_patterns(patterns)
    import_request = (
        EvalExamplesImportRequest(examples=dataset_cases) if body.dataset_id else None
    )
    return EvalTraceFeedbackResponse(
        trace_family=body.trace_family,
        dataset_id=body.dataset_id,
        patterns=[
            EvalTraceFailurePattern(
                trace_id=pattern.trace_id,
                trace_family=pattern.trace_family,
                failure_mode=pattern.failure_mode,
                reasons=pattern.reasons,
                severity=pattern.severity,
            )
            for pattern in patterns
        ],
        clusters=clusters,
        dataset_cases=dataset_cases,
        import_request=import_request,
        proposals=[
            build_harness_profile_proposal(cluster, proposed_by=proposed_by)
            for cluster in clusters
        ],
        redaction_policy=EXPORT_REDACTION_POLICY,
    )


@router.get("/evaluators", response_model=EvalEvaluatorListResponse)
async def list_eval_evaluators(
    request: Request,
    limit: Annotated[int, Query(ge=1, le=200)] = 50,
    offset: Annotated[int, Query(ge=0)] = 0,
    auth: AuthContext = Depends(get_auth_context),
) -> EvalEvaluatorListResponse:
    _require_eval_trace_access(request, auth)
    rows, total = await _get_trace_repository(request).list_evaluators(
        tenant_id=auth.tenant_id,
        limit=limit,
        offset=offset,
    )
    return EvalEvaluatorListResponse(
        evaluators=[EvalEvaluator(**row) for row in rows],
        total=total,
        limit=limit,
        offset=offset,
    )


@router.get("/evaluators/{evaluator_id}", response_model=EvalEvaluator)
async def get_eval_evaluator(
    evaluator_id: str,
    request: Request,
    auth: AuthContext = Depends(get_auth_context),
) -> EvalEvaluator:
    _require_eval_trace_access(request, auth)
    evaluator = await _get_trace_repository(request).get_evaluator(
        tenant_id=auth.tenant_id,
        evaluator_id=evaluator_id,
    )
    if not evaluator:
        raise HTTPException(status_code=404, detail="Evaluator not found")
    return EvalEvaluator(**evaluator)


@router.post("/evaluators", response_model=EvalEvaluator, status_code=201)
async def create_eval_evaluator(
    body: EvalEvaluatorCreate,
    request: Request,
    auth: AuthContext = Depends(get_auth_context),
) -> EvalEvaluator:
    _require_eval_run_access(request, auth)
    evaluator = await _get_trace_repository(request).create_evaluator(
        tenant_id=auth.tenant_id,
        created_by=auth.user_id,
        payload=body.model_dump(),
    )
    return EvalEvaluator(**evaluator)


@router.get("/experiments", response_model=EvalExperimentListResponse)
async def list_eval_experiments(
    request: Request,
    limit: Annotated[int, Query(ge=1, le=200)] = 50,
    offset: Annotated[int, Query(ge=0)] = 0,
    auth: AuthContext = Depends(get_auth_context),
) -> EvalExperimentListResponse:
    _require_eval_trace_access(request, auth)
    rows, total = await _get_trace_repository(request).list_experiments(
        tenant_id=auth.tenant_id,
        limit=limit,
        offset=offset,
    )
    return EvalExperimentListResponse(
        experiments=[EvalExperiment(**row) for row in rows],
        total=total,
        limit=limit,
        offset=offset,
    )


@router.post("/experiments", response_model=EvalExperiment, status_code=201)
async def create_eval_experiment(
    body: EvalExperimentCreate,
    request: Request,
    auth: AuthContext = Depends(get_auth_context),
) -> EvalExperiment:
    _require_eval_run_access(request, auth)
    experiment = await _get_trace_repository(request).create_experiment(
        tenant_id=auth.tenant_id,
        created_by=auth.user_id,
        payload=body.model_dump(),
    )
    return EvalExperiment(**experiment)


@router.get("/experiments/{experiment_id}", response_model=EvalExperiment)
async def get_eval_experiment(
    experiment_id: str,
    request: Request,
    auth: AuthContext = Depends(get_auth_context),
) -> EvalExperiment:
    _require_eval_trace_access(request, auth)
    experiment = await _get_trace_repository(request).get_experiment(
        tenant_id=auth.tenant_id,
        experiment_id=experiment_id,
    )
    if not experiment:
        raise HTTPException(status_code=404, detail="Experiment not found")
    return EvalExperiment(**experiment)


@router.get("/experiment-runs/{run_id}", response_model=EvalExperimentRun)
async def get_eval_experiment_run(
    run_id: str,
    request: Request,
    auth: AuthContext = Depends(get_auth_context),
) -> EvalExperimentRun:
    _require_eval_trace_access(request, auth)
    repo = _get_trace_repository(request)
    run = await repo.get_experiment_run(
        tenant_id=auth.tenant_id,
        run_id=run_id,
    )
    if not run:
        raise HTTPException(status_code=404, detail="Experiment run not found")
    run = await _hydrate_live_run(repo, tenant_id=auth.tenant_id, run=run)
    return EvalExperimentRun(**run)


@router.get(
    "/experiment-runs/{run_id}/results",
    response_model=EvalExperimentRunResultsResponse,
)
async def get_eval_experiment_run_results(
    run_id: str,
    request: Request,
    limit: Annotated[int, Query(ge=1, le=200)] = 50,
    offset: Annotated[int, Query(ge=0)] = 0,
    auth: AuthContext = Depends(get_auth_context),
) -> EvalExperimentRunResultsResponse:
    _require_eval_trace_access(request, auth)
    repo = _get_trace_repository(request)
    run = await repo.get_experiment_run(tenant_id=auth.tenant_id, run_id=run_id)
    if not run:
        raise HTTPException(status_code=404, detail="Experiment run not found")
    run = await _hydrate_live_run(repo, tenant_id=auth.tenant_id, run=run)
    cases, total = await repo.list_experiment_run_case_results(
        tenant_id=auth.tenant_id,
        run_id=run_id,
        limit=limit,
        offset=offset,
    )
    return EvalExperimentRunResultsResponse(
        run=EvalExperimentRun(**run),
        cases=cases,
        total=total,
        limit=limit,
        offset=offset,
    )


@router.get("/experiment-runs:compare", response_model=EvalExperimentRunComparisonResponse)
async def compare_eval_experiment_runs(
    request: Request,
    baseline_run_id: Annotated[str, Query()],
    candidate_run_id: Annotated[str, Query()],
    auth: AuthContext = Depends(get_auth_context),
) -> EvalExperimentRunComparisonResponse:
    _require_eval_trace_access(request, auth)
    comparison = await _get_trace_repository(request).compare_experiment_runs(
        tenant_id=auth.tenant_id,
        baseline_run_id=baseline_run_id,
        candidate_run_id=candidate_run_id,
    )
    if not comparison:
        raise HTTPException(status_code=404, detail="Experiment run not found")
    return EvalExperimentRunComparisonResponse(**comparison)


@router.post("/experiments/{experiment_id}:run", response_model=EvalExperimentRunBatchResponse, status_code=202)
async def run_eval_experiment(
    experiment_id: str,
    body: EvalExperimentRunCreate,
    request: Request,
    auth: AuthContext = Depends(get_auth_context),
) -> EvalExperimentRunBatchResponse:
    _require_eval_run_access(request, auth)
    repo = _get_trace_repository(request)
    experiment = await repo.get_experiment(tenant_id=auth.tenant_id, experiment_id=experiment_id)
    if not experiment:
        raise HTTPException(status_code=404, detail="Experiment not found")
    dataset_id = body.dataset_id or experiment.get("dataset_id")
    trace_id = body.target_snapshot.get("trace_id")
    if dataset_id and trace_id:
        raise HTTPException(
            status_code=422,
            detail="Dataset and trace targets are mutually exclusive",
        )
    if body.run_mode == "live_candidate":
        if not dataset_id:
            raise HTTPException(status_code=422, detail="live_candidate requires a dataset")
        examples = await repo.list_example_manifest(
            tenant_id=auth.tenant_id,
            dataset_id=str(dataset_id),
        )
        if not examples:
            raise HTTPException(status_code=422, detail="Dataset has no examples")
        invalid_cases: list[dict[str, Any]] = []
        for example in examples:
            input_payload = example.get("input") if isinstance(example.get("input"), dict) else {}
            errors = validate_case(_golden_case_from_example(example))
            if not str(input_payload.get("message") or "").strip():
                errors.append("input.message must be a non-empty executable message")
            metadata = example.get("metadata") if isinstance(example.get("metadata"), dict) else {}
            if metadata.get("behavior_confirmed") is False:
                errors.append("expected behavior must be confirmed before live execution")
            if errors:
                invalid_cases.append(
                    {
                        "case_id": str(
                            metadata.get("case_id") or example.get("example_id") or "unknown"
                        ),
                        "errors": errors,
                    }
                )
        if invalid_cases:
            raise HTTPException(
                status_code=422,
                detail={
                    "message": "live_candidate dataset contains invalid behavior contracts",
                    "cases": invalid_cases[:20],
                },
            )
        evaluators = []
        for evaluator_id in body.evaluator_ids:
            evaluator = await repo.get_evaluator(
                tenant_id=auth.tenant_id,
                evaluator_id=evaluator_id,
            )
            if not evaluator:
                raise HTTPException(status_code=404, detail=f"Evaluator not found: {evaluator_id}")
            evaluators.append(evaluator)

        repetitions = body.repetitions or 3
        prompt_override = body.candidate_config.system_prompt_override
        prompt_override_hash = (
            hashlib.sha256(prompt_override.encode("utf-8")).hexdigest() if prompt_override else None
        )
        execution_config = {
            **(experiment.get("target_config") or {}),
            **body.target_snapshot,
            "system_prompt_override": prompt_override,
        }
        public_target = {
            key: value
            for key, value in body.target_snapshot.items()
            if key
            not in {
                "system_prompt",
                "system_prompt_override",
                "eval_system_prompt_override",
            }
        }
        public_target.update(
            {
                "candidate_label": body.candidate_label,
                "baseline_label": body.baseline_label,
                "prompt_override_hash": prompt_override_hash,
            }
        )
        candidate_fingerprint = {
            "prompt_override_hash": prompt_override_hash,
            "requested_model_id": execution_config.get("model_id"),
            "requested_temperature": execution_config.get("temperature"),
            "requested_execution_profile": execution_config.get("execution_profile"),
            "verification": "pending",
        }
        try:
            job = await repo.enqueue_live_experiment_run(
                tenant_id=auth.tenant_id,
                experiment_id=experiment_id,
                dataset_id=str(dataset_id),
                evaluator_snapshots=evaluators,
                examples=examples,
                repetitions=repetitions,
                created_by=auth.user_id,
                target_snapshot=public_target,
                execution_config=execution_config,
                candidate_fingerprint=candidate_fingerprint,
                baseline_run_id=body.baseline_run_id or experiment.get("baseline_run_id"),
            )
        except ValueError as exc:
            raise HTTPException(status_code=422, detail=str(exc)) from exc
        return EvalExperimentRunBatchResponse(jobs=[EvalAsyncJobResponse(**job)])

    jobs = []
    for evaluator_id in body.evaluator_ids:
        job = await repo.enqueue_evaluator_run(
            tenant_id=auth.tenant_id,
            evaluator_id=evaluator_id,
            created_by=auth.user_id,
            payload={
                "experiment_id": experiment_id,
                "dataset_id": dataset_id,
                "trace_id": trace_id,
                "target_snapshot": {
                    **body.target_snapshot,
                    "run_mode": "rescore_trace",
                    "candidate_label": body.candidate_label,
                    "baseline_label": body.baseline_label,
                },
                "metadata": body.metadata,
            },
        )
        jobs.append(EvalAsyncJobResponse(**job))
    return EvalExperimentRunBatchResponse(jobs=jobs)


@router.post(
    "/experiments/{experiment_id}:promote-baseline",
    response_model=EvalBaselinePromotionResponse,
)
async def promote_eval_experiment_baseline(
    experiment_id: str,
    body: EvalBaselinePromotionRequest,
    request: Request,
    auth: AuthContext = Depends(get_auth_context),
) -> EvalBaselinePromotionResponse:
    _require_eval_run_access(request, auth)
    repo = _get_trace_repository(request)
    experiment = await repo.get_experiment(tenant_id=auth.tenant_id, experiment_id=experiment_id)
    run = await repo.get_experiment_run(tenant_id=auth.tenant_id, run_id=body.run_id)
    if not experiment or not run or run.get("experiment_id") != experiment_id:
        raise HTTPException(status_code=404, detail="Experiment run not found")
    if run.get("run_mode") != "live_candidate" or run.get("status") != "succeeded":
        raise HTTPException(
            status_code=409, detail="Baseline requires a succeeded live_candidate run"
        )
    metrics = run.get("metrics") if isinstance(run.get("metrics"), dict) else {}
    raw_summary = (
        run.get("score_summary") if isinstance(run.get("score_summary"), dict) else {}
    )
    try:
        summary = EvalGateMetricsV2.model_validate(raw_summary).model_dump()
    except ValidationError as exc:
        raise HTTPException(
            status_code=409,
            detail={
                "error": "incompatible_gate_metrics_schema",
                "required_schema_version": "eval-gate-metrics/v2",
                "validation_errors": exc.errors(include_url=False),
            },
        ) from exc
    release_gate = apply_gate(
        summary,
        require_critical_coverage=True,
        require_stateful_coverage=True,
    )
    if release_gate.get("status") != "pass":
        raise HTTPException(
            status_code=409,
            detail={
                "error": "baseline_quality_gate_failed",
                "failures": release_gate.get("failures") or [],
            },
        )
    critical_pass_rate = summary["critical_pass_rate"]
    if critical_pass_rate is None or float(critical_pass_rate) < 1.0:
        raise HTTPException(status_code=409, detail="All critical behavior cases must pass")
    hard_blocker_results = (
        metrics.get("hard_blocker_results")
        if isinstance(metrics.get("hard_blocker_results"), dict)
        else {}
    )
    required_hard_blockers = metrics.get("required_hard_blockers")
    metrics_critical_case_count = metrics.get("critical_case_count")
    if (
        isinstance(metrics_critical_case_count, bool)
        or not isinstance(metrics_critical_case_count, int)
        or metrics_critical_case_count != summary["critical_case_count"]
        or metrics_critical_case_count < 1
        or not isinstance(required_hard_blockers, list)
        or len(required_hard_blockers) != len(REQUIRED_ASSISTANT_HARD_BLOCKERS)
        or set(required_hard_blockers) != set(REQUIRED_ASSISTANT_HARD_BLOCKERS)
        or set(hard_blocker_results) != set(REQUIRED_ASSISTANT_HARD_BLOCKERS)
        or metrics.get("hard_blockers_passed") is not True
        or any(
            hard_blocker_results.get(case_id) is not True
            for case_id in REQUIRED_ASSISTANT_HARD_BLOCKERS
        )
    ):
        raise HTTPException(
            status_code=409,
            detail="Baseline requires all mandatory Assistant safety blockers",
        )
    sha256_chars = frozenset("0123456789abcdef")
    if any(
        len(value := str(run.get(field) or "")) != 64
        or any(char not in sha256_chars for char in value)
        for field in ("dataset_manifest_hash", "evaluator_suite_hash")
    ):
        raise HTTPException(
            status_code=409,
            detail="Baseline requires verified dataset and evaluator provenance",
        )
    if metrics.get("mixed_runtime") is not False:
        raise HTTPException(
            status_code=409,
            detail="Baseline requires an explicit single-runtime fingerprint cohort",
        )
    run_gate = metrics.get("gate") if isinstance(metrics.get("gate"), dict) else {}
    trial_fields = {
        field: metrics.get(field)
        for field in (
            "attempted_trials",
            "completed_trials",
            "failed_trials",
            "total_trials",
        )
    }
    valid_trial_receipt = all(
        not isinstance(value, bool) and isinstance(value, int) and value >= 0
        for value in trial_fields.values()
    )
    attempted_trials = trial_fields["attempted_trials"]
    completed_trials = trial_fields["completed_trials"]
    failed_trials = trial_fields["failed_trials"]
    total_trials = trial_fields["total_trials"]
    if (
        run_gate.get("status") != "pass"
        or not valid_trial_receipt
        or total_trials == 0
        or attempted_trials != total_trials
        or completed_trials != total_trials
        or failed_trials != 0
        or completed_trials + failed_trials != attempted_trials
    ):
        raise HTTPException(
            status_code=409, detail="Baseline requires a complete, error-free live run"
        )
    actual_fingerprint = (
        metrics.get("actual_fingerprint")
        if isinstance(metrics.get("actual_fingerprint"), dict)
        else {}
    )
    required_fingerprint_keys = (
        "system_prompt_hash",
        "tool_schema_hash",
        "model_id",
        "provider",
        "runtime_revision",
    )
    if any(not actual_fingerprint.get(key) for key in required_fingerprint_keys):
        raise HTTPException(
            status_code=409, detail="Baseline requires a complete verified runtime fingerprint"
        )
    current_baseline = experiment.get("baseline_run_id")
    if current_baseline and current_baseline != body.run_id:
        comparison = await repo.compare_experiment_runs(
            tenant_id=auth.tenant_id,
            baseline_run_id=str(current_baseline),
            candidate_run_id=body.run_id,
        )
        comparison_gate = (
            comparison.get("gate")
            if comparison and isinstance(comparison.get("gate"), dict)
            else {}
        )
        if not comparison or comparison_gate.get("status") != "pass":
            raise HTTPException(
                status_code=409, detail="Candidate must pass the current baseline gate"
            )
    promoted = await repo.promote_experiment_baseline(
        tenant_id=auth.tenant_id,
        experiment_id=experiment_id,
        run_id=body.run_id,
        promoted_by=auth.user_id,
        expected_previous_baseline_run_id=(
            str(current_baseline) if current_baseline else None
        ),
    )
    if not promoted:
        raise HTTPException(status_code=409, detail="Run is not eligible for baseline promotion")
    return EvalBaselinePromotionResponse(**promoted)


@router.post("/evaluators/{evaluator_id}:run-async", response_model=EvalAsyncJobResponse, status_code=202)
async def run_eval_evaluator_async(
    evaluator_id: str,
    body: EvalEvaluatorRunRequest,
    request: Request,
    auth: AuthContext = Depends(get_auth_context),
) -> EvalAsyncJobResponse:
    _require_eval_run_access(request, auth)
    job = await _get_trace_repository(request).enqueue_evaluator_run(
        tenant_id=auth.tenant_id,
        evaluator_id=evaluator_id,
        created_by=auth.user_id,
        payload=body.model_dump(),
    )
    return EvalAsyncJobResponse(**job)


@router.get("/knowledge/summary", response_model=KbRagasKnowledgeSummaryResponse)
async def get_kb_ragas_summary_endpoint(
    request: Request,
    days: Annotated[int, Query(ge=1, le=90)] = 7,
    dataset_id: Annotated[str | None, Query()] = None,
    auth: AuthContext = Depends(get_auth_context),
) -> KbRagasKnowledgeSummaryResponse:
    _require_eval_trace_access(request, auth)
    summary = await get_kb_ragas_knowledge_summary(
        _get_trace_repository(request),
        tenant_id=auth.tenant_id,
        days=days,
        dataset_id=dataset_id,
    )
    summary_payload = dict(summary)
    metric_items = summary_payload.pop("metrics", [])
    return KbRagasKnowledgeSummaryResponse(
        **summary_payload,
        metrics=[KbRagasMetricSummary(**item) for item in metric_items or []],
    )


@router.post(
    "/knowledge/{dataset_id}/batch-score",
    response_model=KbRagasBatchScoreResponse,
    status_code=202,
)
async def batch_score_kb_ragas_dataset(
    dataset_id: str,
    body: KbRagasBatchScoreRequest,
    request: Request,
    auth: AuthContext = Depends(get_auth_context),
) -> KbRagasBatchScoreResponse:
    _require_eval_run_access(request, auth)
    try:
        result = await batch_score_kb_ragas_traces(
            _get_trace_repository(request),
            tenant_id=auth.tenant_id,
            dataset_id=dataset_id,
            evaluator_id=body.evaluator_id,
            created_by=auth.user_id or "eval-api",
            limit=body.limit,
            only_unscored=body.only_unscored,
        )
    except ValueError as exc:
        raise HTTPException(status_code=400, detail=str(exc)) from exc
    return KbRagasBatchScoreResponse(
        queued=result["queued"],
        skipped=result["skipped"],
        jobs=[EvalAsyncJobResponse(**job) for job in result["jobs"]],
    )


@router.post("/knowledge/score-retrieval", response_model=KbRagasScoreRetrievalResponse)
async def score_kb_ragas_retrieval(
    body: KbRagasScoreRetrievalRequest,
    request: Request,
    auth: AuthContext = Depends(get_auth_context),
) -> KbRagasScoreRetrievalResponse:
    _require_eval_run_access(request, auth)
    try:
        payload = await score_retrieval_with_kb_ragas(
            query=body.query,
            contexts=body.contexts,
            answer=body.answer,
            metrics=body.metrics,
            ground_truth=body.ground_truth,
            llm_config=(
                body.llm_config.model_dump(exclude_none=True)
                if body.llm_config is not None
                else None
            ),
        )
    except RuntimeError as exc:
        raise HTTPException(status_code=502, detail=str(exc)) from exc
    return KbRagasScoreRetrievalResponse(
        judge_model=str(payload.get("judge_model") or ""),
        results=[
            KbRagasScoreRetrievalResult(**item)
            for item in payload.get("results") or []
            if isinstance(item, dict)
        ],
    )


@router.post("/gates:dry-run", response_model=EvalGateDryRunResponse)
async def dry_run_eval_gate(
    body: EvalGateDryRunRequest,
    request: Request,
    auth: AuthContext = Depends(get_auth_context),
) -> EvalGateDryRunResponse:
    _require_eval_run_access(request, auth)
    if bool(body.baseline_run_id) != bool(body.candidate_run_id):
        raise HTTPException(
            status_code=422,
            detail="baseline_run_id and candidate_run_id must be supplied together",
        )
    raw_metrics = body.result_payload.get("metrics") or body.result_payload
    baseline_metrics = None
    compatibility: dict[str, Any] = {}
    authoritative_gate: dict[str, Any] = {}
    if body.baseline_run_id and body.candidate_run_id:
        comparison = await _get_trace_repository(request).compare_experiment_runs(
            tenant_id=auth.tenant_id,
            baseline_run_id=body.baseline_run_id,
            candidate_run_id=body.candidate_run_id,
        )
        if not comparison:
            raise HTTPException(status_code=404, detail="Experiment run not found")
        raw_metrics = comparison.get("candidate_summary") or {}
        baseline_metrics = comparison.get("baseline_summary")
        compatibility = (
            comparison.get("compatibility")
            if isinstance(comparison.get("compatibility"), dict)
            else {}
        )
        authoritative_gate = (
            comparison.get("gate") if isinstance(comparison.get("gate"), dict) else {}
        )
    try:
        metrics = EvalGateMetricsV2.model_validate(raw_metrics).model_dump()
        validated_baseline = (
            EvalGateMetricsV2.model_validate(baseline_metrics).model_dump()
            if baseline_metrics is not None
            else None
        )
    except ValidationError as exc:
        raise HTTPException(
            status_code=422,
            detail={
                "error": "incompatible_gate_metrics_schema",
                "required_schema_version": "eval-gate-metrics/v2",
                "validation_errors": exc.errors(include_url=False),
            },
        ) from exc
    gate = apply_gate(
        metrics,
        thresholds=body.thresholds,
        baseline_metrics=validated_baseline,
        require_critical_coverage=True,
        require_stateful_coverage=True,
    )
    authoritative_failures: list[str] = []
    if body.baseline_run_id and body.candidate_run_id:
        compatible = compatibility.get("compatible") is True and compatibility.get(
            "status"
        ) == "compatible"
        if not compatible:
            reasons = ", ".join(str(item) for item in compatibility.get("reasons") or [])
            authoritative_failures.append(
                f"authoritative run comparison is incompatible: {reasons or 'unknown'}"
            )
        if authoritative_gate.get("status") != "pass":
            reasons = ", ".join(
                str(item) for item in authoritative_gate.get("failures") or []
            )
            authoritative_failures.append(
                f"authoritative run comparison gate failed: {reasons or 'unknown'}"
            )
    if authoritative_failures:
        gate["status"] = "fail"
        gate["failures"] = list(dict.fromkeys([*gate["failures"], *authoritative_failures]))
    return EvalGateDryRunResponse(
        status=gate["status"],
        thresholds=gate["thresholds"],
        metrics=gate["metrics"],
        failures=gate["failures"],
        skipped_thresholds=gate["skipped_thresholds"],
        coverage=gate["coverage"],
        compatibility=compatibility,
        authoritative_gate=authoritative_gate,
        report={
            "source": "api-dry-run",
            "gate_profile": "release",
            "baseline_run_id": body.baseline_run_id,
            "candidate_run_id": body.candidate_run_id,
            "compatibility": compatibility,
            "authoritative_gate": authoritative_gate,
        },
    )
