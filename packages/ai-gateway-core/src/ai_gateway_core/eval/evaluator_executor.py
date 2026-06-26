from __future__ import annotations

import json
import re
from collections.abc import Awaitable, Callable
from dataclasses import dataclass, field
from typing import Any

from ai_gateway_core.logging import get_logger
from ai_gateway_core.persistence.repositories.agent_trace_repository import AgentTraceRepository

logger = get_logger(__name__)

LlmCompleteFn = Callable[[str, str], Awaitable[str]]


@dataclass
class EvaluatorRunResult:
    run_id: str | None
    status: str
    score_summary: dict[str, Any] = field(default_factory=dict)
    metrics: dict[str, Any] = field(default_factory=dict)
    error_message: str | None = None
    scores_written: int = 0


def _resolve_trace_family(job_payload: dict[str, Any]) -> str:
    snapshot = job_payload.get("target_snapshot") or {}
    if isinstance(snapshot, dict):
        family = str(snapshot.get("trace_family") or "").strip()
        if family in {"assistant", "langgraph_proxy", "rag"}:
            return family
    family = str(job_payload.get("trace_family") or "").strip()
    if family in {"assistant", "langgraph_proxy", "rag"}:
        return family
    return "assistant"


def _trace_target(detail: dict[str, Any] | None) -> dict[str, Any] | None:
    if not detail:
        return None
    trace = detail.get("trace") or {}
    return {
        "trace_id": trace.get("trace_id"),
        "input_preview": trace.get("input_preview") or "",
        "output_preview": trace.get("output_preview") or "",
        "status": trace.get("status"),
        "total_latency_ms": int(trace.get("total_latency_ms") or 0),
        "model_id": trace.get("model_id"),
        "metadata": trace.get("metadata") or {},
    }


def _apply_rule(rule: dict[str, Any], target: dict[str, Any]) -> tuple[bool, str]:
    rule_type = str(rule.get("type") or "").strip().lower()
    if rule_type == "status_eq":
        expected = str(rule.get("value") or "")
        actual = str(target.get("status") or "")
        ok = actual == expected
        return ok, f"status {actual!r} {'==' if ok else '!='} {expected!r}"
    if rule_type == "latency_ms_lt":
        limit = int(rule.get("value") or 0)
        actual = int(target.get("total_latency_ms") or 0)
        ok = actual < limit
        return ok, f"latency {actual}ms {'<' if ok else '>='} {limit}ms"
    if rule_type == "output_contains":
        needle = str(rule.get("value") or "")
        haystack = str(target.get("output_preview") or "")
        ok = needle.lower() in haystack.lower() if needle else True
        return ok, f"output {'contains' if ok else 'missing'} {needle!r}"
    if rule_type == "output_not_empty":
        haystack = str(target.get("output_preview") or "").strip()
        ok = bool(haystack)
        return ok, "output preview is non-empty" if ok else "output preview is empty"
    return True, f"unknown rule type {rule_type!r} skipped"


def _score_with_rules(
    evaluator: dict[str, Any],
    target: dict[str, Any],
) -> dict[str, Any]:
    filter_config = evaluator.get("filter_config") or {}
    rules = filter_config.get("rules")
    if not isinstance(rules, list) or not rules:
        rules = [{"type": "output_not_empty"}]
    passed: list[str] = []
    failed: list[str] = []
    for rule in rules:
        if not isinstance(rule, dict):
            continue
        ok, message = _apply_rule(rule, target)
        (passed if ok else failed).append(message)
    score = 1.0 if not failed else max(0.0, 1.0 - (len(failed) / max(len(rules), 1)))
    return {
        "score_name": evaluator.get("name") or "quality",
        "score_type": "numeric",
        "numeric_value": round(score, 4),
        "label": "pass" if score >= float(filter_config.get("pass_threshold", 0.8)) else "fail",
        "explanation": "; ".join(passed + failed)[:2000],
        "scorer_type": "rule",
        "score_source": "rule",
        "evaluator_id": evaluator.get("evaluator_id"),
        "evaluator_name": evaluator.get("name"),
        "evaluator_version": evaluator.get("version"),
        "confidence": 1.0,
        "target_type": "trace",
        "target_id": target.get("trace_id"),
    }


