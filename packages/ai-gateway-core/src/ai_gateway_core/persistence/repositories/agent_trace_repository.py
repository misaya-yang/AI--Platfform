from __future__ import annotations

import hashlib
import json
import math
import random
import statistics
import uuid
from datetime import datetime, timezone
from typing import Any

from .base import BaseRepository

EXAMPLE_METADATA_PATCH_KEYS = (
    "expected_trajectory",
    "assertions",
    "tags",
    "difficulty",
    "owner",
    "review_status",
)
EVAL_GATE_METRICS_SCHEMA_VERSION = "eval-gate-metrics/v2"
EVAL_GATE_METRICS_REQUIRED_FIELDS = frozenset(
    {
        "case_count",
        "critical_case_count",
        "critical_failed_count",
        "critical_pass_rate",
        "failed_case_count",
        "overall_score",
        "pass_rate",
        "score_sum",
        "stateful_case_count",
        "stateful_failed_count",
        "stateful_pass_rate",
        "trajectory_case_count",
        "trajectory_failed_count",
        "trajectory_pass_rate",
    }
)
EVAL_GATE_RATE_ABS_TOLERANCE = 0.00005


def _canonical_hash(value: Any) -> str:
    payload = json.dumps(value, ensure_ascii=False, sort_keys=True, separators=(",", ":"))
    return hashlib.sha256(payload.encode("utf-8")).hexdigest()


def _percentile(values: list[float], percentile: float) -> float | None:
    if not values:
        return None
    ordered = sorted(values)
    position = (len(ordered) - 1) * percentile
    lower = math.floor(position)
    upper = math.ceil(position)
    if lower == upper:
        return ordered[lower]
    return ordered[lower] + (ordered[upper] - ordered[lower]) * (position - lower)


def _paired_bootstrap_ci(deltas: list[float], *, samples: int = 10_000) -> list[float] | None:
    if not deltas:
        return None
    rng = random.Random(42)
    count = len(deltas)
    means = [
        sum(deltas[rng.randrange(count)] for _ in range(count)) / count for _ in range(samples)
    ]
    low = _percentile(means, 0.025)
    high = _percentile(means, 0.975)
    if low is None or high is None:
        return None
    return [round(low, 4), round(high, 4)]


def _known_number(value: Any) -> float | None:
    if isinstance(value, bool) or not isinstance(value, int | float):
        return None
    number = float(value)
    return number if math.isfinite(number) else None


def _has_versioned_gate_metrics(value: Any) -> bool:
    if (
        not isinstance(value, dict)
        or value.get("schema_version") != EVAL_GATE_METRICS_SCHEMA_VERSION
        or not EVAL_GATE_METRICS_REQUIRED_FIELDS.issubset(value)
    ):
        return False
    count_fields = (
        "case_count",
        "failed_case_count",
        "trajectory_case_count",
        "trajectory_failed_count",
        "critical_case_count",
        "critical_failed_count",
        "stateful_case_count",
        "stateful_failed_count",
    )
    counts: dict[str, int] = {}
    for field in count_fields:
        raw = value.get(field)
        if isinstance(raw, bool) or not isinstance(raw, int) or raw < 0:
            return False
        counts[field] = raw
    case_count = counts["case_count"]
    if (
        case_count <= 0
        or counts["failed_case_count"] > case_count
        or counts["trajectory_case_count"] != case_count
        or counts["trajectory_failed_count"] > counts["trajectory_case_count"]
        or counts["critical_case_count"] > case_count
        or counts["critical_failed_count"] > counts["critical_case_count"]
        or counts["stateful_case_count"] > case_count
        or counts["stateful_failed_count"] > counts["stateful_case_count"]
    ):
        return False

    score_sum = _known_number(value.get("score_sum"))
    overall_score = _known_number(value.get("overall_score"))
    if (
        score_sum is None
        or overall_score is None
        or not 0.0 <= score_sum <= float(case_count)
        or not 0.0 <= overall_score <= 1.0
        or not math.isclose(
            overall_score,
            score_sum / case_count,
            rel_tol=0.0,
            abs_tol=EVAL_GATE_RATE_ABS_TOLERANCE,
        )
    ):
        return False

    def rate_matches(rate_field: str, count_field: str, failed_field: str) -> bool:
        count = counts[count_field]
        failed = counts[failed_field]
        raw_rate = value.get(rate_field)
        if count == 0:
            return raw_rate is None
        rate = _known_number(raw_rate)
        return (
            rate is not None
            and 0.0 <= rate <= 1.0
            and math.isclose(
                rate,
                (count - failed) / count,
                rel_tol=0.0,
                abs_tol=EVAL_GATE_RATE_ABS_TOLERANCE,
            )
        )

    return all(
        (
            rate_matches("pass_rate", "case_count", "failed_case_count"),
            rate_matches(
                "trajectory_pass_rate",
                "trajectory_case_count",
                "trajectory_failed_count",
            ),
            rate_matches(
                "critical_pass_rate",
                "critical_case_count",
                "critical_failed_count",
            ),
            rate_matches(
                "stateful_pass_rate",
                "stateful_case_count",
                "stateful_failed_count",
            ),
        )
    )


def _average(values: list[float]) -> float | None:
    return round(sum(values) / len(values), 4) if values else None


def _average_complete_metric(rows: list[dict[str, Any]], key: str) -> float | None:
    values = [_known_number(row.get(key)) for row in rows]
    if not values or any(value is None for value in values):
        return None
    return _average([value for value in values if value is not None])


def _aggregate_live_case_rows(rows: list[dict[str, Any]]) -> dict[str, dict[str, Any]]:
    grouped: dict[str, list[dict[str, Any]]] = {}
    for row in rows:
        grouped.setdefault(str(row.get("case_id") or ""), []).append(row)

    aggregated: dict[str, dict[str, Any]] = {}
    for case_id, trials in grouped.items():
        observed = [
            row.get("observed_metrics") if isinstance(row.get("observed_metrics"), dict) else {}
            for row in trials
        ]
        scores = [
            value
            for item in observed
            if (value := _known_number(item.get("aggregate_score"))) is not None
        ]
        behavior_labels = [
            item.get("behavior_pass")
            for item in observed
            if isinstance(item.get("behavior_pass"), bool)
        ]
        execution_labels = [item.get("execution_succeeded") is True for item in observed]
        representative = next(
            (item for item in observed if item.get("trace_id")),
            observed[0] if observed else {},
        )
        representative_row = next(
            (
                row
                for row in trials
                if str(row.get("candidate_trace_id") or "")
                == str(representative.get("trace_id") or "")
            ),
            trials[0],
        )
        status = "unscored"
        if observed and (not all(execution_labels) or not all(behavior_labels)):
            status = "failed"
        elif observed and behavior_labels and all(behavior_labels):
            status = "passed"
        aggregated[case_id] = {
            "case_id": case_id,
            "example_id": trials[0].get("example_id"),
            "input": trials[0].get("input") or {},
            "expected_output": trials[0].get("expected_output") or {},
            "assertions": trials[0].get("assertions") or [],
            "metadata": trials[0].get("metadata") or {},
            "candidate_trace_id": representative.get("trace_id"),
            "trace_ids": [str(item.get("trace_id")) for item in observed if item.get("trace_id")],
            "status": status,
            "behavior_pass": bool(behavior_labels) and all(behavior_labels),
            "execution_succeeded": bool(execution_labels) and all(execution_labels),
            "critical": bool((trials[0].get("metadata") or {}).get("critical")),
            "aggregate_score": _average(scores),
            "score_stddev": (
                round(statistics.pstdev(scores), 4) if len(scores) > 1 else 0.0 if scores else None
            ),
            "flaky": len(set(behavior_labels)) > 1,
            "trial_count": len(trials),
            "observed_metrics": {
                key: _average_complete_metric(observed, key)
                for key in (
                    "latency_ms",
                    "input_tokens",
                    "output_tokens",
                    "total_tokens",
                    "cost_cents",
                )
            },
            "output_preview": representative.get("output_preview") or "",
            "trace": {
                "trace_family": representative_row.get("trace_family") or "assistant",
                "status": representative_row.get("trace_status"),
                "model_id": representative_row.get("model_id"),
                "provider": representative_row.get("provider"),
                "total_latency_ms": representative.get("latency_ms"),
                "total_tokens": representative.get("total_tokens"),
                "output_preview": representative.get("output_preview") or "",
            },
            "tool_trajectory": representative.get("tool_trajectory") or [],
            "rag_evidence": representative.get("rag_evidence") or [],
            "exit_reason": representative.get("exit_reason"),
            "contract_failures": sorted(
                {
                    str(failure)
                    for item in observed
                    for failure in item.get("contract_failures") or []
                }
            ),
            "errors": sorted({str(item.get("error")) for item in observed if item.get("error")}),
        }
    return aggregated


def _example_metadata_patch(payload: dict[str, Any]) -> dict[str, Any]:
    patch = dict(payload.get("metadata") or {})
    for key in EXAMPLE_METADATA_PATCH_KEYS:
        if payload.get(key) is not None:
            patch[key] = payload[key]
    return patch


def _import_example_metadata(example: dict[str, Any]) -> dict[str, Any]:
    return {
        "case_id": example.get("case_id"),
        "expected_trajectory": example.get("expected_trajectory") or {},
        "assertions": example.get("assertions") or [],
        **(example.get("metadata") or {}),
    }


def _experiment_score_id(
    *,
    tenant_id: str,
    trace_id: str,
    trace_family: str,
    payload: dict[str, Any],
) -> str | None:
    metadata = payload.get("metadata")
    if not isinstance(metadata, dict):
        return None
    experiment_run_id = str(metadata.get("experiment_run_id") or "").strip()
    if not experiment_run_id:
        return None

    span_id = payload.get("span_id")
    target_type = payload.get("target_type") or ("span" if span_id else "trace")
    target_id = payload.get("target_id") or span_id or trace_id
    identity = {
        "evaluator_id": str(payload.get("evaluator_id") or ""),
        "evaluator_name": str(payload.get("evaluator_name") or ""),
        "evaluator_version": str(payload.get("evaluator_version") or ""),
        "experiment_run_id": experiment_run_id,
        "score_name": str(payload.get("score_name") or ""),
        "score_source": str(payload.get("score_source") or payload.get("scorer_type") or "human"),
        "scorer_type": str(payload.get("scorer_type") or "human"),
        "span_id": str(span_id or ""),
        "target_id": str(target_id),
        "target_type": str(target_type),
        "tenant_id": tenant_id,
        "trace_family": trace_family,
        "trace_id": trace_id,
    }
    identity_json = json.dumps(identity, sort_keys=True, separators=(",", ":"))
    return str(uuid.uuid5(uuid.NAMESPACE_URL, f"ai-gateway/eval-score/{identity_json}"))


def _coerce_timestamptz(value: Any) -> datetime | None:
    if value is None or value == "":
        return None
    if isinstance(value, datetime):
        return value if value.tzinfo is not None else value.replace(tzinfo=timezone.utc)
    if isinstance(value, bool):
        return None
    if isinstance(value, int | float):
        if value <= 0:
            return None
        return datetime.fromtimestamp(value, tz=timezone.utc)
    if isinstance(value, str):
        text = value.strip()
        if not text:
            return None
        if text.endswith("Z"):
            text = f"{text[:-1]}+00:00"
        try:
            parsed = datetime.fromisoformat(text)
        except ValueError:
            return None
        return parsed if parsed.tzinfo is not None else parsed.replace(tzinfo=timezone.utc)
    return None


