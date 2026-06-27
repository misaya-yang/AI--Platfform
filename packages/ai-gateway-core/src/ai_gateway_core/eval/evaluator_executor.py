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
        "spans": detail.get("spans") or [],
        "events": detail.get("events") or [],
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
    if rule_type == "expected_output_contains":
        expected = target.get("expected_output") if isinstance(target.get("expected_output"), dict) else {}
        needle = str(rule.get("value") or expected.get("contains") or expected.get("output_preview") or "")
        haystack = str(target.get("output_preview") or "")
        ok = needle.lower() in haystack.lower() if needle else True
        return ok, f"expected output {'matched' if ok else 'missing'} {needle!r}"
    if rule_type == "output_matches_expected":
        expected = target.get("expected_output") if isinstance(target.get("expected_output"), dict) else {}
        needle = str(expected.get("output_preview") or expected.get("contains") or "")
        haystack = str(target.get("output_preview") or "")
        ok = needle.lower() in haystack.lower() if needle else True
        return ok, f"output {'matches' if ok else 'does not match'} expected preview"
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


def _span_kinds(target: dict[str, Any]) -> list[str]:
    spans = target.get("spans") if isinstance(target.get("spans"), list) else []
    kinds: list[str] = []
    for span in spans:
        if isinstance(span, dict) and span.get("span_kind"):
            kinds.append(str(span["span_kind"]))
    return kinds


def _event_types(target: dict[str, Any]) -> list[str]:
    events = target.get("events") if isinstance(target.get("events"), list) else []
    types: list[str] = []
    for event in events:
        if isinstance(event, dict) and event.get("event_type"):
            types.append(str(event["event_type"]))
    return types


