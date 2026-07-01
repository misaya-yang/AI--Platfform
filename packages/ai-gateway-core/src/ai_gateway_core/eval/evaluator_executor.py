from __future__ import annotations

import inspect
import json
import re
from collections.abc import Awaitable, Callable
from dataclasses import dataclass, field
from typing import Any

from ai_gateway_core.eval.kb_ragas_sample import kb_ragas_sample_from_target
from ai_gateway_core.logging import get_logger
from ai_gateway_core.persistence.repositories.agent_trace_repository import AgentTraceRepository

logger = get_logger(__name__)

LlmCompleteFn = Callable[..., Awaitable[str]]
KbRagasEvaluateFn = Callable[..., Awaitable[list[dict[str, Any]]]]


@dataclass(frozen=True)
class LlmCompleteContext:
    tenant_id: str
    trace_family: str = "assistant"
    trace_id: str | None = None


_DEFAULT_RAG_RUBRIC = (
    "Score RAG retrieval quality from 0 to 1. Penalize empty retrieval, failed spans, "
    "and answers that are not grounded in the bounded previews. Reward concise, faithful answers."
)


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
    metadata = trace.get("metadata") if isinstance(trace.get("metadata"), dict) else {}
    metrics = trace.get("metrics") if isinstance(trace.get("metrics"), dict) else {}
    return {
        "trace_id": trace.get("trace_id"),
        "trace_family": trace.get("trace_family"),
        "workflow_kind": trace.get("workflow_kind"),
        "input_preview": trace.get("input_preview") or "",
        "output_preview": trace.get("output_preview") or "",
        "status": trace.get("status"),
        "total_latency_ms": int(trace.get("total_latency_ms") or 0),
        "model_id": trace.get("model_id"),
        "metadata": metadata,
        "metrics": metrics,
        "spans": detail.get("spans") or [],
        "events": detail.get("events") or [],
    }


def _target_metrics(target: dict[str, Any]) -> dict[str, Any]:
    metrics = target.get("metrics") if isinstance(target.get("metrics"), dict) else {}
    metadata = target.get("metadata") if isinstance(target.get("metadata"), dict) else {}
    merged = dict(metrics)
    for key in ("retrieval.document_count", "retrieval_document_count"):
        if key in metadata and key not in merged:
            merged[key] = metadata[key]
    return merged


def _preview_excerpt(value: Any, *, limit: int = 120) -> str:
    text = str(value or "").strip()
    if len(text) <= limit:
        return text
    return f"{text[:limit]}...[truncated]"


def build_trajectory_summary(
    target: dict[str, Any],
    *,
    max_spans: int = 12,
    max_events: int = 8,
) -> str:
    spans = target.get("spans") if isinstance(target.get("spans"), list) else []
    events = target.get("events") if isinstance(target.get("events"), list) else []
    lines: list[str] = []
    for span in spans[:max_spans]:
        if not isinstance(span, dict):
            continue
        lines.append(
            "span "
            f"{span.get('span_kind') or 'unknown'}/"
            f"{span.get('name') or 'unnamed'} "
            f"status={span.get('status') or 'unknown'} "
            f"in={_preview_excerpt(span.get('input_preview'))!r} "
            f"out={_preview_excerpt(span.get('output_preview'))!r} "
            f"err={_preview_excerpt(span.get('error_message'), limit=80) or '-'}"
        )
    for event in events[:max_events]:
        if not isinstance(event, dict):
            continue
        lines.append(
            f"event {event.get('event_type') or 'unknown'} "
            f"seq={event.get('sequence_no')} "
            f"payload={_preview_excerpt(event.get('payload'), limit=80)}"
        )
    metrics = _target_metrics(target)
    if metrics:
        metric_bits = ", ".join(f"{key}={value}" for key, value in sorted(metrics.items())[:8])
        lines.append(f"metrics {metric_bits}")
    workflow = str(target.get("workflow_kind") or "").strip()
    if workflow:
        lines.append(f"workflow_kind={workflow}")
    return "\n".join(lines) if lines else "No trajectory steps recorded."


