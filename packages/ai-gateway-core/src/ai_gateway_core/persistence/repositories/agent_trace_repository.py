from __future__ import annotations

import json
import uuid
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
        if turn_index is not None:
            params.append(str(turn_index))
            filters.append(
                f"t.metadata->'transcript_locator'->>'turn_index' = ${len(params)}"
            )
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

        row = await self.fetchrow(
            """
            INSERT INTO agent_trace_scores (
                trace_id, span_id, score_name, score_type, numeric_value,
                boolean_value, categorical_value, text_value, label, explanation,
                scorer_type, evaluator_version, target_type, target_id,
                evaluator_id, evaluator_name, score_source, confidence,
                created_by, metadata
            ) VALUES (
                $1, $2, $3, $4, $5,
                $6, $7, $8, $9, $10,
                $11, $12, $13, $14,
                $15::uuid, $16, $17, $18,
                $19, $20::jsonb
            )
            RETURNING *
            """,
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
            trace.get("started_at"),
            trace.get("ended_at"),
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
            trace.get("retention_expires_at"),
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
        span_id = str(span.get("span_id") or uuid.uuid5(uuid.UUID(trace_id), span.get("name") or str(uuid.uuid4())))
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
            span.get("started_at"),
            span.get("ended_at"),
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
            event.get("occurred_at"),
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
            WHERE {' AND '.join(filters)}
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
        input_payload = {
            "input_preview": trace.get("input_preview") or "",
            "thread_id": trace.get("thread_id") or trace.get("session_id"),
            "run_id": trace.get("run_id"),
            "request_id": trace.get("request_id"),
            "metadata": trace.get("metadata") or {},
        }
        expected_output = payload.get("expected_output") or {
            "output_preview": trace.get("output_preview") or "",
        }
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
            self._json_dumps(payload.get("metadata") or {}),
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
        return {
            str(row["case_id"])
            for row in rows
            if row.get("case_id")
        }

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
            if mode == "skip_duplicates" and case_id:
                if case_id in existing_case_ids or case_id in seen_in_request:
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
              AND status IN ('queued', 'running', 'succeeded')
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
            SELECT $1, 'trace.ingested', $2::jsonb
            WHERE NOT EXISTS (
                SELECT 1
                FROM agent_trace_outbox
                WHERE tenant_id = $1
                  AND job_type = 'trace.ingested'
                  AND status IN ('queued', 'running')
                  AND payload->>'trace_id' = $3
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
                """,
                job_id,
                error[:4000],
                retry_after_seconds,
                max_attempts,
            )
            return
        await self.execute(
            """
            UPDATE agent_trace_outbox
            SET status = 'failed', last_error = $2, updated_at = NOW()
            WHERE job_id = $1::uuid
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
        numeric_keys = {
            key
            for key in set(baseline_summary) | set(candidate_summary)
            if isinstance(baseline_summary.get(key), int | float)
            or isinstance(candidate_summary.get(key), int | float)
        }
        deltas = {
            key: round(float(candidate_summary.get(key) or 0) - float(baseline_summary.get(key) or 0), 4)
            for key in sorted(numeric_keys)
        }
        regression_summary = {
            "baseline_status": baseline.get("status"),
            "candidate_status": candidate.get("status"),
            "regressed_metrics": [key for key, value in deltas.items() if value < 0],
        }
        return {
            "baseline_run_id": baseline_run_id,
            "candidate_run_id": candidate_run_id,
            "baseline_summary": baseline_summary,
            "candidate_summary": candidate_summary,
            "deltas": deltas,
            "regression_summary": regression_summary,
            "case_diffs": [],
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
        metrics = {**summary, **dict(counts or {})}
        metrics.setdefault("latest_baseline", None)
        metrics.setdefault("latest_candidate", None)
        metrics.setdefault("pass_rate", 0)
        metrics.setdefault("trajectory_pass_rate", 0)
        metrics.setdefault("critical_failures", 0)
        return {
            "metrics": metrics,
            "run_health": dict(run_health or {}),
            "queue_health": dict(queue_health or {}),
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
            WHERE {' AND '.join(scored_filters)}
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

        trace_row = await self.fetchrow(
            f"""
            SELECT
                COUNT(DISTINCT t.trace_id)::int AS rag_traces,
                COUNT(DISTINCT CASE
                    WHEN s.score_source = 'kb_ragas' AND COALESCE(s.label, '') <> 'review'
                    THEN t.trace_id
                END)::int AS ragas_scored_traces
            FROM agent_traces t
            LEFT JOIN agent_trace_scores s
                ON s.trace_id = t.trace_id
               AND s.score_source = 'kb_ragas'
               AND COALESCE(s.label, '') <> 'review'
            WHERE {trace_where}
            """,
            *params,
        )

        metric_rows = await self.fetch(
            f"""
            SELECT
                s.score_name AS metric,
                COALESCE(AVG(s.numeric_value), 0)::float AS average_score,
                COUNT(*)::int AS scored_count,
                COUNT(*) FILTER (WHERE s.label = 'pass')::int AS pass_count,
                COUNT(*) FILTER (WHERE s.label = 'fail')::int AS fail_count,
                COUNT(*) FILTER (WHERE s.label = 'review')::int AS review_count
            FROM agent_trace_scores s
            INNER JOIN agent_traces t ON t.trace_id = s.trace_id
            WHERE {trace_where}
              AND s.score_source = 'kb_ragas'
            GROUP BY s.score_name
            ORDER BY s.score_name
            """,
            *params,
        )

        judge_row = await self.fetchrow(
            f"""
            SELECT s.metadata->>'judge_model' AS judge_model
            FROM agent_trace_scores s
            INNER JOIN agent_traces t ON t.trace_id = s.trace_id
            WHERE {trace_where}
              AND s.score_source = 'kb_ragas'
              AND COALESCE(s.metadata->>'judge_model', '') <> ''
            ORDER BY s.created_at DESC
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
            "COALESCE(s.label, '') <> 'review'",
        ]
        if evaluator_id:
            params.append(evaluator_id)
            filters.append(f"s.evaluator_id = ${len(params)}::uuid")
        row = await self.fetchrow(
            f"""
            SELECT 1
            FROM agent_trace_scores s
            INNER JOIN agent_traces t ON t.trace_id = s.trace_id
            WHERE {' AND '.join(filters)}
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
        for key in ("trace_id",):
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
            "sampling_config",
            "filter_config",
            "target_config",
            "target_snapshot",
            "score_summary",
            "metrics",
            "payload",
        ):
            if key in decoded:
                decoded[key] = self._decode_json(decoded.get(key), default={})
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