def _parse_llm_score_response(text: str) -> dict[str, Any] | None:
    cleaned = text.strip()
    if not cleaned:
        return None
    try:
        payload = json.loads(cleaned)
        if isinstance(payload, dict):
            return payload
    except json.JSONDecodeError:
        pass
    match = re.search(r"\{[^{}]*\}", cleaned, flags=re.DOTALL)
    if not match:
        return None
    try:
        payload = json.loads(match.group(0))
    except json.JSONDecodeError:
        return None
    return payload if isinstance(payload, dict) else None


def _heuristic_llm_score(evaluator: dict[str, Any], target: dict[str, Any]) -> dict[str, Any]:
    output = str(target.get("output_preview") or "")
    score = 0.2
    if output.strip():
        score += 0.4
    if len(output.strip()) >= 40:
        score += 0.2
    if str(target.get("status") or "") == "succeeded":
        score += 0.2
    score = min(1.0, score)
    return {
        "score_name": evaluator.get("name") or "quality",
        "score_type": "numeric",
        "numeric_value": round(score, 4),
        "label": "pass" if score >= 0.7 else "review",
        "explanation": "Heuristic LLM-judge fallback used because no judge model response was available.",
        "scorer_type": "llm",
        "score_source": "llm",
        "evaluator_id": evaluator.get("evaluator_id"),
        "evaluator_name": evaluator.get("name"),
        "evaluator_version": evaluator.get("version"),
        "confidence": 0.35,
        "target_type": "trace",
        "target_id": target.get("trace_id"),
    }


