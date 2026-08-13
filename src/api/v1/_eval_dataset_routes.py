"""Eval dataset and trace-feedback route family."""

from __future__ import annotations

from collections.abc import Callable
from dataclasses import dataclass
from typing import Annotated, Any

from ai_gateway_core.persistence.repositories.agent_trace_repository import (
    AgentTraceRepository,
)
from fastapi import APIRouter, Depends, HTTPException, Query, Request

from ...services.eval.golden import validate_case
from ...services.eval.trace_feedback import (
    build_harness_profile_proposal,
    build_redacted_dataset_case,
    classify_trace_failure,
    cluster_failure_patterns,
)
from ..deps import AuthContext, get_auth_context
from ..eval_export import EXPORT_REDACTION_POLICY
from ..schemas.eval import (
    EvalDataset,
    EvalDatasetCreate,
    EvalDatasetListResponse,
    EvalExample,
    EvalExampleFromTraceCreate,
    EvalExampleListResponse,
    EvalExamplesExportResponse,
    EvalExamplesImportRequest,
    EvalExamplesImportResponse,
    EvalExampleUpdate,
    EvalTraceFailurePattern,
    EvalTraceFeedbackRequest,
    EvalTraceFeedbackResponse,
)


@dataclass(frozen=True)
class EvalDatasetRouteDependencies:
    get_trace_repository: Callable[[Request], AgentTraceRepository]
    require_trace_access: Callable[[Request, AuthContext], None]
    require_run_access: Callable[[Request, AuthContext], None]
    require_supported_family: Callable[[str], None]
    scoped_user_id: Callable[[AuthContext, str | None], str | None]


@dataclass(frozen=True)
class EvalDatasetRouteFamily:
    router: APIRouter
    list_eval_datasets: Callable[..., Any]
    get_eval_dataset: Callable[..., Any]
    list_eval_examples: Callable[..., Any]
    update_eval_example: Callable[..., Any]
    import_eval_examples: Callable[..., Any]
    export_eval_examples: Callable[..., Any]
    create_eval_dataset: Callable[..., Any]
    create_eval_example_from_trace: Callable[..., Any]
    preview_eval_trace_feedback: Callable[..., Any]


def build_eval_dataset_routes(
    dependencies: EvalDatasetRouteDependencies,
) -> EvalDatasetRouteFamily:
    """Build dataset routes against facade-owned dependency seams."""
    router = APIRouter()
    _get_trace_repository = dependencies.get_trace_repository
    _require_eval_trace_access = dependencies.require_trace_access
    _require_eval_run_access = dependencies.require_run_access
    _require_supported_family = dependencies.require_supported_family
    _scoped_user_id = dependencies.scoped_user_id

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

    @router.post(
        "/datasets/{dataset_id}/examples:from-trace", response_model=EvalExample, status_code=201
    )
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

    return EvalDatasetRouteFamily(
        router=router,
        list_eval_datasets=list_eval_datasets,
        get_eval_dataset=get_eval_dataset,
        list_eval_examples=list_eval_examples,
        update_eval_example=update_eval_example,
        import_eval_examples=import_eval_examples,
        export_eval_examples=export_eval_examples,
        create_eval_dataset=create_eval_dataset,
        create_eval_example_from_trace=create_eval_example_from_trace,
        preview_eval_trace_feedback=preview_eval_trace_feedback,
    )
