from __future__ import annotations

from typing import Annotated

from ai_gateway_core.persistence.repositories.agent_trace_repository import (
    AgentTraceRepository,
)
from fastapi import APIRouter, Depends, HTTPException, Query, Request

from ...api.deps import AuthContext, get_auth_context, require_gateway_capability
from ...core.auth.permissions import Capability, build_permission_denied_detail
from ...persistence.database import DatabaseStorage
from ...services.eval.golden import apply_gate
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
    EvalGateDryRunRequest,
    EvalGateDryRunResponse,
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
    example = await _get_trace_repository(request).update_example(
        tenant_id=auth.tenant_id,
        dataset_id=dataset_id,
        example_id=example_id,
        payload=body.model_dump(exclude_unset=True),
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
    result = await _get_trace_repository(request).import_examples(
        tenant_id=auth.tenant_id,
        dataset_id=dataset_id,
        created_by=auth.user_id,
        examples=[example.model_dump() for example in body.examples],
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
    run = await _get_trace_repository(request).get_experiment_run(
        tenant_id=auth.tenant_id,
        run_id=run_id,
    )
    if not run:
        raise HTTPException(status_code=404, detail="Experiment run not found")
    return EvalExperimentRun(**run)


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
                    "candidate_label": body.candidate_label,
                    "baseline_label": body.baseline_label,
                },
                "metadata": body.metadata,
            },
        )
        jobs.append(EvalAsyncJobResponse(**job))
    return EvalExperimentRunBatchResponse(jobs=jobs)


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
            metrics=body.metrics,
            ground_truth=body.ground_truth,
            llm_config=body.llm_config,
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
    metrics = dict(body.result_payload.get("metrics") or body.result_payload)
    baseline_metrics = None
    if body.baseline_run_id and body.candidate_run_id:
        comparison = await _get_trace_repository(request).compare_experiment_runs(
            tenant_id=auth.tenant_id,
            baseline_run_id=body.baseline_run_id,
            candidate_run_id=body.candidate_run_id,
        )
        if not comparison:
            raise HTTPException(status_code=404, detail="Experiment run not found")
        metrics = comparison.get("candidate_summary") or metrics
        baseline_metrics = comparison.get("baseline_summary")
    gate = apply_gate(metrics, thresholds=body.thresholds, baseline_metrics=baseline_metrics)
    return EvalGateDryRunResponse(
        status=gate["status"],
        thresholds=gate["thresholds"],
        metrics=gate["metrics"],
        failures=gate["failures"],
        report={"source": "api-dry-run", "baseline_run_id": body.baseline_run_id},
    )