def _failed_span_count(target: dict[str, Any]) -> int:
    spans = target.get("spans") if isinstance(target.get("spans"), list) else []
    return sum(
        1
        for span in spans
        if isinstance(span, dict) and str(span.get("status") or "").lower() in {"failed", "error"}
    )


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
        needle = str(rule.get("value") or "").strip()
        if not needle:
            return False, "output_contains rule value is empty"
        haystack = str(target.get("output_preview") or "")
        ok = needle.lower() in haystack.lower()
        return ok, f"output {'contains' if ok else 'missing'} {needle!r}"
    if rule_type == "expected_output_contains":
        expected = target.get("expected_output") if isinstance(target.get("expected_output"), dict) else {}
        needle = str(rule.get("value") or expected.get("contains") or expected.get("output_preview") or "").strip()
        if not needle:
            return False, "expected_output_contains rule value is empty"
        haystack = str(target.get("output_preview") or "")
        ok = needle.lower() in haystack.lower()
        return ok, f"expected output {'matched' if ok else 'missing'} {needle!r}"
    if rule_type == "output_matches_expected":
        expected = target.get("expected_output") if isinstance(target.get("expected_output"), dict) else {}
        needle = str(expected.get("output_preview") or expected.get("contains") or "").strip()
        if not needle:
            return False, "output_matches_expected has no expected preview"
        haystack = str(target.get("output_preview") or "")
        ok = needle.lower() in haystack.lower()
        return ok, f"output {'matches' if ok else 'does not match'} expected preview"
    if rule_type == "output_not_empty":
        haystack = str(target.get("output_preview") or "").strip()
        ok = bool(haystack)
        return ok, "output preview is non-empty" if ok else "output preview is empty"
    if rule_type == "retrieval_document_count_gte":
        metrics = _target_metrics(target)
        limit = int(rule.get("value") or 1)
        actual = int(metrics.get("retrieval.document_count") or metrics.get("retrieval_document_count") or 0)
        ok = actual >= limit
        return ok, f"retrieval documents {actual} {'>=' if ok else '<'} {limit}"
    if rule_type == "no_error_spans":
        failed = _failed_span_count(target)
        ok = failed == 0
        return ok, "no failed spans" if ok else f"{failed} failed span(s) present"
    if rule_type == "required_span_kinds":
        required = [str(item) for item in rule.get("value") or [] if isinstance(item, str)]
        actual = set(_span_kinds(target))
        missing = [kind for kind in required if kind not in actual]
        ok = not missing
        return ok, "required span kinds present" if ok else f"missing span kinds: {', '.join(missing)}"
    return True, f"unknown rule type {rule_type!r} skipped"


def _rules_from_filter_config(filter_config: dict[str, Any]) -> dict[str, Any]:
    rules = filter_config.get("rules")
    if not isinstance(rules, list) or not rules:
        rules_config = filter_config.get("rules_config")
        if isinstance(rules_config, dict):
            nested_rules = rules_config.get("rules")
            if isinstance(nested_rules, list) and nested_rules:
                rules = nested_rules
    extracted: dict[str, Any] = {}
    if isinstance(rules, list) and rules:
        extracted["rules"] = rules
    if "pass_threshold" in filter_config:
        extracted["pass_threshold"] = filter_config["pass_threshold"]
    return extracted


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


def _extract_json_object(text: str) -> str | None:
    start = text.find("{")
    if start < 0:
        return None
    depth = 0
    in_string = False
    escaped = False
    for index in range(start, len(text)):
        char = text[index]
        if escaped:
            escaped = False
            continue
        if in_string:
            if char == "\\":
                escaped = True
            elif char == '"':
                in_string = False
            continue
        if char == '"':
            in_string = True
            continue
        if char == "{":
            depth += 1
        elif char == "}":
            depth -= 1
            if depth == 0:
                return text[start : index + 1]
    return None