def _score_with_trajectory(
    evaluator: dict[str, Any],
    target: dict[str, Any],
) -> dict[str, Any]:
    filter_config = evaluator.get("filter_config") or {}
    expected = target.get("expected_trajectory") if isinstance(target.get("expected_trajectory"), dict) else {}
    config = {**expected, **filter_config}
    required_spans = [str(item) for item in config.get("required_span_kinds", []) if isinstance(item, str)]
    forbidden_spans = [str(item) for item in config.get("forbidden_span_kinds", []) if isinstance(item, str)]
    required_events = [str(item) for item in config.get("required_events", []) if isinstance(item, str)]
    actual_spans = _span_kinds(target)
    actual_events = _event_types(target)
    failed: list[str] = []
    missing_spans = [span for span in required_spans if span not in actual_spans]
    forbidden_present = [span for span in forbidden_spans if span in actual_spans]
    missing_events = [event for event in required_events if event not in actual_events]
    if missing_spans:
        failed.append(f"missing span kinds: {', '.join(missing_spans)}")
    if forbidden_present:
        failed.append(f"forbidden span kinds present: {', '.join(forbidden_present)}")
    if missing_events:
        failed.append(f"missing events: {', '.join(missing_events)}")
    checks = max(len(required_spans) + len(forbidden_spans) + len(required_events), 1)
    score = max(0.0, 1.0 - (len(failed) / checks))
    return {
        "score_name": evaluator.get("name") or "trajectory",
        "score_type": "numeric",
        "numeric_value": round(score, 4),
        "label": "pass" if not failed else "fail",
        "explanation": "; ".join(failed) if failed else "trajectory matched expected spans/events",
        "scorer_type": "rule",
        "score_source": "rule",
        "evaluator_id": evaluator.get("evaluator_id"),
        "evaluator_name": evaluator.get("name"),
        "evaluator_version": evaluator.get("version"),
        "confidence": 1.0,
        "target_type": "trace",
        "target_id": target.get("trace_id"),
        "metadata": {
            "component": "trajectory",
            "actual_span_kinds": actual_spans,
            "actual_events": actual_events,
        },
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
    return {
        "score_name": evaluator.get("name") or "quality",
        "score_type": "numeric",
        "numeric_value": 0.0,
        "label": "review",
        "explanation": "LLM judge did not return a valid score; manual review is required.",
        "scorer_type": "llm",
        "score_source": "llm",
        "evaluator_id": evaluator.get("evaluator_id"),
        "evaluator_name": evaluator.get("name"),
        "evaluator_version": evaluator.get("version"),
        "confidence": 0.0,
        "target_type": "trace",
        "target_id": target.get("trace_id"),
    }


def _score_number(payload: dict[str, Any]) -> float:
    value = payload.get("numeric_value")
    if isinstance(value, int | float):
        return max(0.0, min(1.0, float(value)))
    return 0.0


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
            metadata = example.get("metadata") if isinstance(example.get("metadata"), dict) else {}
            target["expected_trajectory"] = metadata.get("expected_trajectory") or {}
            target["assertions"] = metadata.get("assertions") or []
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
        elif evaluator_type == "trajectory":
            payload = _score_with_trajectory(evaluator, target)
        elif evaluator_type == "composite":
            payload = await self._score_with_composite(evaluator, target)
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
        temperature = float(metadata.get("temperature") or 0)
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
                    if "numeric_value" not in parsed:
                        raise ValueError("LLM judge response missing numeric_value")
                    numeric = float(parsed.get("numeric_value", 0))
                    confidence = float(parsed.get("confidence", 0.6))
                    if not 0 <= numeric <= 1 or not 0 <= confidence <= 1:
                        raise ValueError("LLM judge score or confidence out of range")
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
                        "confidence": confidence,
                        "target_type": "trace",
                        "target_id": target.get("trace_id"),
                        "metadata": {"judge_model_id": judge_model_id, "temperature": temperature},
                    }
            except Exception as exc:  # noqa: BLE001 - evaluator must degrade gracefully
                logger.warning("LLM judge failed, marking review required: %s", exc)
        return _heuristic_llm_score(evaluator, target)

    async def _score_with_composite(
        self,
        evaluator: dict[str, Any],
        target: dict[str, Any],
    ) -> dict[str, Any]:
        filter_config = evaluator.get("filter_config") or {}
        components = filter_config.get("components")
        if not isinstance(components, list) or not components:
            components = [
                {"type": "rule", "weight": 0.5},
                {"type": "trajectory", "weight": 0.5},
            ]
        total_weight = 0.0
        weighted_score = 0.0
        hard_failed = False
        breakdown: list[dict[str, Any]] = []
        for index, component in enumerate(components, start=1):
            if not isinstance(component, dict):
                continue
            component_type = str(component.get("type") or "rule")
            weight = float(component.get("weight") or 1.0)
            component_evaluator = {
                **evaluator,
                "name": component.get("name") or f"{evaluator.get('name') or 'composite'}:{component_type}",
                "evaluator_type": component_type,
                "filter_config": component.get("config") or filter_config,
            }
            if component_type == "trajectory":
                score_payload = _score_with_trajectory(component_evaluator, target)
            elif component_type in {"llm", "llm_judge"}:
                score_payload = await self._score_with_llm(component_evaluator, target)
            else:
                score_payload = _score_with_rules(component_evaluator, target)
            score = _score_number(score_payload)
            threshold = float(component.get("threshold") or 0.8)
            if component.get("hard_blocker") and score < threshold:
                hard_failed = True
            total_weight += weight
            weighted_score += score * weight
            breakdown.append(
                {
                    "index": index,
                    "type": component_type,
                    "weight": weight,
                    "score": score,
                    "label": score_payload.get("label"),
                    "hard_blocker": bool(component.get("hard_blocker")),
                }
            )
        final_score = weighted_score / total_weight if total_weight else 0.0
        if hard_failed:
            final_score = min(final_score, 0.49)
        return {
            "score_name": evaluator.get("name") or "composite",
            "score_type": "numeric",
            "numeric_value": round(final_score, 4),
            "label": "pass" if final_score >= float(filter_config.get("pass_threshold", 0.8)) else "fail",
            "explanation": "Composite evaluator score from rule, trajectory, and judge components.",
            "scorer_type": "system",
            "score_source": "system",
            "evaluator_id": evaluator.get("evaluator_id"),
            "evaluator_name": evaluator.get("name"),
            "evaluator_version": evaluator.get("version"),
            "confidence": min(1.0, max((item["score"] for item in breakdown), default=0.0)),
            "target_type": "trace",
            "target_id": target.get("trace_id"),
            "metadata": {"components": breakdown, "hard_failed": hard_failed},
        }