class EvaluatorExecutor:
    """Execute queued evaluator runs against traces or dataset examples."""

    def __init__(
        self,
        repository: AgentTraceRepository,
        *,
        llm_complete: LlmCompleteFn | None = None,
        created_by: str = "eval-worker",
    ) -> None:
        self.repository = repository
        self.llm_complete = llm_complete
        self.created_by = created_by

    async def run_job(
        self,
        *,
        tenant_id: str,
        job_payload: dict[str, Any],
    ) -> EvaluatorRunResult:
        run_id = str(job_payload.get("run_id") or "")
        evaluator_id = str(job_payload.get("evaluator_id") or "")
        if not run_id or not evaluator_id:
            return EvaluatorRunResult(
                run_id=run_id or None,
                status="failed",
                error_message="Missing run_id or evaluator_id in outbox payload",
            )

        await self.repository.update_experiment_run(
            tenant_id=tenant_id,
            run_id=run_id,
            status="running",
            mark_started=True,
        )

        evaluator = await self.repository.get_evaluator(
            tenant_id=tenant_id,
            evaluator_id=evaluator_id,
        )
        if not evaluator:
            await self.repository.update_experiment_run(
                tenant_id=tenant_id,
                run_id=run_id,
                status="failed",
                error_message="Evaluator not found",
                mark_finished=True,
            )
            return EvaluatorRunResult(
                run_id=run_id,
                status="failed",
                error_message="Evaluator not found",
            )

        trace_family = _resolve_trace_family(job_payload)
        evaluator_type = str(evaluator.get("evaluator_type") or "human")
        if evaluator_type == "human":
            summary = {"pending_human": True, "message": "Human evaluator runs require manual scoring."}
            await self.repository.update_experiment_run(
                tenant_id=tenant_id,
                run_id=run_id,
                status="succeeded",
                score_summary=summary,
                metrics={"targets": 0},
                mark_finished=True,
            )
            return EvaluatorRunResult(run_id=run_id, status="succeeded", score_summary=summary)

        targets = await self._resolve_targets(
            tenant_id=tenant_id,
            job_payload=job_payload,
            trace_family=trace_family,
        )
        if not targets:
            await self.repository.update_experiment_run(
                tenant_id=tenant_id,
                run_id=run_id,
                status="failed",
                error_message="No evaluation targets resolved",
                mark_finished=True,
            )
            return EvaluatorRunResult(
                run_id=run_id,
                status="failed",
                error_message="No evaluation targets resolved",
            )

        scores: list[float] = []
        written = 0
        for target in targets:
            score_payload = await self._score_target(evaluator, target)
            trace_id = str(target.get("trace_id") or "")
            if not trace_id:
                continue
            created = await self.repository.create_eval_score(
                tenant_id=tenant_id,
                trace_id=trace_id,
                created_by=self.created_by,
                payload=score_payload,
                trace_family=trace_family,
            )
            if created:
                written += 1
                numeric = created.get("numeric_value")
                if isinstance(numeric, int | float):
                    scores.append(float(numeric))

        avg_score = sum(scores) / len(scores) if scores else 0.0
        summary = {
            "average_score": round(avg_score, 4),
            "scored_count": written,
            "target_count": len(targets),
            "evaluator_type": evaluator_type,
            "evaluator_name": evaluator.get("name"),
        }
        metrics = {
            "targets": len(targets),
            "scores_written": written,
            "pass_count": sum(1 for value in scores if value >= 0.8),
        }
        await self.repository.update_experiment_run(
            tenant_id=tenant_id,
            run_id=run_id,
            status="succeeded",
            score_summary=summary,
            metrics=metrics,
            mark_finished=True,
        )
        return EvaluatorRunResult(
            run_id=run_id,
            status="succeeded",
            score_summary=summary,
            metrics=metrics,
            scores_written=written,
        )

    async def _resolve_targets(
        self,
        *,
        tenant_id: str,
        job_payload: dict[str, Any],
        trace_family: str = "assistant",
    ) -> list[dict[str, Any]]:
        trace_id = job_payload.get("trace_id")
        if trace_id:
            detail = await self.repository.get_trace_detail(
                tenant_id=tenant_id,
                trace_id=str(trace_id),
                trace_family=trace_family,
            )
            target = _trace_target(detail)
            return [target] if target else []

        dataset_id = job_payload.get("dataset_id")
        if not dataset_id:
            return []

        examples, _total = await self.repository.list_examples(
            tenant_id=tenant_id,
            dataset_id=str(dataset_id),
            limit=200,
            offset=0,
        )
        targets: list[dict[str, Any]] = []
        for example in examples:
            source_trace_id = example.get("source_trace_id")
            if not source_trace_id:
                continue
            detail = await self.repository.get_trace_detail(
                tenant_id=tenant_id,
                trace_id=str(source_trace_id),
                trace_family=trace_family,
            )
            target = _trace_target(detail)
            if not target:
                continue
            target["expected_output"] = example.get("expected_output") or {}
            target["example_id"] = example.get("example_id")
            if target.get("example_id"):
                target["target_type"] = "example"
                target["target_id"] = target["example_id"]
            targets.append(target)
        return targets

    async def _score_target(
        self,
        evaluator: dict[str, Any],
        target: dict[str, Any],
    ) -> dict[str, Any]:
        evaluator_type = str(evaluator.get("evaluator_type") or "rule")
        if evaluator_type == "rule":
            payload = _score_with_rules(evaluator, target)
        else:
            payload = await self._score_with_llm(evaluator, target)
        if target.get("target_type"):
            payload["target_type"] = target["target_type"]
        if target.get("target_id"):
            payload["target_id"] = target["target_id"]
        return payload

    async def _score_with_llm(
        self,
        evaluator: dict[str, Any],
        target: dict[str, Any],
    ) -> dict[str, Any]:
        metadata = evaluator.get("metadata") or {}
        judge_model_id = str(metadata.get("judge_model_id") or "gpt-4o-mini")
        rubric = str(evaluator.get("rubric") or "Score helpfulness from 0 to 1.")
        prompt = (
            "You are an evaluator. Return JSON only with keys: "
            "numeric_value (0-1), label, explanation, confidence (0-1).\n"
            f"Rubric:\n{rubric}\n\n"
            f"Input preview:\n{target.get('input_preview') or ''}\n\n"
            f"Output preview:\n{target.get('output_preview') or ''}\n"
        )
        if self.llm_complete is not None:
            try:
                response = await self.llm_complete(judge_model_id, prompt)
                parsed = _parse_llm_score_response(response)
                if parsed:
                    numeric = float(parsed.get("numeric_value", 0))
                    return {
                        "score_name": evaluator.get("name") or "quality",
                        "score_type": "numeric",
                        "numeric_value": max(0.0, min(1.0, numeric)),
                        "label": str(parsed.get("label") or ("pass" if numeric >= 0.7 else "review")),
                        "explanation": str(parsed.get("explanation") or "")[:2000],
                        "scorer_type": "llm",
                        "score_source": "llm",
                        "evaluator_id": evaluator.get("evaluator_id"),
                        "evaluator_name": evaluator.get("name"),
                        "evaluator_version": evaluator.get("version"),
                        "confidence": float(parsed.get("confidence") or 0.6),
                        "target_type": "trace",
                        "target_id": target.get("trace_id"),
                        "metadata": {"judge_model_id": judge_model_id},
                    }
            except Exception as exc:  # noqa: BLE001 - evaluator must degrade gracefully
                logger.warning("LLM judge failed, using heuristic fallback: %s", exc)
        return _heuristic_llm_score(evaluator, target)
