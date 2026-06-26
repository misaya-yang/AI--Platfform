from __future__ import annotations

from typing import Annotated

from ai_gateway_core.persistence.repositories.agent_trace_repository import (
    AgentTraceRepository,
)
from fastapi import APIRouter, Depends, HTTPException, Query, Request

from ...api.deps import AuthContext, get_auth_context, require_gateway_capability
from ...core.auth.permissions import Capability, build_permission_denied_detail
from ...persistence.database import DatabaseStorage
from ..eval_export import export_trace
from ..schemas.eval import (
    AgentTraceDetailResponse,
    AgentTraceIngestRequest,
    AgentTraceIngestResponse,
    AgentTraceListResponse,
    AgentTraceScore,
    AgentTraceScoreCreate,
    AgentTraceSummary,
    EvalAsyncJobResponse,
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
    EvalExperiment,
    EvalExperimentCreate,
    EvalExperimentListResponse,
    EvalExperimentRun,
    EvalTraceExportResponse,
    EvalTraceMonitoringSummary,
    EvalTraceThreadResponse,
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
