from __future__ import annotations

import asyncio
from typing import Any

from ai_gateway_core.billing.pricing_catalog import resolve_pricing_with_status
from ai_gateway_core.eval import EvalOutboxWorker, EvaluatorExecutor
from ai_gateway_core.logging import get_logger
from ai_gateway_core.persistence.repositories.agent_trace_repository import AgentTraceRepository

from .eval_candidate_client import (
    EVAL_CANDIDATE_USER_ID,
    EvalCandidateClient,
    candidate_fingerprint_from_context,
)
from .eval_llm_client import build_eval_llm_complete, load_eval_llm_settings
from .golden import evaluate_case
from .kb_ragas_client import KbRagasClient, build_kb_ragas_complete

logger = get_logger(__name__)

_eval_outbox_worker: EvalOutboxWorker | None = None
_kb_ragas_client: KbRagasClient | None = None
_eval_candidate_client: EvalCandidateClient | None = None


def _persisted_candidate_fingerprint(detail: dict[str, Any]) -> dict[str, Any]:
    for span in detail.get("spans") or []:
        if not isinstance(span, dict) or span.get("span_kind") != "context_building":
            continue
        attributes = span.get("attributes")
        if isinstance(attributes, dict):
            return candidate_fingerprint_from_context(attributes)
    return {}


async def _kb_ragas_evaluate(**kwargs: Any) -> list[dict[str, Any]]:
    global _kb_ragas_client
    if _kb_ragas_client is None:
        _kb_ragas_client = build_kb_ragas_complete()
    results = await _kb_ragas_client.evaluate_retrieval(**kwargs)
    return [
        {
            "metric": item.metric,
            "score": item.score,
            "explanation": item.explanation,
            "label": item.label,
            "judge_model": item.judge_model,
            "failure_kind": item.failure_kind,
        }
        for item in results
    ]


def _candidate_cost_cents(model_id: str, usage: dict[str, Any]) -> float | None:
    if not any(
        key in usage
        for key in ("input_tokens", "output_tokens", "prompt_tokens", "completion_tokens")
    ):
        return None
    input_tokens = int(usage.get("input_tokens") or usage.get("prompt_tokens") or 0)
    output_tokens = int(usage.get("output_tokens") or usage.get("completion_tokens") or 0)
    pricing, pricing_status = resolve_pricing_with_status(model_id)
    if pricing_status == "unknown":
        return None
    return round(
        (
            (input_tokens / 1000) * float(pricing.get("input") or 0)
            + (output_tokens / 1000) * float(pricing.get("output") or 0)
        )
        * 100,
        6,
    )