class AgentTraceRepository(BaseRepository):
    """Tenant-scoped persistence helper for Agent Trace Eval APIs."""

    async def list_traces(
        self,
        *,
        tenant_id: str,
        user_id: str | None = None,
        trace_family: str = "assistant",
        status: str | None = None,
        model_id: str | None = None,
        session_id: str | None = None,
        run_id: str | None = None,
        request_id: str | None = None,
        agent_id: str | None = None,
        agent_version_id: str | None = None,
        publication_id: str | None = None,
        channel: str | None = None,
        transcript_query: str | None = None,
        turn_index: int | None = None,
        span_kind: str | None = None,
        score_name: str | None = None,
        score_label: str | None = None,
        min_score: float | None = None,
        max_score: float | None = None,
        min_latency_ms: int | None = None,
        max_latency_ms: int | None = None,
        dataset_id: str | None = None,
        metadata_dataset_id: str | None = None,
        started_after: Any | None = None,
        started_before: Any | None = None,
        limit: int = 50,
        offset: int = 0,
    ) -> tuple[list[dict[str, Any]], int]:
        params: list[Any] = [tenant_id, trace_family]
        filters = ["t.tenant_id = $1", "t.trace_family = $2"]

        if user_id:
            params.append(user_id)
            filters.append(f"t.user_id = ${len(params)}")
        if status:
            params.append(status)
            filters.append(f"t.status = ${len(params)}")
        if model_id:
            params.append(model_id)
            filters.append(f"t.model_id = ${len(params)}")
        if session_id:
            params.append(session_id)
            filters.append(f"t.session_id = ${len(params)}")
        if run_id:
            params.append(run_id)
            filters.append(f"t.run_id = ${len(params)}")
        if request_id:
            params.append(request_id)
            filters.append(f"t.request_id = ${len(params)}")
        if agent_id:
            params.append(agent_id)
            filters.append(f"t.agent_id = ${len(params)}::uuid")
        if agent_version_id:
            params.append(agent_version_id)
            filters.append(f"t.agent_version_id = ${len(params)}::uuid")
        if publication_id:
            params.append(publication_id)
            filters.append(f"t.publication_id = ${len(params)}::uuid")
        if channel:
            params.append(channel)
            filters.append(f"t.channel = ${len(params)}")
        if turn_index is not None:
            params.append(str(turn_index))
            filters.append(f"t.metadata->'transcript_locator'->>'turn_index' = ${len(params)}")
        if transcript_query:
            params.append(f"%{transcript_query}%")
            query_param = f"${len(params)}"
            filters.append(
                f"""
                (
                    t.trace_id::text ILIKE {query_param}
                    OR COALESCE(t.request_id, '') ILIKE {query_param}
                    OR COALESCE(t.run_id, '') ILIKE {query_param}
                    OR COALESCE(t.session_id, '') ILIKE {query_param}
                    OR COALESCE(t.user_id, '') ILIKE {query_param}
                    OR COALESCE(t.model_id, '') ILIKE {query_param}
                    OR COALESCE(t.provider, '') ILIKE {query_param}
                    OR COALESCE(t.input_preview, '') ILIKE {query_param}
                    OR COALESCE(t.output_preview, '') ILIKE {query_param}
                    OR COALESCE(
                        t.metadata->'transcript_locator'->>'current_message_preview',
                        ''
                    ) ILIKE {query_param}
                    OR COALESCE(
                        t.metadata->'transcript_locator'->>'transcript_excerpt',
                        ''
                    ) ILIKE {query_param}
                    OR COALESCE(
                        t.metadata->'transcript_locator'->>'turn_id',
                        ''
                    ) ILIKE {query_param}
                )
                """
            )
        if span_kind:
            params.append(span_kind)
            filters.append(
                f"""
                EXISTS (
                    SELECT 1
                    FROM agent_trace_spans st
                    WHERE st.trace_id = t.trace_id AND st.span_kind = ${len(params)}
                )
                """
            )
        if min_latency_ms is not None:
            params.append(min_latency_ms)
            filters.append(f"t.total_latency_ms >= ${len(params)}")
        if max_latency_ms is not None:
            params.append(max_latency_ms)
            filters.append(f"t.total_latency_ms <= ${len(params)}")
        if started_after is not None:
            params.append(started_after)
            filters.append(f"t.started_at >= ${len(params)}")
        if started_before is not None:
            params.append(started_before)
            filters.append(f"t.started_at <= ${len(params)}")
        if score_name:
            params.append(score_name)
            filters.append(
                f"""
                EXISTS (
                    SELECT 1
                    FROM agent_trace_scores ss
                    WHERE ss.trace_id = t.trace_id AND ss.score_name = ${len(params)}
                )
                """
            )
        if score_label:
            params.append(score_label)
            filters.append(
                f"""
                EXISTS (
                    SELECT 1
                    FROM agent_trace_scores sl
                    WHERE sl.trace_id = t.trace_id AND sl.label = ${len(params)}
                )
                """
            )
        if min_score is not None:
            params.append(min_score)
            filters.append(
                f"""
                EXISTS (
                    SELECT 1
                    FROM agent_trace_scores smn
                    WHERE smn.trace_id = t.trace_id AND smn.numeric_value >= ${len(params)}
                )
                """
            )
        if max_score is not None:
            params.append(max_score)
            filters.append(
                f"""
                EXISTS (
                    SELECT 1
                    FROM agent_trace_scores smx
                    WHERE smx.trace_id = t.trace_id AND smx.numeric_value <= ${len(params)}
                )
                """
            )
        if dataset_id:
            params.append(dataset_id)
            filters.append(
                f"""
                EXISTS (
                    SELECT 1
                    FROM eval_examples ex
                    WHERE ex.source_trace_id = t.trace_id AND ex.dataset_id = ${len(params)}::uuid
                )
                """
            )
        if metadata_dataset_id:
            params.append(metadata_dataset_id)
            filters.append(f"t.metadata->>'dataset_id' = ${len(params)}")

        where_clause = " AND ".join(filters)
        count_row = await self.fetchrow(
            f"SELECT COUNT(*) AS total FROM agent_traces t WHERE {where_clause}",
            *params,
        )
        total = int(count_row.get("total") or 0) if count_row else 0

        page_params = [*params, limit, offset]
        rows = await self.fetch(
            f"""
            SELECT
                t.*,
                COUNT(s.score_id)::int AS scores_count
            FROM agent_traces t
            LEFT JOIN agent_trace_scores s ON s.trace_id = t.trace_id
            WHERE {where_clause}
            GROUP BY t.trace_id
            ORDER BY t.created_at DESC
            LIMIT ${len(params) + 1}
            OFFSET ${len(params) + 2}
            """,
            *page_params,
        )
        return [self._decode_trace_row(row) for row in rows], total

    async def get_trace_detail(
        self,
        *,
        tenant_id: str,
        trace_id: str,
        user_id: str | None = None,
        trace_family: str = "assistant",
    ) -> dict[str, Any] | None:
        params: list[Any] = [trace_id, tenant_id, trace_family]
        filters = ["trace_id = $1", "tenant_id = $2", "trace_family = $3"]
        if user_id:
            params.append(user_id)
            filters.append(f"user_id = ${len(params)}")

        trace = await self.fetchrow(
            f"SELECT *, 0::int AS scores_count FROM agent_traces WHERE {' AND '.join(filters)}",
            *params,
        )
        if not trace:
            return None

        spans = await self.fetch(
            """
            SELECT *
            FROM agent_trace_spans
            WHERE trace_id = $1
            ORDER BY sequence_no ASC, started_at ASC
            """,
            trace_id,
        )
        events = await self.fetch(
            """
            SELECT *
            FROM agent_trace_events
            WHERE trace_id = $1
            ORDER BY sequence_no ASC, occurred_at ASC
            """,
            trace_id,
        )
        scores = await self.fetch(
            """
            SELECT *
            FROM agent_trace_scores
            WHERE trace_id = $1
            ORDER BY created_at DESC
            """,
            trace_id,
        )

        decoded_trace = self._decode_trace_row(trace)
        decoded_trace["scores_count"] = len(scores)
        return {
            "trace": decoded_trace,
            "spans": [self._decode_span_row(row) for row in spans],
            "events": [self._decode_event_row(row) for row in events],
            "scores": [self._decode_score_row(row) for row in scores],
        }

    async def get_trace_details(
        self,
        *,
        tenant_id: str,
        trace_ids: list[str],
        user_id: str | None = None,
        trace_family: str = "assistant",
    ) -> dict[str, dict[str, Any]]:
        requested_ids = list(dict.fromkeys(str(trace_id) for trace_id in trace_ids if trace_id))
        if not requested_ids:
            return {}

        params: list[Any] = [requested_ids, tenant_id, trace_family]
        filters = ["trace_id = ANY($1::uuid[])", "tenant_id = $2", "trace_family = $3"]
        if user_id:
            params.append(user_id)
            filters.append(f"user_id = ${len(params)}")

        traces = await self.fetch(
            f"""
            SELECT *, 0::int AS scores_count
            FROM agent_traces
            WHERE {" AND ".join(filters)}
            ORDER BY array_position($1::uuid[], trace_id)
            """,
            *params,
        )
        allowed_ids = [str(row["trace_id"]) for row in traces]
        if not allowed_ids:
            return {}

        spans = await self.fetch(
            """
            SELECT * FROM agent_trace_spans
            WHERE trace_id = ANY($1::uuid[])
            ORDER BY trace_id, sequence_no ASC, started_at ASC
            """,
            allowed_ids,
        )
        events = await self.fetch(
            """
            SELECT * FROM agent_trace_events
            WHERE trace_id = ANY($1::uuid[])
            ORDER BY trace_id, sequence_no ASC, occurred_at ASC
            """,
            allowed_ids,
        )
        scores = await self.fetch(
            """
            SELECT * FROM agent_trace_scores
            WHERE trace_id = ANY($1::uuid[])
            ORDER BY trace_id, created_at DESC
            """,
            allowed_ids,
        )

        details = {
            trace_id: {
                "trace": self._decode_trace_row(row),
                "spans": [],
                "events": [],
                "scores": [],
            }
            for row in traces
            if (trace_id := str(row["trace_id"]))
        }
        for key, rows, decoder in (
            ("spans", spans, self._decode_span_row),
            ("events", events, self._decode_event_row),
            ("scores", scores, self._decode_score_row),
        ):
            for row in rows:
                trace_id = str(row["trace_id"])
                if trace_id in details:
                    details[trace_id][key].append(decoder(row))
        for detail in details.values():
            detail["trace"]["scores_count"] = len(detail["scores"])
        return details

    async def create_score(
        self,
        *,
        tenant_id: str,
        trace_id: str,
        created_by: str,
        payload: dict[str, Any],
        user_id: str | None = None,
        trace_family: str = "assistant",
    ) -> dict[str, Any] | None:
        params: list[Any] = [trace_id, tenant_id, trace_family]
        filters = ["trace_id = $1", "tenant_id = $2", "trace_family = $3"]
        if user_id:
            params.append(user_id)
            filters.append(f"user_id = ${len(params)}")

        trace = await self.fetchrow(
            f"SELECT trace_id FROM agent_traces WHERE {' AND '.join(filters)}",
            *params,
        )
        if not trace:
            return None

        score_id = _experiment_score_id(
            tenant_id=tenant_id,
            trace_id=trace_id,
            trace_family=trace_family,
            payload=payload,
        )
        row = await self.fetchrow(
            """
            INSERT INTO agent_trace_scores (
                score_id, trace_id, span_id, score_name, score_type, numeric_value,
                boolean_value, categorical_value, text_value, label, explanation,
                scorer_type, evaluator_version, target_type, target_id,
                evaluator_id, evaluator_name, score_source, confidence,
                created_by, metadata
            ) VALUES (
                COALESCE($1::uuid, gen_random_uuid()), $2, $3, $4, $5, $6,
                $7, $8, $9, $10, $11,
                $12, $13, $14, $15,
                $16::uuid, $17, $18, $19,
                $20, $21::jsonb
            )
            ON CONFLICT (score_id)
            DO UPDATE SET
                score_type = EXCLUDED.score_type,
                numeric_value = EXCLUDED.numeric_value,
                boolean_value = EXCLUDED.boolean_value,
                categorical_value = EXCLUDED.categorical_value,
                text_value = EXCLUDED.text_value,
                label = EXCLUDED.label,
                explanation = EXCLUDED.explanation,
                scorer_type = EXCLUDED.scorer_type,
                evaluator_version = EXCLUDED.evaluator_version,
                target_type = EXCLUDED.target_type,
                target_id = EXCLUDED.target_id,
                evaluator_id = EXCLUDED.evaluator_id,
                evaluator_name = EXCLUDED.evaluator_name,
                score_source = EXCLUDED.score_source,
                confidence = EXCLUDED.confidence,
                created_by = EXCLUDED.created_by,
                metadata = EXCLUDED.metadata
            RETURNING *
            """,
            score_id,
            trace_id,
            payload.get("span_id"),
            payload["score_name"],
            payload.get("score_type") or "numeric",
            payload.get("numeric_value"),
            payload.get("boolean_value"),
            payload.get("categorical_value"),
            payload.get("text_value"),
            payload.get("label"),
            payload.get("explanation"),
            payload.get("scorer_type") or "human",
            payload.get("evaluator_version"),
            payload.get("target_type") or ("span" if payload.get("span_id") else "trace"),
            payload.get("target_id") or payload.get("span_id") or trace_id,
            payload.get("evaluator_id"),
            payload.get("evaluator_name"),
            payload.get("score_source") or payload.get("scorer_type") or "human",
            payload.get("confidence"),
            created_by,
            self._json_dumps(payload.get("metadata") or {}),
        )
        return self._decode_score_row(row) if row else None

    async def ingest_trace(
        self,
        *,
        tenant_id: str,
        created_by: str,
        payload: dict[str, Any],
        enqueue: bool = True,
    ) -> dict[str, Any]:
        trace = payload["trace"]
        trace_id = str(trace.get("trace_id") or uuid.uuid4())
        thread_id = trace.get("thread_id") or trace.get("session_id")
        metrics = trace.get("metrics") or {}
        privacy = trace.get("privacy") or {}
        metadata = {
            "schema_version": "ate-03",
            "source_adapter": trace.get("source_adapter") or "api",
            **(trace.get("metadata") or {}),
        }
        await self.fetchrow(
            """
            INSERT INTO agent_traces (
                trace_id, trace_family, workflow_kind, tenant_id, user_id,
                session_id, thread_id, run_id, request_id, otel_trace_id, traceparent,
                model_id, provider,
                status, started_at, ended_at, total_latency_ms, first_token_latency_ms,
                input_tokens, output_tokens, total_tokens, total_cost_cents,
                input_preview, output_preview, redaction_state, metadata,
                metrics, privacy, source_adapter, retention_expires_at
            ) VALUES (
                $1::uuid, $2, $3, $4, $5,
                $6, $7, $8, $9, $10, $11,
                $12, $13,
                $14, COALESCE($15::timestamptz, NOW()), $16::timestamptz, $17, $18,
                $19, $20, $21, $22,
                $23, $24, $25::jsonb, $26::jsonb,
                $27::jsonb, $28::jsonb, $29, $30::timestamptz
            )
            ON CONFLICT (trace_id)
            DO UPDATE SET
                trace_family = EXCLUDED.trace_family,
                workflow_kind = EXCLUDED.workflow_kind,
                tenant_id = EXCLUDED.tenant_id,
                user_id = EXCLUDED.user_id,
                session_id = EXCLUDED.session_id,
                thread_id = EXCLUDED.thread_id,
                run_id = EXCLUDED.run_id,
                request_id = EXCLUDED.request_id,
                model_id = EXCLUDED.model_id,
                provider = EXCLUDED.provider,
                status = EXCLUDED.status,
                ended_at = COALESCE(EXCLUDED.ended_at, agent_traces.ended_at),
                total_latency_ms = GREATEST(agent_traces.total_latency_ms, EXCLUDED.total_latency_ms),
                first_token_latency_ms = GREATEST(agent_traces.first_token_latency_ms, EXCLUDED.first_token_latency_ms),
                input_tokens = GREATEST(agent_traces.input_tokens, EXCLUDED.input_tokens),
                output_tokens = GREATEST(agent_traces.output_tokens, EXCLUDED.output_tokens),
                total_tokens = GREATEST(agent_traces.total_tokens, EXCLUDED.total_tokens),
                total_cost_cents = GREATEST(agent_traces.total_cost_cents, EXCLUDED.total_cost_cents),
                input_preview = COALESCE(NULLIF(EXCLUDED.input_preview, ''), agent_traces.input_preview),
                output_preview = COALESCE(NULLIF(EXCLUDED.output_preview, ''), agent_traces.output_preview),
                redaction_state = agent_traces.redaction_state || EXCLUDED.redaction_state,
                metadata = agent_traces.metadata || EXCLUDED.metadata,
                metrics = agent_traces.metrics || EXCLUDED.metrics,
                privacy = agent_traces.privacy || EXCLUDED.privacy,
                source_adapter = COALESCE(EXCLUDED.source_adapter, agent_traces.source_adapter),
                updated_at = NOW()
            RETURNING trace_id
            """,
            trace_id,
            trace.get("trace_family") or "assistant",
            trace.get("workflow_kind") or "ai_assistant_chat",
            tenant_id,
            trace.get("user_id") or created_by,
            trace.get("session_id"),
            thread_id,
            trace.get("run_id"),
            trace.get("request_id"),
            trace.get("otel_trace_id"),
            trace.get("traceparent"),
            trace.get("model_id"),
            trace.get("provider"),
            trace.get("status") or "succeeded",
            _coerce_timestamptz(trace.get("started_at")),
            _coerce_timestamptz(trace.get("ended_at")),
            int(metrics.get("total_latency_ms") or trace.get("total_latency_ms") or 0),
            int(metrics.get("first_token_latency_ms") or trace.get("first_token_latency_ms") or 0),
            int(metrics.get("input_tokens") or trace.get("input_tokens") or 0),
            int(metrics.get("output_tokens") or trace.get("output_tokens") or 0),
            int(metrics.get("total_tokens") or trace.get("total_tokens") or 0),
            int(metrics.get("total_cost_cents") or trace.get("total_cost_cents") or 0),
            trace.get("input_preview") or "",
            trace.get("output_preview") or "",
            self._json_dumps(trace.get("redaction_state") or {}),
            self._json_dumps(metadata),
            self._json_dumps(metrics),
            self._json_dumps(privacy),
            trace.get("source_adapter") or "api",
            _coerce_timestamptz(trace.get("retention_expires_at")),
        )
        for span in trace.get("spans") or []:
            await self._upsert_ingested_span(trace_id, span)
        for event in trace.get("events") or []:
            await self._upsert_ingested_event(trace_id, event)
        if enqueue:
            job = await self.create_trace_ingested_outbox_job(
                tenant_id=tenant_id,
                trace_id=trace_id,
                trace_family=str(trace.get("trace_family") or "assistant"),
                status=str(trace.get("status") or "succeeded"),
                source_adapter=str(trace.get("source_adapter") or "api"),
            )
            return {
                "trace_id": trace_id,
                "status": "stored",
                "job_id": job.get("job_id") if job else None,
            }
        return {"trace_id": trace_id, "status": "stored", "job_id": None}

    async def _upsert_ingested_span(self, trace_id: str, span: dict[str, Any]) -> None:
        span_id = str(
            span.get("span_id")
            or uuid.uuid5(uuid.UUID(trace_id), span.get("name") or str(uuid.uuid4()))
        )
        await self.fetchrow(
            """
            INSERT INTO agent_trace_spans (
                span_id, trace_id, parent_span_id, span_kind, name, status,
                sequence_no, started_at, ended_at, duration_ms, input_preview,
                output_preview, attributes, error_type, error_message
            ) VALUES (
                $1::uuid, $2::uuid, $3::uuid, $4, $5, $6,
                $7, COALESCE($8::timestamptz, NOW()), $9::timestamptz, $10, $11,
                $12, $13::jsonb, $14, $15
            )
            ON CONFLICT (span_id)
            DO UPDATE SET
                status = EXCLUDED.status,
                ended_at = COALESCE(EXCLUDED.ended_at, agent_trace_spans.ended_at),
                duration_ms = GREATEST(agent_trace_spans.duration_ms, EXCLUDED.duration_ms),
                output_preview = COALESCE(NULLIF(EXCLUDED.output_preview, ''), agent_trace_spans.output_preview),
                attributes = agent_trace_spans.attributes || EXCLUDED.attributes,
                error_type = COALESCE(EXCLUDED.error_type, agent_trace_spans.error_type),
                error_message = COALESCE(EXCLUDED.error_message, agent_trace_spans.error_message)
            RETURNING span_id
            """,
            span_id,
            trace_id,
            span.get("parent_span_id"),
            span.get("span_kind") or "custom",
            str(span.get("name") or "span")[:160],
            span.get("status") or "succeeded",
            int(span.get("sequence_no") or 0),
            _coerce_timestamptz(span.get("started_at")),
            _coerce_timestamptz(span.get("ended_at")),
            int(span.get("duration_ms") or 0),
            span.get("input_preview") or "",
            span.get("output_preview") or "",
            self._json_dumps(span.get("attributes") or {}),
            span.get("error_type"),
            span.get("error_message"),
        )

    async def _upsert_ingested_event(self, trace_id: str, event: dict[str, Any]) -> None:
        await self.fetchrow(
            """
            INSERT INTO agent_trace_events (
                trace_id, span_id, event_type, sequence_no, occurred_at,
                payload, payload_size_bytes, redacted
            ) VALUES (
                $1::uuid, $2::uuid, $3, $4, COALESCE($5::timestamptz, NOW()),
                $6::jsonb, $7, $8
            )
            ON CONFLICT (trace_id, sequence_no)
            DO UPDATE SET
                span_id = EXCLUDED.span_id,
                event_type = EXCLUDED.event_type,
                payload = EXCLUDED.payload,
                payload_size_bytes = EXCLUDED.payload_size_bytes,
                redacted = EXCLUDED.redacted
            RETURNING event_id
            """,
            trace_id,
            event.get("span_id"),
            event.get("event_type") or "event",
            int(event.get("sequence_no") or 0),
            _coerce_timestamptz(event.get("occurred_at")),
            self._json_dumps(event.get("payload") or {}),
            int(event.get("payload_size_bytes") or 0),
            bool(event.get("redacted", True)),
        )

    async def get_thread(
        self,
        *,
        tenant_id: str,
        thread_id: str,
        user_id: str | None = None,
        limit: int = 100,
    ) -> dict[str, Any]:
        params: list[Any] = [tenant_id, thread_id]
        filters = ["t.tenant_id = $1", "COALESCE(t.thread_id, t.session_id) = $2"]
        if user_id:
            params.append(user_id)
            filters.append(f"t.user_id = ${len(params)}")
        rows = await self.fetch(
            f"""
            SELECT t.*, COUNT(s.score_id)::int AS scores_count
            FROM agent_traces t
            LEFT JOIN agent_trace_scores s ON s.trace_id = t.trace_id
            WHERE {" AND ".join(filters)}
            GROUP BY t.trace_id
            ORDER BY t.started_at ASC, t.created_at ASC
            LIMIT ${len(params) + 1}
            """,
            *params,
            limit,
        )
        traces = [self._decode_trace_row(row) for row in rows]
        metrics = {
            "trace_count": len(traces),
            "total_latency_ms": sum(int(trace.get("total_latency_ms") or 0) for trace in traces),
            "total_tokens": sum(int(trace.get("total_tokens") or 0) for trace in traces),
            "failed_count": sum(1 for trace in traces if trace.get("status") == "failed"),
        }
        return {"thread_id": thread_id, "traces": traces, "total": len(traces), "metrics": metrics}

    async def create_dataset(
        self,
        *,
        tenant_id: str,
        created_by: str,
        payload: dict[str, Any],
    ) -> dict[str, Any]:
        row = await self.fetchrow(
            """
            INSERT INTO eval_datasets (
                tenant_id, name, description, version, schema, metadata, created_by
            ) VALUES ($1, $2, $3, $4, $5::jsonb, $6::jsonb, $7)
            RETURNING *
            """,
            tenant_id,
            payload["name"],
            payload.get("description") or "",
            payload.get("version") or "v1",
            self._json_dumps(payload.get("schema") or payload.get("json_schema") or {}),
            self._json_dumps(payload.get("metadata") or {}),
            created_by,
        )
        return self._decode_eval_row(row) if row else {}

    async def create_example_from_trace(
        self,
        *,
        tenant_id: str,
        dataset_id: str,
        created_by: str,
        payload: dict[str, Any],
        user_id: str | None = None,
        trace_family: str = "assistant",
    ) -> dict[str, Any] | None:
        detail = await self.get_trace_detail(
            tenant_id=tenant_id,
            trace_id=payload["source_trace_id"],
            user_id=user_id,
            trace_family=trace_family,
        )
        if not detail:
            return None
        trace = detail["trace"]
        input_preview = str(trace.get("input_preview") or "")
        input_payload = {
            "message": input_preview,
            "input_preview": input_preview,
            "thread_id": trace.get("thread_id") or trace.get("session_id"),
            "run_id": trace.get("run_id"),
            "request_id": trace.get("request_id"),
            "metadata": trace.get("metadata") or {},
        }
        expected_output = payload.get("expected_output") or {
            "output_preview": trace.get("output_preview") or "",
        }
        metadata = dict(payload.get("metadata") or {})
        trace_metadata = trace.get("metadata") if isinstance(trace.get("metadata"), dict) else {}
        runtime_trajectory = (
            trace_metadata.get("runtime_trajectory")
            if isinstance(trace_metadata.get("runtime_trajectory"), dict)
            else {}
        )
        spans = detail.get("spans") if isinstance(detail.get("spans"), list) else []
        span_kinds = sorted(
            {
                str(span.get("span_kind") or "")
                for span in spans
                if isinstance(span, dict) and span.get("span_kind")
            }
        )
        metadata.setdefault(
            "expected_trajectory",
            {
                "required_span_kinds": span_kinds,
                "runtime": {
                    "expected_exit_reason": runtime_trajectory.get("exit_reason")
                    or trace.get("status"),
                },
            },
        )
        metadata.setdefault("assertions", [{"type": "no_sensitive_output"}])
        metadata["behavior_confirmed"] = False
        row = await self.fetchrow(
            """
            INSERT INTO eval_examples (
                dataset_id, tenant_id, split, input, expected_output, metadata,
                source_trace_id, source_span_id, created_by
            ) VALUES (
                $1::uuid, $2, $3, $4::jsonb, $5::jsonb, $6::jsonb,
                $7::uuid, $8::uuid, $9
            )
            RETURNING *
            """,
            dataset_id,
            tenant_id,
            payload.get("split") or "regression",
            self._json_dumps(input_payload),
            self._json_dumps(expected_output),
            self._json_dumps(metadata),
            payload["source_trace_id"],
            payload.get("source_span_id"),
            created_by,
        )
        return self._decode_eval_row(row) if row else None

    async def create_example(
        self,
        *,
        tenant_id: str,
        dataset_id: str,
        created_by: str,
        payload: dict[str, Any],
    ) -> dict[str, Any] | None:
        row = await self.fetchrow(
            """
            INSERT INTO eval_examples (
                dataset_id, tenant_id, split, input, expected_output, metadata,
                source_trace_id, source_span_id, created_by
            ) VALUES (
                $1::uuid, $2, $3, $4::jsonb, $5::jsonb, $6::jsonb,
                $7::uuid, $8::uuid, $9
            )
            RETURNING *
            """,
            dataset_id,
            tenant_id,
            payload.get("split") or "regression",
            self._json_dumps(payload.get("input") or {}),
            self._json_dumps(payload.get("expected_output") or {}),
            self._json_dumps(payload.get("metadata") or {}),
            payload.get("source_trace_id"),
            payload.get("source_span_id"),
            created_by,
        )
        return self._decode_eval_row(row) if row else None

    async def update_example(
        self,
        *,
        tenant_id: str,
        dataset_id: str,
        example_id: str,
        payload: dict[str, Any],
    ) -> dict[str, Any] | None:
        metadata_patch = _example_metadata_patch(payload)
        row = await self.fetchrow(
            """
            UPDATE eval_examples
            SET split = COALESCE($4, split),
                input = COALESCE($5::jsonb, input),
                expected_output = COALESCE($6::jsonb, expected_output),
                metadata = COALESCE(metadata, '{}'::jsonb) || $7::jsonb
            WHERE tenant_id = $1
              AND dataset_id = $2::uuid
              AND example_id = $3::uuid
            RETURNING *
            """,
            tenant_id,
            dataset_id,
            example_id,
            payload.get("split"),
            self._json_dumps(payload["input"]) if payload.get("input") is not None else None,
            self._json_dumps(payload["expected_output"])
            if payload.get("expected_output") is not None
            else None,
            self._json_dumps(metadata_patch),
        )
        return self._decode_eval_row(row) if row else None

    async def list_dataset_example_case_ids(
        self,
        *,
        tenant_id: str,
        dataset_id: str,
    ) -> set[str]:
        rows = await self.fetch(
            """
            SELECT DISTINCT metadata->>'case_id' AS case_id
            FROM eval_examples
            WHERE tenant_id = $1
              AND dataset_id = $2::uuid
              AND COALESCE(metadata->>'case_id', '') <> ''
            """,
            tenant_id,
            dataset_id,
        )
        return {str(row["case_id"]) for row in rows if row.get("case_id")}

    async def import_examples(
        self,
        *,
        tenant_id: str,
        dataset_id: str,
        created_by: str,
        examples: list[dict[str, Any]],
        mode: str = "skip_duplicates",
    ) -> dict[str, Any]:
        existing_case_ids: set[str] = set()
        if mode == "skip_duplicates":
            existing_case_ids = await self.list_dataset_example_case_ids(
                tenant_id=tenant_id,
                dataset_id=dataset_id,
            )
        seen_in_request: set[str] = set()
        imported: list[dict[str, Any]] = []
        skipped = 0
        for example in examples:
            case_id = str(example.get("case_id") or "").strip()
            if (
                mode == "skip_duplicates"
                and case_id
                and (case_id in existing_case_ids or case_id in seen_in_request)
            ):
                skipped += 1
                continue
            created = await self.create_example(
                tenant_id=tenant_id,
                dataset_id=dataset_id,
                created_by=created_by,
                payload={
                    "split": example.get("split") or "regression",
                    "input": example.get("input") or {},
                    "expected_output": example.get("expected_output") or {},
                    "metadata": _import_example_metadata(example),
                    "source_trace_id": example.get("source_trace_id"),
                    "source_span_id": example.get("source_span_id"),
                },
            )
            if created:
                imported.append(created)
                if case_id:
                    existing_case_ids.add(case_id)
                    seen_in_request.add(case_id)
        return {"imported": len(imported), "skipped": skipped, "examples": imported}

    async def create_evaluator(
        self,
        *,
        tenant_id: str,
        created_by: str,
        payload: dict[str, Any],
    ) -> dict[str, Any]:
        row = await self.fetchrow(
            """
            INSERT INTO eval_evaluators (
                tenant_id, name, evaluator_type, rubric, version,
                sampling_config, filter_config, metadata, created_by
            ) VALUES ($1, $2, $3, $4, $5, $6::jsonb, $7::jsonb, $8::jsonb, $9)
            RETURNING *
            """,
            tenant_id,
            payload["name"],
            payload.get("evaluator_type") or "human",
            payload.get("rubric") or "",
            payload.get("version") or "v1",
            self._json_dumps(payload.get("sampling_config") or {}),
            self._json_dumps(payload.get("filter_config") or {}),
            self._json_dumps(payload.get("metadata") or {}),
            created_by,
        )
        return self._decode_eval_row(row) if row else {}

    async def create_experiment(
        self,
        *,
        tenant_id: str,
        created_by: str,
        payload: dict[str, Any],
    ) -> dict[str, Any]:
        row = await self.fetchrow(
            """
            INSERT INTO eval_experiments (
                tenant_id, dataset_id, name, description, target_config, metadata, created_by
            ) VALUES ($1, $2::uuid, $3, $4, $5::jsonb, $6::jsonb, $7)
            RETURNING *
            """,
            tenant_id,
            payload.get("dataset_id"),
            payload["name"],
            payload.get("description") or "",
            self._json_dumps(payload.get("target_config") or {}),
            self._json_dumps(payload.get("metadata") or {}),
            created_by,
        )
        decoded = self._decode_eval_row(row) if row else {}
        decoded["runs"] = []
        return decoded

    async def get_experiment(
        self,
        *,
        tenant_id: str,
        experiment_id: str,
    ) -> dict[str, Any] | None:
        experiment = await self.fetchrow(
            "SELECT * FROM eval_experiments WHERE tenant_id = $1 AND experiment_id = $2::uuid",
            tenant_id,
            experiment_id,
        )
        if not experiment:
            return None
        runs = await self.fetch(
            """
            SELECT *
            FROM eval_experiment_runs
            WHERE tenant_id = $1 AND experiment_id = $2::uuid
            ORDER BY created_at DESC
            """,
            tenant_id,
            experiment_id,
        )
        decoded = self._decode_eval_row(experiment)
        decoded["runs"] = [self._decode_eval_row(run) for run in runs]
        return decoded

    async def has_active_evaluator_run_for_trace(
        self,
        *,
        tenant_id: str,
        evaluator_id: str,
        trace_id: str,
    ) -> bool:
        row = await self.fetchrow(
            """
            SELECT 1
            FROM eval_experiment_runs
            WHERE tenant_id = $1
              AND evaluator_id = $2::uuid
              AND status IN ('queued', 'running')
              AND target_snapshot->>'trace_id' = $3
            LIMIT 1
            """,
            tenant_id,
            evaluator_id,
            trace_id,
        )
        return row is not None

    async def count_pending_online_eval_runs(self, *, tenant_id: str) -> int:
        row = await self.fetchrow(
            """
            SELECT COUNT(*)::int AS pending
            FROM eval_experiment_runs
            WHERE tenant_id = $1
              AND status IN ('queued', 'running')
              AND target_snapshot->>'source' = 'online_sampling'
            """,
            tenant_id,
        )
        return int((row or {}).get("pending") or 0)

    async def has_pending_trace_ingested_job(
        self,
        *,
        tenant_id: str,
        trace_id: str,
    ) -> bool:
        row = await self.fetchrow(
            """
            SELECT 1
            FROM agent_trace_outbox
            WHERE tenant_id = $1
              AND job_type = 'trace.ingested'
              AND status IN ('queued', 'running')
              AND payload->>'trace_id' = $2
            LIMIT 1
            """,
            tenant_id,
            trace_id,
        )
        return row is not None

    async def create_trace_ingested_outbox_job(
        self,
        *,
        tenant_id: str,
        trace_id: str,
        trace_family: str,
        status: str,
        source_adapter: str,
    ) -> dict[str, Any] | None:
        payload = {
            "trace_id": trace_id,
            "trace_family": trace_family,
            "status": status,
            "source_adapter": source_adapter,
        }
        row = await self.fetchrow(
            """
            INSERT INTO agent_trace_outbox (tenant_id, job_type, payload)
            SELECT $1::varchar, 'trace.ingested'::varchar, $2::jsonb
            WHERE NOT EXISTS (
                SELECT 1
                FROM agent_trace_outbox
                WHERE tenant_id = $1::varchar
                  AND job_type = 'trace.ingested'
                  AND status IN ('queued', 'running')
                  AND payload->>'trace_id' = $3::text
            )
            RETURNING *
            """,
            tenant_id,
            self._json_dumps(payload),
            trace_id,
        )
        return self._decode_eval_row(row) if row else None

    async def enqueue_evaluator_run(
        self,
        *,
        tenant_id: str,
        evaluator_id: str,
        created_by: str,
        payload: dict[str, Any],
    ) -> dict[str, Any]:
        target_snapshot = dict(payload.get("target_snapshot") or {})
        trace_id = payload.get("trace_id")
        if trace_id and "trace_id" not in target_snapshot:
            target_snapshot["trace_id"] = trace_id
        metadata = payload.get("metadata")
        if isinstance(metadata, dict) and metadata:
            target_snapshot.setdefault("metadata", metadata)
        if not target_snapshot and trace_id:
            target_snapshot = {"trace_id": trace_id, "metadata": metadata or {}}
        run = await self.fetchrow(
            """
            INSERT INTO eval_experiment_runs (
                experiment_id, tenant_id, evaluator_id, dataset_id, status,
                target_snapshot, metrics, created_by
            ) VALUES (
                $1::uuid, $2, $3::uuid, $4::uuid, 'queued',
                $5::jsonb, $6::jsonb, $7
            )
            RETURNING *
            """,
            payload.get("experiment_id"),
            tenant_id,
            evaluator_id,
            payload.get("dataset_id"),
            self._json_dumps(target_snapshot),
            self._json_dumps({}),
            created_by,
        )
        decoded_run = self._decode_eval_row(run) if run else {}
        target_snapshot = payload.get("target_snapshot") or {}
        trace_family = "assistant"
        if isinstance(target_snapshot, dict):
            family = str(target_snapshot.get("trace_family") or "").strip()
            if family in {"assistant", "langgraph_proxy", "rag"}:
                trace_family = family
        job = await self.create_outbox_job(
            tenant_id=tenant_id,
            job_type="eval.evaluator.run",
            payload={
                "run_id": decoded_run.get("run_id"),
                "evaluator_id": evaluator_id,
                "experiment_id": payload.get("experiment_id"),
                "dataset_id": payload.get("dataset_id"),
                "trace_id": payload.get("trace_id"),
                "trace_family": trace_family,
                "target_snapshot": target_snapshot if isinstance(target_snapshot, dict) else {},
            },
        )
        return {"job_id": job["job_id"], "status": "queued", "run_id": decoded_run.get("run_id")}

    async def enqueue_live_experiment_run(
        self,
        *,
        tenant_id: str,
        experiment_id: str,
        dataset_id: str,
        evaluator_snapshots: list[dict[str, Any]],
        examples: list[dict[str, Any]],
        repetitions: int,
        created_by: str,
        target_snapshot: dict[str, Any],
        execution_config: dict[str, Any],
        candidate_fingerprint: dict[str, Any],
        baseline_run_id: str | None = None,
    ) -> dict[str, Any]:
        """Freeze one live candidate run and enqueue it atomically."""
        manifest: list[dict[str, Any]] = []
        seen_case_ids: set[str] = set()
        for example in examples:
            metadata = example.get("metadata") if isinstance(example.get("metadata"), dict) else {}
            case_id = str(metadata.get("case_id") or example.get("example_id") or "").strip()
            if not case_id or case_id in seen_case_ids:
                raise ValueError(
                    f"Dataset contains missing or duplicate case_id: {case_id or '<empty>'}"
                )
            seen_case_ids.add(case_id)
            manifest.append(
                {
                    "case_id": case_id,
                    "example_id": str(example.get("example_id") or "") or None,
                    "input": example.get("input") or {},
                    "expected_output": example.get("expected_output") or {},
                    "expected_trajectory": metadata.get("expected_trajectory") or {},
                    "assertions": metadata.get("assertions") or [],
                    "metadata": {
                        key: value
                        for key, value in metadata.items()
                        if key not in {"expected_trajectory", "assertions"}
                    },
                }
            )
        manifest.sort(key=lambda item: item["case_id"])
        evaluator_manifest = sorted(
            [
                {
                    key: evaluator.get(key)
                    for key in (
                        "evaluator_id",
                        "name",
                        "evaluator_type",
                        "rubric",
                        "version",
                        "sampling_config",
                        "filter_config",
                        "metadata",
                    )
                }
                for evaluator in evaluator_snapshots
            ],
            key=lambda item: str(item.get("evaluator_id") or ""),
        )
        dataset_manifest_hash = _canonical_hash(manifest)
        evaluator_suite_hash = _canonical_hash(evaluator_manifest)
        public_snapshot = {
            **target_snapshot,
            "run_mode": "live_candidate",
            "repetitions": repetitions,
            "evaluator_ids": [item.get("evaluator_id") for item in evaluator_manifest],
            "dataset_manifest_hash": dataset_manifest_hash,
            "evaluator_suite_hash": evaluator_suite_hash,
        }
        private_config = {
            **execution_config,
            "evaluators": evaluator_manifest,
        }

        async with self._pool.acquire() as conn, conn.transaction():
            run = await conn.fetchrow(
                """
                INSERT INTO eval_experiment_runs (
                    experiment_id, tenant_id, evaluator_id, dataset_id, status,
                    run_mode, repetitions, baseline_run_id,
                    dataset_manifest_hash, evaluator_suite_hash,
                    candidate_fingerprint, execution_config,
                    target_snapshot, metrics, created_by
                ) VALUES (
                    $1::uuid, $2, $3::uuid, $4::uuid, 'queued',
                    'live_candidate', $5, $6::uuid,
                    $7, $8, $9::jsonb, $10::jsonb,
                    $11::jsonb, '{}'::jsonb, $12
                )
                RETURNING *
                """,
                experiment_id,
                tenant_id,
                evaluator_manifest[0]["evaluator_id"],
                dataset_id,
                repetitions,
                baseline_run_id,
                dataset_manifest_hash,
                evaluator_suite_hash,
                self._json_dumps(candidate_fingerprint),
                self._json_dumps(private_config),
                self._json_dumps(public_snapshot),
                created_by,
            )
            run_id = str(run["run_id"])
            rows = [
                (
                    run_id,
                    tenant_id,
                    case["case_id"],
                    case.get("example_id"),
                    trial_index,
                    self._json_dumps(case["input"]),
                    self._json_dumps(case["expected_output"]),
                    self._json_dumps(case["expected_trajectory"]),
                    self._json_dumps(case["assertions"]),
                    self._json_dumps(case["metadata"]),
                )
                for case in manifest
                for trial_index in range(1, repetitions + 1)
            ]
            await conn.executemany(
                """
                INSERT INTO eval_experiment_run_cases (
                    run_id, tenant_id, case_id, example_id, trial_index,
                    input, expected_output, expected_trajectory, assertions, metadata
                ) VALUES (
                    $1::uuid, $2, $3, $4::uuid, $5,
                    $6::jsonb, $7::jsonb, $8::jsonb, $9::jsonb, $10::jsonb
                )
                """,
                rows,
            )
            job = await conn.fetchrow(
                """
                INSERT INTO agent_trace_outbox (tenant_id, job_type, payload)
                VALUES (
                    $1,
                    'eval.evaluator.run',
                    jsonb_build_object(
                        'run_id', $2::text,
                        'experiment_id', $3::text,
                        'dataset_id', $4::text,
                        'evaluator_id', $5::text,
                        'evaluator_ids', $6::jsonb,
                        'run_mode', 'live_candidate',
                        'trace_family', 'assistant'
                    )
                )
                RETURNING *
                """,
                tenant_id,
                run_id,
                experiment_id,
                dataset_id,
                evaluator_manifest[0]["evaluator_id"],
                self._json_dumps([item["evaluator_id"] for item in evaluator_manifest]),
            )
        return {"job_id": str(job["job_id"]), "status": "queued", "run_id": run_id}

    async def list_experiment_run_cases(
        self,
        *,
        tenant_id: str,
        run_id: str,
        statuses: tuple[str, ...] | None = None,
    ) -> list[dict[str, Any]]:
        params: list[Any] = [tenant_id, run_id]
        status_sql = ""
        if statuses:
            params.append(list(statuses))
            status_sql = f" AND status = ANY(${len(params)}::varchar[])"
        rows = await self.fetch(
            f"""
            SELECT *
            FROM eval_experiment_run_cases
            WHERE tenant_id = $1 AND run_id = $2::uuid{status_sql}
            ORDER BY case_id, trial_index
            """,
            *params,
        )
        return [self._decode_eval_row(row) for row in rows]

    async def update_experiment_run_case(
        self,
        *,
        tenant_id: str,
        run_case_id: str,
        status: str,
        candidate_trace_id: str | None = None,
        observed_metrics: dict[str, Any] | None = None,
        error_message: str | None = None,
    ) -> dict[str, Any] | None:
        row = await self.fetchrow(
            """
            UPDATE eval_experiment_run_cases
            SET status = $3,
                candidate_trace_id = COALESCE($4::uuid, candidate_trace_id),
                observed_metrics = COALESCE($5::jsonb, observed_metrics),
                error_message = $6,
                updated_at = NOW()
            WHERE tenant_id = $1 AND run_case_id = $2::uuid
            RETURNING *
            """,
            tenant_id,
            run_case_id,
            status,
            candidate_trace_id,
            self._json_dumps(observed_metrics) if observed_metrics is not None else None,
            error_message,
        )
        return self._decode_eval_row(row) if row else None

    async def create_outbox_job(
        self,
        *,
        tenant_id: str,
        job_type: str,
        payload: dict[str, Any],
    ) -> dict[str, Any]:
        row = await self.fetchrow(
            """
            INSERT INTO agent_trace_outbox (tenant_id, job_type, payload)
            VALUES ($1, $2, $3::jsonb)
            RETURNING *
            """,
            tenant_id,
            job_type,
            self._json_dumps(payload),
        )
        return self._decode_eval_row(row) if row else {}

    async def claim_outbox_jobs(
        self,
        *,
        limit: int = 8,
        max_attempts: int = 5,
    ) -> list[dict[str, Any]]:
        if not self.enabled:
            return []
        async with self._pool.acquire() as conn, conn.transaction():
            rows = await conn.fetch(
                """
                WITH picked AS (
                    SELECT job_id
                    FROM agent_trace_outbox
                    WHERE status = 'queued'
                      AND available_at <= NOW()
                      AND attempts < $2
                    ORDER BY available_at ASC, created_at ASC
                    LIMIT $1
                    FOR UPDATE SKIP LOCKED
                )
                UPDATE agent_trace_outbox o SET
                    status = 'running',
                    attempts = o.attempts + 1,
                    updated_at = NOW()
                FROM picked
                WHERE o.job_id = picked.job_id
                RETURNING o.*
                """,
                limit,
                max_attempts,
            )
        return [self._decode_eval_row(dict(row)) for row in rows]

    async def mark_outbox_succeeded(self, job_id: str) -> None:
        await self.execute(
            """
            UPDATE agent_trace_outbox
            SET status = 'succeeded', last_error = NULL, updated_at = NOW()
            WHERE job_id = $1::uuid
            """,
            job_id,
        )

    async def mark_outbox_failed(
        self,
        job_id: str,
        *,
        error: str,
        retry_after_seconds: int | None = None,
        max_attempts: int = 5,
    ) -> None:
        if retry_after_seconds is not None:
            await self.execute(
                """
                WITH updated_job AS (
                    UPDATE agent_trace_outbox
                    SET status = CASE
                            WHEN attempts >= $4 THEN 'failed'
                            ELSE 'queued'
                        END,
                        last_error = $2,
                        available_at = CASE
                            WHEN attempts >= $4 THEN available_at
                            ELSE NOW() + ($3::int * INTERVAL '1 second')
                        END,
                        updated_at = NOW()
                    WHERE job_id = $1::uuid
                    RETURNING tenant_id, job_type, payload, status
                )
                UPDATE eval_experiment_runs r
                SET status = 'failed',
                    error_message = $2,
                    finished_at = NOW(),
                    updated_at = NOW()
                FROM updated_job j
                WHERE j.status = 'failed'
                  AND j.job_type = 'eval.evaluator.run'
                  AND r.tenant_id = j.tenant_id
                  AND r.run_id = NULLIF(j.payload->>'run_id', '')::uuid
                """,
                job_id,
                error[:4000],
                retry_after_seconds,
                max_attempts,
            )
            return
        await self.execute(
            """
            WITH updated_job AS (
                UPDATE agent_trace_outbox
                SET status = 'failed', last_error = $2, updated_at = NOW()
                WHERE job_id = $1::uuid
                RETURNING tenant_id, job_type, payload
            )
            UPDATE eval_experiment_runs r
            SET status = 'failed',
                error_message = $2,
                finished_at = NOW(),
                updated_at = NOW()
            FROM updated_job j
            WHERE j.job_type = 'eval.evaluator.run'
              AND r.tenant_id = j.tenant_id
              AND r.run_id = NULLIF(j.payload->>'run_id', '')::uuid
            """,
            job_id,
            error[:4000],
        )

    async def update_experiment_run(
        self,
        *,
        tenant_id: str,
        run_id: str,
        status: str,
        score_summary: dict[str, Any] | None = None,
        metrics: dict[str, Any] | None = None,
        error_message: str | None = None,
        mark_started: bool = False,
        mark_finished: bool = False,
    ) -> dict[str, Any] | None:
        row = await self.fetchrow(
            """
            UPDATE eval_experiment_runs
            SET status = $3,
                score_summary = COALESCE($4::jsonb, score_summary),
                metrics = COALESCE($5::jsonb, metrics),
                error_message = $6,
                started_at = CASE WHEN $7 THEN COALESCE(started_at, NOW()) ELSE started_at END,
                finished_at = CASE WHEN $8 THEN NOW() ELSE finished_at END,
                updated_at = NOW()
            WHERE tenant_id = $1 AND run_id = $2::uuid
            RETURNING *
            """,
            tenant_id,
            run_id,
            status,
            self._json_dumps(score_summary) if score_summary is not None else None,
            self._json_dumps(metrics) if metrics is not None else None,
            error_message,
            mark_started,
            mark_finished,
        )
        return self._decode_eval_row(row) if row else None

    async def get_experiment_run(
        self,
        *,
        tenant_id: str,
        run_id: str,
    ) -> dict[str, Any] | None:
        row = await self.fetchrow(
            "SELECT * FROM eval_experiment_runs WHERE tenant_id = $1 AND run_id = $2::uuid",
            tenant_id,
            run_id,
        )
        return self._decode_eval_row(row) if row else None

    async def get_experiment_run_progress(
        self,
        *,
        tenant_id: str,
        run_id: str,
    ) -> dict[str, int]:
        row = await self.fetchrow(
            """
            SELECT
                COUNT(*)::int AS total_trials,
                COUNT(*) FILTER (
                    WHERE status IN ('succeeded', 'failed', 'skipped')
                )::int AS completed_trials,
                COUNT(*) FILTER (WHERE status = 'failed')::int AS failed_trials
            FROM eval_experiment_run_cases
            WHERE tenant_id = $1 AND run_id = $2::uuid
            """,
            tenant_id,
            run_id,
        )
        return {
            "total_trials": int((row or {}).get("total_trials") or 0),
            "completed_trials": int((row or {}).get("completed_trials") or 0),
            "failed_trials": int((row or {}).get("failed_trials") or 0),
        }

    async def promote_experiment_baseline(
        self,
        *,
        tenant_id: str,
        experiment_id: str,
        run_id: str,
        promoted_by: str,
        expected_previous_baseline_run_id: str | None = None,
    ) -> dict[str, Any] | None:
        async with self._pool.acquire() as conn, conn.transaction():
            experiment = await conn.fetchrow(
                """
                SELECT baseline_run_id
                FROM eval_experiments
                WHERE tenant_id = $1 AND experiment_id = $2::uuid
                FOR UPDATE
                """,
                tenant_id,
                experiment_id,
            )
            if not experiment:
                return None
            current_baseline = experiment.get("baseline_run_id")
            current_baseline_id = str(current_baseline) if current_baseline else None
            if current_baseline_id != expected_previous_baseline_run_id:
                return None
            eligible = await conn.fetchval(
                """
                SELECT EXISTS (
                    SELECT 1 FROM eval_experiment_runs
                    WHERE tenant_id = $1
                      AND experiment_id = $2::uuid
                      AND run_id = $3::uuid
                      AND run_mode = 'live_candidate'
                      AND status = 'succeeded'
                )
                """,
                tenant_id,
                experiment_id,
                run_id,
            )
            if not eligible:
                return None
            row = await conn.fetchrow(
                """
                UPDATE eval_experiments
                SET baseline_run_id = $3::uuid,
                    baseline_promoted_by = $4,
                    baseline_promoted_at = NOW(),
                    updated_at = NOW()
                WHERE tenant_id = $1 AND experiment_id = $2::uuid
                RETURNING experiment_id, baseline_run_id,
                          baseline_promoted_by, baseline_promoted_at
                """,
                tenant_id,
                experiment_id,
                run_id,
                promoted_by,
            )
            await conn.execute(
                """
                INSERT INTO eval_baseline_promotions (
                    tenant_id, experiment_id, previous_baseline_run_id,
                    baseline_run_id, promoted_by
                ) VALUES ($1, $2::uuid, $3::uuid, $4::uuid, $5)
                """,
                tenant_id,
                experiment_id,
                current_baseline,
                run_id,
                promoted_by,
            )
        result = self._decode_eval_row(row)
        previous = current_baseline
        result["previous_baseline_run_id"] = str(previous) if previous else None
        result["promoted_by"] = result.pop("baseline_promoted_by", promoted_by)
        result["promoted_at"] = result.pop("baseline_promoted_at", None)
        return result

    async def list_experiment_run_case_results(
        self,
        *,
        tenant_id: str,
        run_id: str,
        limit: int = 50,
        offset: int = 0,
    ) -> tuple[list[dict[str, Any]], int]:
        run = await self.get_experiment_run(tenant_id=tenant_id, run_id=run_id)
        if run and run.get("run_mode") == "live_candidate":
            return await self._list_live_experiment_run_case_results(
                tenant_id=tenant_id,
                run_id=run_id,
                limit=limit,
                offset=offset,
            )

        case_key_sql = """
            COALESCE(
                NULLIF(s.metadata->>'example_id', ''),
                NULLIF(s.metadata->>'case_id', ''),
                s.trace_id::text
            )
        """
        target_key_sql = """
            COALESCE(NULLIF(s.target_id, ''), s.span_id::text, s.trace_id::text)
        """
        count_row = await self.fetchrow(
            f"""
            SELECT COUNT(DISTINCT {case_key_sql})::int AS total
            FROM agent_trace_scores s
            INNER JOIN agent_traces t
                ON t.trace_id = s.trace_id AND t.tenant_id = $1
            WHERE s.metadata->>'experiment_run_id' = $2
            """,
            tenant_id,
            run_id,
        )
        total = int((count_row or {}).get("total") or 0)
        if total == 0:
            return [], 0

        rows = await self.fetch(
            f"""
            WITH ranked_scores AS (
                SELECT
                    s.*,
                    {case_key_sql} AS case_key,
                    ROW_NUMBER() OVER (
                        PARTITION BY {case_key_sql}, s.score_name,
                            s.target_type, {target_key_sql}
                        ORDER BY s.created_at DESC, s.score_id DESC
                    ) AS score_revision
                FROM agent_trace_scores s
                INNER JOIN agent_traces t
                    ON t.trace_id = s.trace_id AND t.tenant_id = $1
                WHERE s.metadata->>'experiment_run_id' = $2
            ),
            paged_cases AS (
                SELECT case_key, MAX(created_at) AS latest_created_at
                FROM ranked_scores
                WHERE score_revision = 1
                GROUP BY case_key
                ORDER BY latest_created_at DESC, case_key
                LIMIT $3 OFFSET $4
            )
            SELECT
                s.case_key,
                s.score_id,
                s.trace_id AS candidate_trace_id,
                s.score_name,
                s.target_type,
                s.target_id,
                s.span_id,
                s.numeric_value,
                s.label AS score_label,
                s.explanation AS score_explanation,
                s.score_source,
                s.metadata AS score_metadata,
                e.example_id,
                e.source_trace_id,
                e.input,
                e.expected_output,
                e.metadata AS example_metadata,
                t.trace_family,
                t.status AS trace_status,
                t.model_id,
                t.provider,
                t.total_latency_ms,
                t.total_tokens,
                t.output_preview
            FROM ranked_scores s
            INNER JOIN paged_cases p ON p.case_key = s.case_key
            INNER JOIN agent_traces t
                ON t.trace_id = s.trace_id AND t.tenant_id = $1
            LEFT JOIN eval_examples e
                ON e.tenant_id = $1
               AND e.example_id::text = NULLIF(s.metadata->>'example_id', '')
            WHERE s.score_revision = 1
            ORDER BY p.latest_created_at DESC, s.case_key, s.score_name
            """,
            tenant_id,
            run_id,
            max(1, limit),
            max(0, offset),
        )

        grouped: dict[str, dict[str, Any]] = {}
        for raw_row in rows:
            row = dict(raw_row)
            score_metadata = self._decode_json(row.get("score_metadata"), default={})
            example_metadata = self._decode_json(row.get("example_metadata"), default={})
            case_key = str(row.get("case_key") or row.get("candidate_trace_id") or "")
            case = grouped.setdefault(
                case_key,
                {
                    "example_id": str(
                        row.get("example_id") or score_metadata.get("example_id") or ""
                    )
                    or None,
                    "case_id": str(
                        score_metadata.get("case_id")
                        or example_metadata.get("case_id")
                        or row.get("example_id")
                        or case_key
                    ),
                    "candidate_trace_id": str(row.get("candidate_trace_id") or ""),
                    "source_trace_id": str(row.get("source_trace_id") or "") or None,
                    "status": "unscored",
                    "aggregate_score": None,
                    "failure_reason": None,
                    "input": self._decode_json(row.get("input"), default={}),
                    "expected_output": self._decode_json(row.get("expected_output"), default={}),
                    "trace": {
                        "trace_family": row.get("trace_family"),
                        "status": row.get("trace_status"),
                        "model_id": row.get("model_id"),
                        "provider": row.get("provider"),
                        "total_latency_ms": int(row.get("total_latency_ms") or 0),
                        "total_tokens": int(row.get("total_tokens") or 0),
                        "output_preview": row.get("output_preview") or "",
                    },
                    "scores": [],
                },
            )
            case["scores"].append(
                {
                    "score_name": row.get("score_name"),
                    "target_type": row.get("target_type"),
                    "target_id": row.get("target_id"),
                    "span_id": str(row.get("span_id") or "") or None,
                    "numeric_value": row.get("numeric_value"),
                    "label": row.get("score_label"),
                    "explanation": row.get("score_explanation") or "",
                    "score_source": row.get("score_source"),
                    "failure_kind": score_metadata.get("failure_kind"),
                }
            )

        cases = list(grouped.values())
        for case in cases:
            scores = case["scores"]
            numeric_scores = [
                float(score["numeric_value"])
                for score in scores
                if score.get("label") in {"pass", "fail"}
                and isinstance(score.get("numeric_value"), int | float)
                and not isinstance(score.get("numeric_value"), bool)
            ]
            case["aggregate_score"] = (
                round(sum(numeric_scores) / len(numeric_scores), 4) if numeric_scores else None
            )
            failed = next(
                (
                    score
                    for score in scores
                    if score.get("label") == "fail" or score.get("failure_kind") == "infrastructure"
                ),
                None,
            )
            review = next(
                (score for score in scores if score.get("label") == "review"),
                None,
            )
            if failed:
                case["status"] = "failed"
                case["failure_reason"] = failed.get("explanation") or "Evaluation failed"
            elif review:
                case["status"] = "review"
                case["failure_reason"] = review.get("explanation") or "Manual review required"
            elif any(score.get("label") == "pass" for score in scores):
                case["status"] = "passed"
        return cases, total

    async def _list_live_experiment_run_case_results(
        self,
        *,
        tenant_id: str,
        run_id: str,
        limit: int,
        offset: int,
    ) -> tuple[list[dict[str, Any]], int]:
        count_row = await self.fetchrow(
            """
            SELECT COUNT(DISTINCT case_id)::int AS total
            FROM eval_experiment_run_cases
            WHERE tenant_id = $1 AND run_id = $2::uuid
            """,
            tenant_id,
            run_id,
        )
        total = int((count_row or {}).get("total") or 0)
        if total == 0:
            return [], 0
        rows = await self.fetch(
            """
            WITH paged_cases AS (
                SELECT case_id, MIN(created_at) AS first_created_at
                FROM eval_experiment_run_cases
                WHERE tenant_id = $1 AND run_id = $2::uuid
                GROUP BY case_id
                ORDER BY first_created_at, case_id
                LIMIT $3 OFFSET $4
            )
            SELECT
                c.*,
                t.trace_family,
                t.status AS trace_status,
                t.model_id,
                t.provider
            FROM eval_experiment_run_cases c
            INNER JOIN paged_cases p ON p.case_id = c.case_id
            LEFT JOIN agent_traces t
                ON t.tenant_id = c.tenant_id
               AND t.trace_id = c.candidate_trace_id
            WHERE c.tenant_id = $1 AND c.run_id = $2::uuid
            ORDER BY p.first_created_at, c.case_id, c.trial_index
            """,
            tenant_id,
            run_id,
            max(1, limit),
            max(0, offset),
        )
        decoded = [self._decode_eval_row(dict(row)) for row in rows]
        cases = _aggregate_live_case_rows(decoded)
        public_cases: list[dict[str, Any]] = []
        for case in cases.values():
            failure_reason = "; ".join(case["errors"] or case["contract_failures"]) or None
            public_cases.append(
                {
                    "example_id": case["example_id"],
                    "case_id": case["case_id"],
                    "candidate_trace_id": case["candidate_trace_id"] or "",
                    "status": case["status"],
                    "aggregate_score": case["aggregate_score"],
                    "failure_reason": failure_reason,
                    "input": case["input"],
                    "expected_output": case["expected_output"],
                    "trial_count": case["trial_count"],
                    "score_stddev": case["score_stddev"],
                    "flaky": case["flaky"],
                    "observed_metrics": case["observed_metrics"],
                    "trace": case["trace"],
                    "scores": [],
                }
            )
        return public_cases, total

    async def compare_experiment_runs(
        self,
        *,
        tenant_id: str,
        baseline_run_id: str,
        candidate_run_id: str,
    ) -> dict[str, Any] | None:
        baseline = await self.get_experiment_run(tenant_id=tenant_id, run_id=baseline_run_id)
        candidate = await self.get_experiment_run(tenant_id=tenant_id, run_id=candidate_run_id)
        if not baseline or not candidate:
            return None
        baseline_summary = baseline.get("score_summary") or {}
        candidate_summary = candidate.get("score_summary") or {}
        baseline_metrics = baseline.get("metrics") or {}
        candidate_metrics = candidate.get("metrics") or {}
        reasons: list[str] = []
        if not _has_versioned_gate_metrics(baseline_summary):
            reasons.append("baseline_gate_metrics_unverifiable")
        if not _has_versioned_gate_metrics(candidate_summary):
            reasons.append("candidate_gate_metrics_unverifiable")
        if (
            baseline.get("run_mode") != "live_candidate"
            or candidate.get("run_mode") != "live_candidate"
        ):
            reasons.append("legacy_unverified")
        if baseline.get("status") != "succeeded" or candidate.get("status") != "succeeded":
            reasons.append("run_not_succeeded")
        if baseline.get("experiment_id") != candidate.get("experiment_id"):
            reasons.append("different_experiment")
        for field, reason in (
            ("dataset_manifest_hash", "dataset_manifest_mismatch"),
            ("evaluator_suite_hash", "evaluator_suite_mismatch"),
        ):
            left = baseline.get(field)
            right = candidate.get(field)
            if not left or not right or left != right:
                reasons.append(reason)
        if int(baseline.get("repetitions") or 1) != int(candidate.get("repetitions") or 1):
            reasons.append("trial_plan_mismatch")
        if baseline_metrics.get("mixed_runtime") or candidate_metrics.get("mixed_runtime"):
            reasons.append("mixed_runtime_fingerprint")

        baseline_rows = await self.list_experiment_run_cases(
            tenant_id=tenant_id,
            run_id=baseline_run_id,
        )
        candidate_rows = await self.list_experiment_run_cases(
            tenant_id=tenant_id,
            run_id=candidate_run_id,
        )
        if any(
            row.get("status") not in {"succeeded", "failed", "skipped"}
            for row in [*baseline_rows, *candidate_rows]
        ):
            reasons.append("incomplete_run_cases")
        baseline_cases = _aggregate_live_case_rows(baseline_rows)
        candidate_cases = _aggregate_live_case_rows(candidate_rows)
        if set(baseline_cases) != set(candidate_cases) or not baseline_cases:
            reasons.append("case_set_mismatch")
        else:
            for case_id in baseline_cases:
                if (
                    baseline_cases[case_id]["trial_count"]
                    != candidate_cases[case_id]["trial_count"]
                ):
                    reasons.append("trial_plan_mismatch")
                    break

        baseline_fingerprint = (
            baseline_metrics.get("actual_fingerprint")
            if isinstance(baseline_metrics.get("actual_fingerprint"), dict)
            else {}
        )
        candidate_fingerprint = (
            candidate_metrics.get("actual_fingerprint")
            if isinstance(candidate_metrics.get("actual_fingerprint"), dict)
            else {}
        )
        required_fingerprint_keys = (
            "system_prompt_hash",
            "tool_schema_hash",
            "model_id",
            "provider",
            "runtime_revision",
        )
        if any(not baseline_fingerprint.get(key) for key in required_fingerprint_keys) or any(
            not candidate_fingerprint.get(key) for key in required_fingerprint_keys
        ):
            reasons.append("missing_runtime_fingerprint")

        fingerprint_dimensions = {
            "prompt": ("system_prompt_hash",),
            "tools": ("tool_schema_hash",),
            "model": ("model_id",),
            "provider": ("provider",),
            "sampling": ("sampling",),
            "runtime": ("runtime_revision",),
            "rag": ("rag_config_hash", "rag_revision_hash"),
            "execution_policy": ("execution_policy",),
        }
        changed_dimensions = [
            dimension
            for dimension, keys in fingerprint_dimensions.items()
            if any(baseline_fingerprint.get(key) != candidate_fingerprint.get(key) for key in keys)
        ]
        metric_specs = {
            "quality_score": ("overall_score", "higher"),
            "behavior_pass_rate": ("behavior_pass_rate", "higher"),
            "critical_pass_rate": ("critical_pass_rate", "higher"),
            "flaky_rate": ("flaky_rate", "lower"),
            "latency_ms": ("latency_p50_ms", "lower"),
            "latency_p95_ms": ("latency_p95_ms", "lower"),
            "input_tokens_per_task": ("input_tokens_per_task", "lower"),
            "output_tokens_per_task": ("output_tokens_per_task", "lower"),
            "total_tokens_per_task": ("total_tokens_per_task", "lower"),
            "cost_per_task_cents": ("cost_per_task_cents", "lower"),
            "execution_error_rate": ("execution_error_rate", "lower"),
            "behavior_failure_rate": ("behavior_failure_rate", "lower"),
        }

        def _metric_value(
            run_summary: dict[str, Any], run_metrics: dict[str, Any], key: str
        ) -> float | None:
            summary_value = _known_number(run_summary.get(key))
            return (
                summary_value if summary_value is not None else _known_number(run_metrics.get(key))
            )

        metric_diffs: dict[str, dict[str, Any]] = {}
        deltas: dict[str, float | None] = {}
        for public_key, (stored_key, direction) in metric_specs.items():
            left = _metric_value(baseline_summary, baseline_metrics, stored_key)
            right = _metric_value(candidate_summary, candidate_metrics, stored_key)
            delta = round(right - left, 4) if left is not None and right is not None else None
            status = "unknown"
            if delta is not None:
                signed = delta if direction == "higher" else -delta
                status = "improved" if signed > 0 else "regressed" if signed < 0 else "unchanged"
            metric_diffs[public_key] = {
                "baseline": left,
                "candidate": right,
                "delta": delta,
                "direction": direction,
                "status": status,
            }
            deltas[public_key] = delta
            deltas.setdefault(stored_key, delta)

        if (
            metric_diffs["quality_score"]["baseline"] is None
            or metric_diffs["quality_score"]["candidate"] is None
        ):
            reasons.append("missing_quality_score")
        if (
            metric_diffs["execution_error_rate"]["baseline"] is None
            or metric_diffs["execution_error_rate"]["candidate"] is None
        ):
            reasons.append("missing_execution_error_rate")
        if (
            metric_diffs["latency_ms"]["baseline"] is None
            or metric_diffs["latency_ms"]["candidate"] is None
        ):
            reasons.append("missing_latency")
        attribution = (
            "unverifiable"
            if reasons
            else "repeatability"
            if not changed_dimensions
            else "isolated_change"
            if len(changed_dimensions) == 1
            else "confounded"
        )

        case_diffs: list[dict[str, Any]] = []
        paired_score_deltas: list[float] = []
        for case_id in sorted(set(baseline_cases) & set(candidate_cases)):
            left = baseline_cases[case_id]
            right = candidate_cases[case_id]
            left_score = _known_number(left.get("aggregate_score"))
            right_score = _known_number(right.get("aggregate_score"))
            score_delta = (
                round(right_score - left_score, 4)
                if left_score is not None and right_score is not None
                else None
            )
            if score_delta is not None:
                paired_score_deltas.append(score_delta)
            if left["behavior_pass"] and not right["behavior_pass"]:
                classification = "regressed"
            elif not left["behavior_pass"] and right["behavior_pass"]:
                classification = "improved"
            elif score_delta is not None and score_delta < -0.02:
                classification = "regressed"
            elif score_delta is not None and score_delta > 0.02:
                classification = "improved"
            elif right["flaky"]:
                classification = "flaky"
            else:
                classification = "unchanged"
            left_tools = [
                {"name": item.get("name"), "status": item.get("status")}
                for item in left.get("tool_trajectory") or []
                if isinstance(item, dict)
            ]
            right_tools = [
                {"name": item.get("name"), "status": item.get("status")}
                for item in right.get("tool_trajectory") or []
                if isinstance(item, dict)
            ]
            tool_diffs = (
                [{"type": "trajectory_changed", "baseline": left_tools, "candidate": right_tools}]
                if left_tools != right_tools
                else []
            )
            left_rag = left.get("rag_evidence") or []
            right_rag = right.get("rag_evidence") or []
            rag_diffs = (
                [{"type": "evidence_changed", "baseline": left_rag, "candidate": right_rag}]
                if left_rag != right_rag
                else []
            )
            case_diffs.append(
                {
                    "case_id": case_id,
                    "status": classification,
                    "critical": right["critical"],
                    "baseline_score": left_score,
                    "candidate_score": right_score,
                    "score_delta": score_delta,
                    "baseline_trace_id": left.get("candidate_trace_id"),
                    "candidate_trace_id": right.get("candidate_trace_id"),
                    "baseline_output": left.get("output_preview") or "",
                    "candidate_output": right.get("output_preview") or "",
                    "baseline_metrics": left.get("observed_metrics") or {},
                    "candidate_metrics": right.get("observed_metrics") or {},
                    "baseline_trial_count": left["trial_count"],
                    "candidate_trial_count": right["trial_count"],
                    "flaky": right["flaky"],
                    "failure_reason": "; ".join(right["errors"] or right["contract_failures"])
                    or None,
                    "tool_diffs": tool_diffs,
                    "rag_diffs": rag_diffs,
                }
            )

        rank = {"regressed": 0, "flaky": 1, "improved": 2, "unchanged": 3}
        case_diffs.sort(key=lambda item: (rank.get(str(item["status"]), 9), str(item["case_id"])))
        confidence_interval = _paired_bootstrap_ci(paired_score_deltas)
        evidence_status = "insufficient_evidence"
        if len(paired_score_deltas) >= 10 and confidence_interval:
            evidence_status = (
                "improvement"
                if confidence_interval[0] > 0
                else "regression"
                if confidence_interval[1] < 0
                else "inconclusive"
            )

        gate_failures = list(dict.fromkeys(reasons))
        gate_warnings: list[str] = []
        critical_flips = [
            case_id
            for case_id in sorted(set(baseline_cases) & set(candidate_cases))
            if candidate_cases[case_id]["critical"]
            and baseline_cases[case_id]["behavior_pass"]
            and not candidate_cases[case_id]["behavior_pass"]
        ]
        if critical_flips:
            gate_failures.append("critical_case_regression")
        quality_delta = deltas.get("quality_score")
        if quality_delta is not None and quality_delta < -0.02:
            gate_failures.append("quality_regression")
        baseline_errors = _known_number(baseline_metrics.get("failed_trials"))
        candidate_errors = _known_number(candidate_metrics.get("failed_trials"))
        error_delta = deltas.get("execution_error_rate")
        if (
            baseline_errors is not None
            and candidate_errors is not None
            and candidate_errors > baseline_errors
        ) or (error_delta is not None and error_delta > 0):
            gate_failures.append("execution_error_regression")

        performance_assertions = {
            "latency_ms_lt": "latency_ms",
            "total_tokens_lt": "total_tokens",
            "cost_cents_lt": "cost_cents",
        }
        for row in candidate_rows:
            observed = row.get("observed_metrics") or {}
            for assertion in row.get("assertions") or []:
                if not isinstance(assertion, dict):
                    continue
                metric_key = performance_assertions.get(str(assertion.get("type") or ""))
                if not metric_key:
                    continue
                actual = _known_number(observed.get(metric_key))
                limit = _known_number(assertion.get("value"))
                if actual is None or limit is None or actual >= limit:
                    gate_failures.append("explicit_performance_constraint_failed")
                    break

        for metric_key in ("latency_ms", "total_tokens_per_task", "cost_per_task_cents"):
            if metric_diffs[metric_key]["status"] == "regressed":
                gate_warnings.append(f"{metric_key}_increased")
        if evidence_status in {"insufficient_evidence", "inconclusive"}:
            gate_warnings.append(evidence_status)
        if any(item["status"] == "regressed" and not item["critical"] for item in case_diffs):
            gate_warnings.append("noncritical_case_regressions")
        if any(item["flaky"] for item in case_diffs):
            gate_warnings.append("flaky_cases_present")
        gate_failures = list(dict.fromkeys(gate_failures))
        gate_warnings = list(dict.fromkeys(gate_warnings))
        regressed_cases = [item for item in case_diffs if item["status"] == "regressed"]
        regression_summary = {
            "baseline_status": baseline.get("status"),
            "candidate_status": candidate.get("status"),
            "regressed_metrics": [
                key for key, item in metric_diffs.items() if item["status"] == "regressed"
            ],
            "regressed_case_count": len(regressed_cases),
            "improved_case_count": sum(1 for item in case_diffs if item["status"] == "improved"),
            "unchanged_case_count": sum(1 for item in case_diffs if item["status"] == "unchanged"),
            "flaky_case_count": sum(1 for item in case_diffs if item["flaky"]),
            "critical_regressions": critical_flips,
            "attribution_status": attribution,
        }
        return {
            "baseline_run_id": baseline_run_id,
            "candidate_run_id": candidate_run_id,
            "baseline_summary": baseline_summary,
            "candidate_summary": candidate_summary,
            "compatibility": {
                "status": "compatible" if not reasons else "incompatible",
                "compatible": not reasons,
                "reasons": list(dict.fromkeys(reasons)),
            },
            "changed_dimensions": changed_dimensions,
            "attribution": attribution,
            "deltas": deltas,
            "metric_diffs": metric_diffs,
            "regression_summary": regression_summary,
            "statistics": {
                "paired_case_count": len(paired_score_deltas),
                "quality_delta_ci_95": confidence_interval,
                "evidence_status": evidence_status,
                "wins": sum(1 for value in paired_score_deltas if value > 0.02),
                "ties": sum(1 for value in paired_score_deltas if -0.02 <= value <= 0.02),
                "losses": sum(1 for value in paired_score_deltas if value < -0.02),
                "seed": 42,
            },
            "gate": {
                "status": "fail" if gate_failures else "pass",
                "failures": gate_failures,
                "warnings": gate_warnings,
            },
            "case_diffs": case_diffs,
        }

    async def get_dashboard(
        self,
        *,
        tenant_id: str,
        days: int = 7,
    ) -> dict[str, Any]:
        summary = await self.get_summary(tenant_id=tenant_id, days=days)
        counts = await self.fetchrow(
            """
            SELECT
                (SELECT COUNT(*) FROM eval_datasets WHERE tenant_id = $1)::int AS dataset_count,
                (SELECT COUNT(*) FROM eval_examples WHERE tenant_id = $1)::int AS example_count,
                (SELECT COUNT(*) FROM eval_evaluators WHERE tenant_id = $1)::int AS evaluator_count,
                (SELECT COUNT(*) FROM eval_experiments WHERE tenant_id = $1)::int AS experiment_count,
                (SELECT COUNT(*) FROM eval_experiment_runs WHERE tenant_id = $1)::int AS run_count,
                (SELECT COUNT(*) FROM eval_examples
                 WHERE tenant_id = $1 AND metadata->>'review_status' = 'pending')::int AS judge_pending_count
            """,
            tenant_id,
        )
        run_health = await self.fetchrow(
            """
            SELECT
                COUNT(*) FILTER (WHERE status = 'queued')::int AS queued_runs,
                COUNT(*) FILTER (WHERE status = 'running')::int AS running_runs,
                COUNT(*) FILTER (WHERE status = 'succeeded')::int AS succeeded_runs,
                COUNT(*) FILTER (WHERE status = 'failed')::int AS failed_runs
            FROM eval_experiment_runs
            WHERE tenant_id = $1
            """,
            tenant_id,
        )
        queue_health = await self.fetchrow(
            """
            SELECT
                COUNT(*) FILTER (WHERE status = 'queued')::int AS queued_jobs,
                COUNT(*) FILTER (WHERE status = 'running')::int AS running_jobs,
                COUNT(*) FILTER (WHERE status = 'failed')::int AS failed_jobs
            FROM agent_trace_outbox
            WHERE tenant_id = $1
            """,
            tenant_id,
        )
        runtime_health = await self.fetchrow(
            """
            SELECT
                COUNT(*) FILTER (WHERE trace_family = 'assistant')::int AS assistant_captured_traces,
                COUNT(*) FILTER (WHERE trace_family = 'assistant' AND status = 'succeeded')::int AS assistant_succeeded_traces,
                COUNT(*) FILTER (WHERE trace_family = 'assistant' AND status = 'failed')::int AS assistant_failed_traces,
                COUNT(*) FILTER (WHERE trace_family = 'rag')::int AS rag_captured_traces,
                COUNT(*) FILTER (WHERE trace_family = 'langgraph_proxy')::int AS langgraph_captured_traces,
                COUNT(*) FILTER (WHERE trace_family = 'assistant' AND metadata ? 'runtime_trajectory')::int AS runtime_trajectory_traces,
                COUNT(*) FILTER (
                    WHERE trace_family = 'assistant'
                      AND COALESCE(metadata->'runtime_trajectory'->'trace_writer_health'->>'issue_count', '0')::int > 0
                )::int AS trace_writer_issue_traces,
                COUNT(*) FILTER (
                    WHERE trace_family = 'assistant'
                      AND COALESCE(metadata->'runtime_trajectory'->>'exit_reason', '') IN (
                          'tool_error',
                          'approval_denied',
                          'approval_required',
                          'max_iterations',
                          'interrupted',
                          'cancelled',
                          'timeout'
                      )
                )::int AS critical_runtime_failures
            FROM agent_traces
            WHERE tenant_id = $1
              AND created_at >= NOW() - make_interval(days => $2::int)
            """,
            tenant_id,
            max(1, days),
        )
        tool_safety_row = await self.fetchrow(
            """
            SELECT COUNT(DISTINCT s.trace_id)::int AS tool_safety_failures
            FROM agent_trace_spans s
            INNER JOIN agent_traces t ON t.trace_id = s.trace_id
            WHERE t.tenant_id = $1
              AND t.created_at >= NOW() - make_interval(days => $2::int)
              AND (
                  s.attributes->>'direct_registry_denied' = 'true'
                  OR COALESCE(s.attributes->'gateway_policy_decision'->>'decision', '') IN ('deny', 'denied', 'blocked')
                  OR COALESCE(s.attributes->'sandbox_decision'->>'decision', '') IN ('deny', 'denied', 'blocked')
                  OR COALESCE(s.attributes->'sandbox_decision'->>'available', 'true') = 'false'
              )
            """,
            tenant_id,
            max(1, days),
        )
        latest_run_row = await self.fetchrow(
            """
            SELECT
                (
                    SELECT COALESCE(target_snapshot->>'candidate_label', run_id::text)
                    FROM eval_experiment_runs
                    WHERE tenant_id = $1
                      AND COALESCE(target_snapshot->>'candidate_label', '') ILIKE '%baseline%'
                    ORDER BY created_at DESC
                    LIMIT 1
                ) AS latest_baseline,
                (
                    SELECT COALESCE(target_snapshot->>'candidate_label', run_id::text)
                    FROM eval_experiment_runs
                    WHERE tenant_id = $1
                      AND (
                          COALESCE(target_snapshot->>'candidate_label', '') ILIKE '%candidate%'
                          OR COALESCE(target_snapshot->>'candidate_label', '') = ''
                      )
                    ORDER BY created_at DESC
                    LIMIT 1
                ) AS latest_candidate
            """,
            tenant_id,
        )
        metrics = {**summary, **dict(counts or {})}
        runtime_metrics = dict(runtime_health or {})
        assistant_total = int(runtime_metrics.get("assistant_captured_traces") or 0)
        assistant_succeeded = int(runtime_metrics.get("assistant_succeeded_traces") or 0)
        runtime_trajectory_traces = int(runtime_metrics.get("runtime_trajectory_traces") or 0)
        runtime_metrics["tool_safety_failures"] = int(
            (tool_safety_row or {}).get("tool_safety_failures") or 0
        )
        runtime_metrics["pass_rate"] = (
            round(assistant_succeeded / assistant_total, 4) if assistant_total else 0
        )
        runtime_metrics["trajectory_pass_rate"] = (
            round(runtime_trajectory_traces / assistant_total, 4) if assistant_total else 0
        )
        runtime_metrics["assistant_status"] = "enabled"
        runtime_metrics["rag_status"] = (
            "wired" if int(runtime_metrics.get("rag_captured_traces") or 0) > 0 else "partial"
        )
        runtime_metrics["langgraph_status"] = (
            "wired" if int(runtime_metrics.get("langgraph_captured_traces") or 0) > 0 else "partial"
        )
        metrics["latest_baseline"] = (latest_run_row or {}).get("latest_baseline")
        metrics["latest_candidate"] = (latest_run_row or {}).get("latest_candidate")
        metrics["pass_rate"] = runtime_metrics["pass_rate"]
        metrics["trajectory_pass_rate"] = runtime_metrics["trajectory_pass_rate"]
        metrics["critical_failures"] = (
            int(runtime_metrics.get("critical_runtime_failures") or 0)
            + runtime_metrics["tool_safety_failures"]
        )
        metrics["tool_safety_failures"] = runtime_metrics["tool_safety_failures"]
        return {
            "metrics": metrics,
            "run_health": dict(run_health or {}),
            "queue_health": dict(queue_health or {}),
            "runtime_health": runtime_metrics,
            "latest_gate_status": {"status": "not_run", "source": "offline"},
        }

    async def get_evaluator(
        self,
        *,
        tenant_id: str,
        evaluator_id: str,
    ) -> dict[str, Any] | None:
        row = await self.fetchrow(
            "SELECT * FROM eval_evaluators WHERE tenant_id = $1 AND evaluator_id = $2::uuid",
            tenant_id,
            evaluator_id,
        )
        return self._decode_eval_row(row) if row else None

    async def list_datasets(
        self,
        *,
        tenant_id: str,
        limit: int = 50,
        offset: int = 0,
    ) -> tuple[list[dict[str, Any]], int]:
        count_row = await self.fetchrow(
            "SELECT COUNT(*) AS total FROM eval_datasets WHERE tenant_id = $1",
            tenant_id,
        )
        total = int(count_row.get("total") or 0) if count_row else 0
        rows = await self.fetch(
            """
            SELECT * FROM eval_datasets
            WHERE tenant_id = $1
            ORDER BY created_at DESC
            LIMIT $2 OFFSET $3
            """,
            tenant_id,
            limit,
            offset,
        )
        return [self._decode_eval_row(row) for row in rows], total

    async def get_dataset(
        self,
        *,
        tenant_id: str,
        dataset_id: str,
    ) -> dict[str, Any] | None:
        row = await self.fetchrow(
            "SELECT * FROM eval_datasets WHERE tenant_id = $1 AND dataset_id = $2::uuid",
            tenant_id,
            dataset_id,
        )
        return self._decode_eval_row(row) if row else None

    async def list_examples(
        self,
        *,
        tenant_id: str,
        dataset_id: str,
        split: str | None = None,
        limit: int = 200,
        offset: int = 0,
    ) -> tuple[list[dict[str, Any]], int]:
        params: list[Any] = [tenant_id, dataset_id]
        filters = ["tenant_id = $1", "dataset_id = $2::uuid"]
        if split:
            params.append(split)
            filters.append(f"split = ${len(params)}")
        where_clause = " AND ".join(filters)
        count_row = await self.fetchrow(
            f"SELECT COUNT(*) AS total FROM eval_examples WHERE {where_clause}",
            *params,
        )
        total = int(count_row.get("total") or 0) if count_row else 0
        page_params = [*params, limit, offset]
        rows = await self.fetch(
            f"""
            SELECT * FROM eval_examples
            WHERE {where_clause}
            ORDER BY created_at DESC
            LIMIT ${len(params) + 1} OFFSET ${len(params) + 2}
            """,
            *page_params,
        )
        return [self._decode_eval_row(row) for row in rows], total

    async def list_example_manifest(
        self,
        *,
        tenant_id: str,
        dataset_id: str,
    ) -> list[dict[str, Any]]:
        rows = await self.fetch(
            """
            SELECT * FROM eval_examples
            WHERE tenant_id = $1 AND dataset_id = $2::uuid
            ORDER BY created_at DESC, example_id DESC
            """,
            tenant_id,
            dataset_id,
        )
        return [self._decode_eval_row(row) for row in rows]

    async def list_evaluators(
        self,
        *,
        tenant_id: str,
        limit: int = 50,
        offset: int = 0,
    ) -> tuple[list[dict[str, Any]], int]:
        count_row = await self.fetchrow(
            "SELECT COUNT(*) AS total FROM eval_evaluators WHERE tenant_id = $1",
            tenant_id,
        )
        total = int(count_row.get("total") or 0) if count_row else 0
        rows = await self.fetch(
            """
            SELECT * FROM eval_evaluators
            WHERE tenant_id = $1
            ORDER BY created_at DESC
            LIMIT $2 OFFSET $3
            """,
            tenant_id,
            limit,
            offset,
        )
        return [self._decode_eval_row(row) for row in rows], total

    async def list_experiments(
        self,
        *,
        tenant_id: str,
        limit: int = 50,
        offset: int = 0,
    ) -> tuple[list[dict[str, Any]], int]:
        count_row = await self.fetchrow(
            "SELECT COUNT(*) AS total FROM eval_experiments WHERE tenant_id = $1",
            tenant_id,
        )
        total = int(count_row.get("total") or 0) if count_row else 0
        rows = await self.fetch(
            """
            SELECT * FROM eval_experiments
            WHERE tenant_id = $1
            ORDER BY created_at DESC
            LIMIT $2 OFFSET $3
            """,
            tenant_id,
            limit,
            offset,
        )
        return [self._decode_eval_row(row) for row in rows], total

    async def create_eval_score(
        self,
        *,
        tenant_id: str,
        trace_id: str,
        created_by: str,
        payload: dict[str, Any],
        trace_family: str = "assistant",
    ) -> dict[str, Any] | None:
        return await self.create_score(
            tenant_id=tenant_id,
            trace_id=trace_id,
            created_by=created_by,
            payload=payload,
            trace_family=trace_family,
        )

    async def get_agent_operations_summary(
        self,
        *,
        tenant_id: str,
        agent_id: str,
        agent_version_id: str | None = None,
        publication_id: str | None = None,
        channel: str | None = None,
        started_after: Any | None = None,
        started_before: Any | None = None,
    ) -> dict[str, Any]:
        """Aggregate only explicit Agent runtime dimensions, never metadata hints."""

        params: list[Any] = [tenant_id, agent_id]
        filters = ["tenant_id = $1", "agent_id = $2::uuid"]
        if agent_version_id:
            params.append(agent_version_id)
            filters.append(f"agent_version_id = ${len(params)}::uuid")
        if publication_id:
            params.append(publication_id)
            filters.append(f"publication_id = ${len(params)}::uuid")
        if channel:
            params.append(channel)
            filters.append(f"channel = ${len(params)}")
        if started_after is not None:
            params.append(started_after)
            filters.append(f"started_at >= ${len(params)}")
        if started_before is not None:
            params.append(started_before)
            filters.append(f"started_at <= ${len(params)}")
        where_clause = " AND ".join(filters)
        row = await self.fetchrow(
            f"""
            WITH filtered_traces AS (
                SELECT
                    trace_id, tenant_id, session_id, agent_version_id,
                    publication_id, channel, status, total_latency_ms,
                    first_token_latency_ms, total_tokens, total_cost_cents,
                    created_at
                FROM agent_traces
                WHERE {where_clause}
            ),
            trace_metrics AS (
                SELECT
                    COUNT(*)::int AS total_runs,
                    COUNT(*) FILTER (WHERE status = 'succeeded')::int AS succeeded_runs,
                    COUNT(*) FILTER (WHERE status IN ('failed', 'timeout'))::int AS failed_runs,
                    COUNT(DISTINCT session_id)::int AS sessions,
                    COALESCE(AVG(total_latency_ms), 0)::int AS avg_latency_ms,
                    COALESCE(PERCENTILE_CONT(0.50) WITHIN GROUP (ORDER BY total_latency_ms), 0)::int AS p50_latency_ms,
                    COALESCE(PERCENTILE_CONT(0.95) WITHIN GROUP (ORDER BY total_latency_ms), 0)::int AS p95_latency_ms,
                    COALESCE(AVG(first_token_latency_ms), 0)::int AS avg_ttft_ms,
                    COALESCE(PERCENTILE_CONT(0.50) WITHIN GROUP (ORDER BY first_token_latency_ms), 0)::int AS p50_ttft_ms,
                    COALESCE(PERCENTILE_CONT(0.95) WITHIN GROUP (ORDER BY first_token_latency_ms), 0)::int AS p95_ttft_ms,
                    COALESCE(SUM(total_tokens), 0)::bigint AS total_tokens,
                    COALESCE(SUM(total_cost_cents), 0)::bigint AS total_cost_cents,
                    MIN(created_at) AS oldest_trace_at,
                    MAX(created_at) AS newest_trace_at
                FROM filtered_traces
            ),
            classified_spans AS (
                SELECT
                    s.span_kind,
                    s.status,
                    CASE
                        WHEN COALESCE(
                            s.attributes->>'retrieval.document_count',
                            s.attributes#>>'{{retrieval,document_count}}'
                        ) ~ '^[0-9]+$'
                        THEN COALESCE(
                            s.attributes->>'retrieval.document_count',
                            s.attributes#>>'{{retrieval,document_count}}'
                        )::int
                        WHEN jsonb_typeof(COALESCE(
                            s.attributes->'retrieval.documents',
                            s.attributes#>'{{retrieval,documents}}'
                        )) = 'array'
                        THEN jsonb_array_length(COALESCE(
                            s.attributes->'retrieval.documents',
                            s.attributes#>'{{retrieval,documents}}'
                        ))
                        ELSE 0
                    END AS retrieved_document_count
                FROM agent_trace_spans s
                INNER JOIN filtered_traces t ON t.trace_id = s.trace_id
                WHERE s.span_kind IN ('tool_execution', 'retriever')
            ),
            span_metrics AS (
                SELECT
                    COUNT(*) FILTER (WHERE span_kind = 'tool_execution')::int AS tool_calls,
                    COUNT(*) FILTER (
                        WHERE span_kind = 'tool_execution' AND status = 'succeeded'
                    )::int AS tool_succeeded,
                    COUNT(*) FILTER (WHERE span_kind = 'retriever')::int AS knowledge_queries,
                    COUNT(*) FILTER (
                        WHERE span_kind = 'retriever'
                          AND status = 'succeeded'
                          AND retrieved_document_count > 0
                    )::int AS knowledge_hits
                FROM classified_spans
            ),
            feedback_metrics AS (
                SELECT
                    COUNT(*)::int AS feedback_count,
                    COUNT(*) FILTER (WHERE f.rating = 1)::int AS positive_feedback_count
                FROM agent_runtime_feedback f
                WHERE f.tenant_id = $1
                  AND EXISTS (
                      SELECT 1
                      FROM filtered_traces t
                      WHERE t.tenant_id = f.tenant_id
                        AND t.agent_version_id = f.agent_version_id
                        AND t.publication_id = f.publication_id
                        AND t.channel = f.channel
                        AND t.session_id = f.session_id
                  )
            )
            SELECT trace_metrics.*, span_metrics.*, feedback_metrics.*
            FROM trace_metrics
            CROSS JOIN span_metrics
            CROSS JOIN feedback_metrics
            """,
            *params,
        )
        breakdown_rows = await self.fetch(
            f"""
            SELECT
                channel,
                agent_version_id,
                publication_id,
                COUNT(*)::int AS run_count,
                COUNT(*) FILTER (WHERE status IN ('failed', 'timeout'))::int AS failed_count,
                COALESCE(PERCENTILE_CONT(0.95) WITHIN GROUP (ORDER BY total_latency_ms), 0)::int AS p95_latency_ms,
                COALESCE(SUM(total_tokens), 0)::bigint AS total_tokens,
                COALESCE(SUM(total_cost_cents), 0)::bigint AS total_cost_cents
            FROM agent_traces
            WHERE {where_clause}
            GROUP BY channel, agent_version_id, publication_id
            ORDER BY run_count DESC, channel ASC
            """,
            *params,
        )
        retention = await self.fetchrow(
            """
            SELECT
                COALESCE(p.trace_retention_days, 90)::int AS trace_retention_days,
                COALESCE(p.legal_hold, FALSE) AS legal_hold,
                (
                    SELECT MAX(r.completed_at)
                    FROM agent_data_deletion_requests r
                    WHERE r.tenant_id = $1 AND r.agent_id = $2::uuid
                      AND r.scope = 'retention' AND r.status = 'completed'
                ) AS last_retention_cleanup_at
            FROM agents a
            LEFT JOIN agent_governance_policies p
              ON p.tenant_id = a.tenant_id AND p.agent_id = a.agent_id
            WHERE a.tenant_id = $1 AND a.agent_id = $2::uuid
            """,
            tenant_id,
            agent_id,
        )
        result = dict(row or {})
        total = int(result.get("total_runs") or 0)
        succeeded = int(result.get("succeeded_runs") or 0)
        tool_calls = int(result.get("tool_calls") or 0)
        tool_succeeded = int(result.get("tool_succeeded") or 0)
        knowledge_queries = int(result.get("knowledge_queries") or 0)
        knowledge_hits = int(result.get("knowledge_hits") or 0)
        feedback_count = int(result.get("feedback_count") or 0)
        positive_feedback = int(result.get("positive_feedback_count") or 0)
        result["success_rate"] = (succeeded / total) if total else None
        result["tool_success_rate"] = tool_succeeded / tool_calls if tool_calls else None
        result["knowledge_hit_rate"] = (
            knowledge_hits / knowledge_queries if knowledge_queries else None
        )
        result["feedback_positive_rate"] = (
            positive_feedback / feedback_count if feedback_count else None
        )
        result["breakdown"] = [self._decode_trace_row(item) for item in breakdown_rows]
        result["retention"] = self._decode_trace_row(retention or {})
        result["retention_limited"] = bool((retention or {}).get("last_retention_cleanup_at"))
        return self._decode_trace_row(result)

    async def get_summary(
        self,
        *,
        tenant_id: str,
        user_id: str | None = None,
        days: int = 7,
    ) -> dict[str, Any]:
        params: list[Any] = [tenant_id, max(1, days)]
        filters = [
            "tenant_id = $1",
            "created_at >= NOW() - make_interval(days => $2::int)",
        ]
        if user_id:
            params.append(user_id)
            filters.append(f"user_id = ${len(params)}")
        where_clause = " AND ".join(filters)
        row = await self.fetchrow(
            f"""
            SELECT
                COUNT(*)::int AS total_traces,
                COUNT(*) FILTER (WHERE status = 'failed')::int AS failed_traces,
                COUNT(*) FILTER (WHERE status = 'succeeded')::int AS succeeded_traces,
                COUNT(*) FILTER (WHERE trace_family = 'assistant')::int AS assistant_traces,
                COUNT(*) FILTER (WHERE trace_family = 'langgraph_proxy')::int AS langgraph_traces,
                COUNT(*) FILTER (WHERE trace_family = 'rag')::int AS rag_traces,
                COALESCE(AVG(total_latency_ms), 0)::int AS avg_latency_ms,
                COALESCE(PERCENTILE_CONT(0.95) WITHIN GROUP (ORDER BY total_latency_ms), 0)::int AS p95_latency_ms,
                COALESCE(SUM(total_tokens), 0)::bigint AS total_tokens,
                COALESCE(SUM(total_cost_cents), 0)::bigint AS total_cost_cents
            FROM agent_traces
            WHERE {where_clause}
            """,
            *params,
        )
        scored_filters = [
            "t.tenant_id = $1",
            "t.created_at >= NOW() - make_interval(days => $2::int)",
        ]
        if user_id:
            scored_filters.append(f"t.user_id = ${len(params)}")
        scored_row = await self.fetchrow(
            f"""
            SELECT COUNT(DISTINCT s.trace_id)::int AS scored_traces
            FROM agent_trace_scores s
            INNER JOIN agent_traces t ON t.trace_id = s.trace_id
            WHERE {" AND ".join(scored_filters)}
            """,
            *params,
        )
        summary = dict(row or {})
        summary["scored_traces"] = int((scored_row or {}).get("scored_traces") or 0)
        summary["window_days"] = max(1, days)
        return summary

    async def get_kb_ragas_summary(
        self,
        *,
        tenant_id: str,
        days: int = 7,
        dataset_id: str | None = None,
    ) -> dict[str, Any]:
        params: list[Any] = [tenant_id, max(1, days)]
        trace_filters = [
            "t.tenant_id = $1",
            "t.trace_family = 'rag'",
            "t.created_at >= NOW() - make_interval(days => $2::int)",
        ]
        if dataset_id:
            params.append(dataset_id)
            trace_filters.append(f"t.metadata->>'dataset_id' = ${len(params)}")
        trace_where = " AND ".join(trace_filters)
        latest_scores_ctes = f"""
            in_window_traces AS (
                SELECT t.trace_id
                FROM agent_traces t
                WHERE {trace_where}
            ),
            latest_scores AS (
                SELECT ranked_scores.*
                FROM (
                    SELECT
                        s.*,
                        ROW_NUMBER() OVER (
                            PARTITION BY s.trace_id, s.evaluator_id,
                                COALESCE(s.evaluator_version, ''), s.score_name
                            ORDER BY s.created_at DESC, s.score_id DESC
                        ) AS score_revision
                    FROM agent_trace_scores s
                    INNER JOIN in_window_traces t ON t.trace_id = s.trace_id
                    WHERE s.score_source = 'kb_ragas'
                ) ranked_scores
                WHERE ranked_scores.score_revision = 1
            )
        """

        trace_row = await self.fetchrow(
            f"""
            WITH {latest_scores_ctes}
            SELECT
                (
                    SELECT COUNT(DISTINCT t.trace_id)
                    FROM in_window_traces t
                )::int AS rag_traces,
                COUNT(DISTINCT CASE
                    WHEN s.label IN ('pass', 'fail') THEN s.trace_id
                END)::int AS ragas_scored_traces
            FROM latest_scores s
            """,
            *params,
        )

        metric_rows = await self.fetch(
            f"""
            WITH {latest_scores_ctes}
            SELECT
                s.score_name AS metric,
                COALESCE(
                    AVG(s.numeric_value) FILTER (WHERE s.label IN ('pass', 'fail')),
                    0
                )::float AS average_score,
                COUNT(*) FILTER (WHERE s.label IN ('pass', 'fail'))::int AS scored_count,
                COUNT(*) FILTER (WHERE s.label = 'pass')::int AS pass_count,
                COUNT(*) FILTER (WHERE s.label = 'fail')::int AS fail_count,
                COUNT(*) FILTER (WHERE s.label = 'review')::int AS review_count
            FROM latest_scores s
            GROUP BY s.score_name
            ORDER BY s.score_name
            """,
            *params,
        )

        judge_row = await self.fetchrow(
            f"""
            WITH {latest_scores_ctes}
            SELECT s.metadata->>'judge_model' AS judge_model
            FROM latest_scores s
            WHERE COALESCE(s.metadata->>'judge_model', '') <> ''
            ORDER BY s.created_at DESC, s.score_id DESC
            LIMIT 1
            """,
            *params,
        )

        return {
            "window_days": max(1, days),
            "dataset_id": dataset_id,
            "rag_traces": int((trace_row or {}).get("rag_traces") or 0),
            "ragas_scored_traces": int((trace_row or {}).get("ragas_scored_traces") or 0),
            "metrics": [dict(row) for row in metric_rows],
            "latest_judge_model": (judge_row or {}).get("judge_model"),
        }

    async def trace_has_kb_ragas_score(
        self,
        *,
        tenant_id: str,
        trace_id: str,
        evaluator_id: str | None = None,
    ) -> bool:
        params: list[Any] = [tenant_id, trace_id]
        filters = [
            "s.trace_id = $2::uuid",
            "t.tenant_id = $1",
            "s.score_source = 'kb_ragas'",
            "s.label IN ('pass', 'fail')",
        ]
        if evaluator_id:
            params.append(evaluator_id)
            filters.append(f"s.evaluator_id = ${len(params)}::uuid")
        row = await self.fetchrow(
            f"""
            SELECT 1
            FROM agent_trace_scores s
            INNER JOIN agent_traces t ON t.trace_id = s.trace_id
            WHERE {" AND ".join(filters)}
            LIMIT 1
            """,
            *params,
        )
        return row is not None

    async def purge_expired_traces(
        self,
        *,
        default_retention_days: int = 90,
        batch_size: int = 500,
    ) -> int:
        rows = await self.fetch(
            """
            WITH doomed AS (
                SELECT trace_id
                FROM agent_traces
                WHERE COALESCE(
                    retention_expires_at,
                    created_at + make_interval(days => $1::int)
                ) < NOW()
                ORDER BY created_at ASC
                LIMIT $2
            )
            DELETE FROM agent_traces t
            USING doomed d
            WHERE t.trace_id = d.trace_id
            RETURNING t.trace_id
            """,
            max(1, default_retention_days),
            max(1, batch_size),
        )
        return len(rows)

    def _decode_trace_row(self, row: dict[str, Any]) -> dict[str, Any]:
        decoded = dict(row)
        for key in (
            "trace_id",
            "agent_id",
            "agent_version_id",
            "publication_id",
        ):
            if decoded.get(key) is not None:
                decoded[key] = str(decoded[key])
        for key in ("redaction_state", "metadata", "metrics", "privacy"):
            decoded[key] = self._decode_json(decoded.get(key), default={})
        decoded["scores_count"] = int(decoded.get("scores_count") or 0)
        return decoded

    def _decode_span_row(self, row: dict[str, Any]) -> dict[str, Any]:
        decoded = dict(row)
        for key in ("span_id", "trace_id", "parent_span_id"):
            if decoded.get(key) is not None:
                decoded[key] = str(decoded[key])
        decoded["attributes"] = self._decode_json(decoded.get("attributes"), default={})
        return decoded

    def _decode_event_row(self, row: dict[str, Any]) -> dict[str, Any]:
        decoded = dict(row)
        for key in ("event_id", "trace_id", "span_id"):
            if decoded.get(key) is not None:
                decoded[key] = str(decoded[key])
        decoded["payload"] = self._decode_json(decoded.get("payload"), default={})
        return decoded

    def _decode_score_row(self, row: dict[str, Any]) -> dict[str, Any]:
        decoded = dict(row)
        for key in ("score_id", "trace_id", "span_id"):
            if decoded.get(key) is not None:
                decoded[key] = str(decoded[key])
        decoded["metadata"] = self._decode_json(decoded.get("metadata"), default={})
        return decoded

    def _decode_eval_row(self, row: dict[str, Any] | None) -> dict[str, Any]:
        decoded = dict(row or {})
        for key, value in list(decoded.items()):
            if key.endswith("_id") and value is not None:
                decoded[key] = str(value)
        for key in (
            "schema",
            "metadata",
            "input",
            "expected_output",
            "expected_trajectory",
            "sampling_config",
            "filter_config",
            "target_config",
            "target_snapshot",
            "score_summary",
            "metrics",
            "candidate_fingerprint",
            "execution_config",
            "observed_metrics",
            "payload",
        ):
            if key in decoded:
                decoded[key] = self._decode_json(decoded.get(key), default={})
        if "assertions" in decoded:
            decoded["assertions"] = self._decode_json(decoded.get("assertions"), default=[])
        return decoded

    def _decode_json(self, value: Any, *, default: Any) -> Any:
        if value is None:
            return default
        if isinstance(value, str):
            try:
                return json.loads(value)
            except json.JSONDecodeError:
                return default
        return value

    def _json_dumps(self, value: Any) -> str:
        return json.dumps(value, ensure_ascii=False, separators=(",", ":"), default=str)