def _parse_llm_score_response(text: str) -> dict[str, Any] | None:
    cleaned = text.strip()
    if not cleaned:
        return None
    candidates = [cleaned]
    embedded = _extract_json_object(cleaned)
    if embedded and embedded != cleaned:
        candidates.append(embedded)
    for candidate in candidates:
        try:
            payload = json.loads(candidate)
        except json.JSONDecodeError:
            continue
        if isinstance(payload, dict):
            return payload
    fenced = re.search(r"```(?:json)?\s*(\{.*\})\s*```", cleaned, flags=re.DOTALL)
    if fenced:
        try:
            payload = json.loads(fenced.group(1))
        except json.JSONDecodeError:
            return None
        return payload if isinstance(payload, dict) else None
    return None


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
        kb_ragas_evaluate: KbRagasEvaluateFn | None = None,
        created_by: str = "eval-worker",
    ) -> None:
        self.repository = repository
        self.llm_complete = llm_complete
        self.kb_ragas_evaluate = kb_ragas_evaluate
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
            llm_context = LlmCompleteContext(
                tenant_id=tenant_id,
                trace_family=trace_family,
                trace_id=str(target.get("trace_id") or "") or None,
            )
            score_payloads = await self._score_target_payloads(
                evaluator,
                target,
                llm_context=llm_context,
            )
            trace_id = str(target.get("trace_id") or "")
            if not trace_id:
                continue
            for score_payload in score_payloads:
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

    def _selected_spans(self, target: dict[str, Any], filter_config: dict[str, Any]) -> list[dict[str, Any]]:
        spans = target.get("spans") if isinstance(target.get("spans"), list) else []
        span_kinds = filter_config.get("span_kinds")
        allowed_kinds = (
            {str(item) for item in span_kinds if isinstance(item, str)}
            if isinstance(span_kinds, list) and span_kinds
            else {"tool_execution", "retriever", "model_invocation", "document_fetch", "gateway_proxy"}
        )
        selected: list[dict[str, Any]] = []
        for span in spans:
            if not isinstance(span, dict):
                continue
            kind = str(span.get("span_kind") or "")
            if kind in allowed_kinds:
                selected.append(span)
        return selected

    def _span_target(self, parent_target: dict[str, Any], span: dict[str, Any]) -> dict[str, Any]:
        span_id = str(span.get("span_id") or "")
        return {
            "trace_id": parent_target.get("trace_id"),
            "trace_family": parent_target.get("trace_family"),
            "workflow_kind": parent_target.get("workflow_kind"),
            "input_preview": span.get("input_preview") or parent_target.get("input_preview") or "",
            "output_preview": span.get("output_preview") or "",
            "status": span.get("status") or parent_target.get("status"),
            "total_latency_ms": int(span.get("duration_ms") or parent_target.get("total_latency_ms") or 0),
            "model_id": parent_target.get("model_id"),
            "metadata": {
                **(parent_target.get("metadata") if isinstance(parent_target.get("metadata"), dict) else {}),
                "span_kind": span.get("span_kind"),
                "span_name": span.get("name"),
            },
            "metrics": parent_target.get("metrics") if isinstance(parent_target.get("metrics"), dict) else {},
            "spans": [span],
            "events": parent_target.get("events") if isinstance(parent_target.get("events"), list) else [],
            "span_id": span_id,
            "target_type": "span",
            "target_id": span_id,
        }

    async def _score_span_targets(
        self,
        evaluator: dict[str, Any],
        target: dict[str, Any],
        *,
        llm_context: LlmCompleteContext | None = None,
    ) -> list[dict[str, Any]]:
        filter_config = evaluator.get("filter_config") if isinstance(evaluator.get("filter_config"), dict) else {}
        mode = str(filter_config.get("mode") or "rule").strip().lower()
        spans = self._selected_spans(target, filter_config)
        if not spans:
            return [
                {
                    "score_name": evaluator.get("name") or "span-quality",
                    "score_type": "numeric",
                    "numeric_value": 0.0,
                    "label": "fail",
                    "explanation": "No matching spans found for span evaluator.",
                    "scorer_type": "rule",
                    "score_source": "rule",
                    "evaluator_id": evaluator.get("evaluator_id"),
                    "evaluator_name": evaluator.get("name"),
                    "evaluator_version": evaluator.get("version"),
                    "confidence": 1.0,
                    "target_type": "trace",
                    "target_id": target.get("trace_id"),
                }
            ]

        payloads: list[dict[str, Any]] = []
        for span in spans:
            span_target = self._span_target(target, span)
            component_evaluator = {
                **evaluator,
                "name": f"{evaluator.get('name') or 'span'}:{span.get('span_kind')}",
                "filter_config": _rules_from_filter_config(filter_config),
            }
            if mode in {"llm", "llm_judge"}:
                payload = await self._score_with_llm(component_evaluator, span_target, llm_context=llm_context)
            else:
                payload = _score_with_rules(component_evaluator, span_target)
            payload["span_id"] = span_target.get("span_id")
            payload["target_type"] = "span"
            payload["target_id"] = span_target.get("span_id")
            payload["score_name"] = str(payload.get("score_name") or component_evaluator["name"])
            payloads.append(payload)
        return payloads

    async def _score_target_payloads(
        self,
        evaluator: dict[str, Any],
        target: dict[str, Any],
        *,
        llm_context: LlmCompleteContext | None = None,
    ) -> list[dict[str, Any]]:
        evaluator_type = str(evaluator.get("evaluator_type") or "rule")
        if evaluator_type == "span":
            return await self._score_span_targets(evaluator, target, llm_context=llm_context)
        if evaluator_type == "ragas":
            return await self._score_with_kb_ragas(evaluator, target)
        payload = await self._build_score_payload(evaluator, target, llm_context=llm_context)
        return [payload]

    async def _build_score_payload(
        self,
        evaluator: dict[str, Any],
        target: dict[str, Any],
        *,
        llm_context: LlmCompleteContext | None = None,
    ) -> dict[str, Any]:
        evaluator_type = str(evaluator.get("evaluator_type") or "rule")
        if evaluator_type == "rule":
            payload = _score_with_rules(evaluator, target)
        elif evaluator_type == "trajectory":
            payload = _score_with_trajectory(evaluator, target)
        elif evaluator_type == "composite":
            payload = await self._score_with_composite(evaluator, target, llm_context=llm_context)
        else:
            payload = await self._score_with_llm(evaluator, target, llm_context=llm_context)
        if target.get("target_type"):
            payload["target_type"] = target["target_type"]
        if target.get("target_id"):
            payload["target_id"] = target["target_id"]
        if target.get("span_id"):
            payload["span_id"] = target.get("span_id")
        return payload

    async def _invoke_llm_complete(
        self,
        model_id: str,
        prompt: str,
        context: LlmCompleteContext,
    ) -> str:
        if self.llm_complete is None:
            raise RuntimeError("llm_complete is not configured")
        try:
            signature = inspect.signature(self.llm_complete)
        except (TypeError, ValueError):
            return await self.llm_complete(model_id, prompt, context)
        accepts_context = any(
            parameter.kind is inspect.Parameter.VAR_POSITIONAL
            for parameter in signature.parameters.values()
        ) or len(signature.parameters) >= 3
        if accepts_context:
            return await self.llm_complete(model_id, prompt, context)
        return await self.llm_complete(model_id, prompt)

    def _resolve_llm_rubric(
        self,
        evaluator: dict[str, Any],
        target: dict[str, Any],
        *,
        trace_family: str,
    ) -> str:
        rubric = str(evaluator.get("rubric") or "").strip()
        if rubric:
            return rubric
        family = str(target.get("trace_family") or trace_family or "").strip()
        if family == "rag":
            return _DEFAULT_RAG_RUBRIC
        return "Score helpfulness, grounding, and safety from 0 to 1 using bounded previews."

    def _resolve_ground_truth(self, evaluator: dict[str, Any], target: dict[str, Any]) -> str | None:
        filter_config = evaluator.get("filter_config") if isinstance(evaluator.get("filter_config"), dict) else {}
        if filter_config.get("ground_truth"):
            return str(filter_config["ground_truth"]).strip() or None
        expected = target.get("expected_output") if isinstance(target.get("expected_output"), dict) else {}
        for key in ("output_preview", "contains", "answer"):
            value = expected.get(key)
            if value:
                return str(value).strip() or None
        return None

    def _heuristic_kb_ragas_score(
        self,
        evaluator: dict[str, Any],
        target: dict[str, Any],
        *,
        explanation: str,
        metric: str = "context_relevancy",
    ) -> dict[str, Any]:
        return {
            "score_name": metric,
            "score_type": "numeric",
            "numeric_value": 0.0,
            "label": "review",
            "explanation": explanation[:2000],
            "scorer_type": "llm",
            "score_source": "kb_ragas",
            "evaluator_id": evaluator.get("evaluator_id"),
            "evaluator_name": evaluator.get("name"),
            "evaluator_version": evaluator.get("version"),
            "confidence": 0.0,
            "target_type": "trace",
            "target_id": target.get("trace_id"),
            "metadata": {"metric": metric, "component": "kb_ragas"},
        }

    async def _score_with_kb_ragas(
        self,
        evaluator: dict[str, Any],
        target: dict[str, Any],
    ) -> list[dict[str, Any]]:
        filter_config = evaluator.get("filter_config") if isinstance(evaluator.get("filter_config"), dict) else {}
        metadata = evaluator.get("metadata") if isinstance(evaluator.get("metadata"), dict) else {}
        ground_truth = self._resolve_ground_truth(evaluator, target)
        sample = kb_ragas_sample_from_target(target, ground_truth=ground_truth)
        if not sample:
            return [
                self._heuristic_kb_ragas_score(
                    evaluator,
                    target,
                    explanation="KB RAGAS sample could not be built from rag trace retrieval spans.",
                )
            ]

        required_span_kinds = filter_config.get("required_span_kinds")
        if isinstance(required_span_kinds, list) and required_span_kinds:
            actual = set(_span_kinds(target))
            missing = [kind for kind in required_span_kinds if str(kind) not in actual]
            if missing:
                return [
                    self._heuristic_kb_ragas_score(
                        evaluator,
                        target,
                        explanation=f"Missing required span kinds for KB RAGAS: {', '.join(missing)}",
                    )
                ]

        metrics = filter_config.get("metrics") or metadata.get("metrics") or ["context_relevancy"]
        if not isinstance(metrics, list):
            metrics = ["context_relevancy"]
        llm_config = filter_config.get("llm_config") or metadata.get("llm_config")
        if llm_config is not None and not isinstance(llm_config, dict):
            llm_config = None

        if self.kb_ragas_evaluate is None:
            return [
                self._heuristic_kb_ragas_score(
                    evaluator,
                    target,
                    explanation="KB RAGAS client is not configured; manual review is required.",
                )
            ]

        try:
            raw_results = await self.kb_ragas_evaluate(
                query=sample.question,
                contexts=sample.contexts,
                metrics=[str(item) for item in metrics if isinstance(item, str)],
                ground_truth=sample.ground_truth,
                llm_config=llm_config,
            )
        except Exception as exc:  # noqa: BLE001 - evaluator must degrade gracefully
            logger.warning("KB RAGAS evaluation failed: %s", exc)
            return [
                self._heuristic_kb_ragas_score(
                    evaluator,
                    target,
                    explanation=f"KB RAGAS evaluation failed: {exc}",
                )
            ]

        if not raw_results:
            return [
                self._heuristic_kb_ragas_score(
                    evaluator,
                    target,
                    explanation="KB RAGAS evaluation returned no metric results.",
                )
            ]

        pass_threshold = float(filter_config.get("pass_threshold", 0.7))
        payloads: list[dict[str, Any]] = []
        for item in raw_results:
            if not isinstance(item, dict):
                continue
            metric = str(item.get("metric") or "context_relevancy")
            score = max(0.0, min(1.0, float(item.get("score") or 0.0)))
            service_label = str(item.get("label") or "")
            if service_label == "review":
                label = "review"
            else:
                label = "pass" if score >= pass_threshold else "fail"
            payloads.append(
                {
                    "score_name": metric,
                    "score_type": "numeric",
                    "numeric_value": round(score, 4),
                    "label": label,
                    "explanation": str(item.get("explanation") or "")[:2000],
                    "scorer_type": "llm",
                    "score_source": "kb_ragas",
                    "evaluator_id": evaluator.get("evaluator_id"),
                    "evaluator_name": evaluator.get("name"),
                    "evaluator_version": evaluator.get("version"),
                    "confidence": 0.7 if label != "review" else 0.0,
                    "target_type": target.get("target_type") or "trace",
                    "target_id": target.get("target_id") or target.get("trace_id"),
                    "metadata": {
                        "metric": metric,
                        "component": "kb_ragas",
                        "judge_model": item.get("judge_model"),
                        "dataset_id": sample.dataset_id,
                    },
                }
            )
        return payloads or [
            self._heuristic_kb_ragas_score(
                evaluator,
                target,
                explanation="KB RAGAS evaluation returned invalid metric payloads.",
            )
        ]

    async def _score_with_llm(
        self,
        evaluator: dict[str, Any],
        target: dict[str, Any],
        *,
        llm_context: LlmCompleteContext | None = None,
    ) -> dict[str, Any]:
        metadata = evaluator.get("metadata") or {}
        judge_model_id = str(metadata.get("judge_model_id") or "qwen3.7-plus")
        context = llm_context or LlmCompleteContext(tenant_id="default")
        rubric = self._resolve_llm_rubric(
            evaluator,
            target,
            trace_family=context.trace_family,
        )
        temperature = float(metadata.get("temperature") or 0)
        trajectory = build_trajectory_summary(target)
        prompt = (
            "You are an evaluator. Return JSON only with keys: "
            "numeric_value (0-1), label, explanation, confidence (0-1).\n"
            f"Trace family: {context.trace_family}\n"
            f"Rubric:\n{rubric}\n\n"
            f"Input preview:\n{target.get('input_preview') or ''}\n\n"
            f"Output preview:\n{target.get('output_preview') or ''}\n\n"
            f"Trajectory summary:\n{trajectory}\n"
        )
        if self.llm_complete is not None:
            try:
                response = await self._invoke_llm_complete(judge_model_id, prompt, context)
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
        *,
        llm_context: LlmCompleteContext | None = None,
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
                score_payload = await self._score_with_llm(
                    component_evaluator,
                    target,
                    llm_context=llm_context,
                )
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