def _build_candidate_runner(repository: AgentTraceRepository):
    async def _run_candidate(
        *,
        tenant_id: str,
        run_case: dict[str, Any],
        execution_config: dict[str, Any],
    ) -> dict[str, Any]:
        global _eval_candidate_client
        run_case_id = str(run_case.get("run_case_id") or "")
        trace_id = str(run_case.get("candidate_trace_id") or "")
        detail = None
        if trace_id:
            detail = await repository.get_trace_detail(
                tenant_id=tenant_id,
                trace_id=trace_id,
                trace_family="assistant",
            )
        if detail is None:
            existing, _ = await repository.list_traces(
                tenant_id=tenant_id,
                user_id=EVAL_CANDIDATE_USER_ID,
                trace_family="assistant",
                session_id=run_case_id,
                limit=1,
                offset=0,
            )
            if existing:
                trace_id = str(existing[0].get("trace_id") or "")
                detail = await repository.get_trace_detail(
                    tenant_id=tenant_id,
                    trace_id=trace_id,
                    trace_family="assistant",
                )

        result = None
        if detail is None:
            if _eval_candidate_client is None:
                _eval_candidate_client = EvalCandidateClient()
            input_payload = run_case.get("input") if isinstance(run_case.get("input"), dict) else {}
            message = str(input_payload.get("message") or "").strip()

            result = await _eval_candidate_client.run(
                tenant_id=tenant_id,
                run_case_id=run_case_id,
                message=message,
                config=execution_config,
            )
            trace_id = result.trace_id
            for delay in (0.05, 0.1, 0.2, 0.4, 0.8, 1.6, 3.2, 3.2):
                detail = await repository.get_trace_detail(
                    tenant_id=tenant_id,
                    trace_id=trace_id,
                    trace_family="assistant",
                )
                if detail and (detail.get("trace") or {}).get("status") in {
                    "succeeded",
                    "failed",
                    "cancelled",
                    "timeout",
                }:
                    break
                await asyncio.sleep(delay)
        if not detail:
            raise RuntimeError("Candidate trace persistence timed out")
        if result is not None and result.error:
            raise RuntimeError(result.error)

        trace = detail.get("trace") or {}
        metadata = trace.get("metadata") if isinstance(trace.get("metadata"), dict) else {}
        runtime = (
            metadata.get("runtime_trajectory")
            if isinstance(metadata.get("runtime_trajectory"), dict)
            else {}
        )
        usage = (
            dict(result.usage)
            if result is not None
            else {
                key: trace.get(key)
                for key in ("input_tokens", "output_tokens", "total_tokens")
                if isinstance(trace.get(key), int | float)
            }
        )
        output = (
            result.output
            if result is not None and result.output
            else trace.get("output_preview") or ""
        )
        spans = detail.get("spans") if isinstance(detail.get("spans"), list) else []
        observation = {
            "status": trace.get("status"),
            "output_preview": output,
            "span_kinds": [
                str(span.get("span_kind"))
                for span in spans
                if isinstance(span, dict) and span.get("span_kind")
            ],
            "spans": spans,
            "total_latency_ms": trace.get("total_latency_ms"),
            "total_tokens": (
                usage.get("total_tokens")
                if "total_tokens" in usage
                else (
                    int(usage.get("input_tokens") or usage.get("prompt_tokens") or 0)
                    + int(usage.get("output_tokens") or usage.get("completion_tokens") or 0)
                    if usage
                    else None
                )
            ),
            "total_cost_cents": _candidate_cost_cents(str(trace.get("model_id") or ""), usage),
            **runtime,
        }
        contract_case = {
            "case_id": run_case.get("case_id"),
            "input": run_case.get("input") or {},
            "expected_output": run_case.get("expected_output") or {},
            "expected_trajectory": run_case.get("expected_trajectory") or {},
            "assertions": run_case.get("assertions") or [],
            "metadata": run_case.get("metadata") or {},
        }
        return {
            "trace_id": trace_id,
            "detail": detail,
            "usage": usage,
            "fingerprint": (
                dict(result.fingerprint)
                if result is not None
                else _persisted_candidate_fingerprint(detail)
            ),
            "contract_result": evaluate_case(contract_case, observation),
        }

    return _run_candidate


def init_eval_outbox_worker(
    database: Any,
    *,
    enabled: bool = True,
    concurrency: int = 2,
    poll_interval_s: float = 2.0,
) -> EvalOutboxWorker | None:
    global _eval_outbox_worker
    if not enabled or not getattr(database, "enabled", False):
        _eval_outbox_worker = None
        return None
    repository = AgentTraceRepository(database)
    llm_settings = load_eval_llm_settings()
    llm_complete = build_eval_llm_complete(llm_settings)
    executor = EvaluatorExecutor(
        repository,
        llm_complete=llm_complete,
        kb_ragas_evaluate=_kb_ragas_evaluate,
        candidate_run=_build_candidate_runner(repository),
        created_by="eval-worker",
    )
    if llm_complete is not None:
        logger.info(
            "Eval LLM judge enabled model=%s assistant=%s",
            llm_settings.default_judge_model_id,
            llm_settings.assistant_base_url,
        )
    else:
        logger.warning("Eval LLM judge disabled; llm evaluators will require manual review")
    _eval_outbox_worker = EvalOutboxWorker(
        repository,
        executor,
        poll_interval_s=poll_interval_s,
    )
    return _eval_outbox_worker


def get_eval_outbox_worker() -> EvalOutboxWorker | None:
    return _eval_outbox_worker
