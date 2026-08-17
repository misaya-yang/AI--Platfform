"""Best-effort AI Assistant trace writer.

Trace capture must never sit on the user-facing chat critical path. The public
methods in this module only submit bounded background work; all database IO,
redaction, and payload shaping happens inside those background tasks.
"""

from __future__ import annotations

import asyncio
import hashlib
import json
import logging
import time
import urllib.parse
import uuid
from collections.abc import Coroutine
from dataclasses import dataclass, field
from datetime import datetime, timezone
from typing import Any

from ai_gateway_core.billing.pricing_catalog import calculate_token_cost_cents
from ai_gateway_core.logging import get_logger, log_internal_exception
from ai_gateway_core.security import SENSITIVE_KEY_RE as _SENSITIVE_KEY_RE
from ai_gateway_core.security import redact_trace_text as _redact_trace_text_impl

from ..config.startup_fingerprint import (
    StartupConfigSnapshot,
    fingerprinted_runtime_names,
    fingerprinted_secret_names,
)
from .trace_metrics import trace_writer_metrics

logger = get_logger(__name__)

_TRACE_NAMESPACE = uuid.UUID("9502a954-d1f8-49cf-8a7f-57ef18f8a7d6")
_TERMINAL_STATUSES = {"succeeded", "failed", "cancelled", "timeout"}
_MAX_PREVIEW_CHARS = 500
_MAX_PAYLOAD_CHARS = 4000
_MAX_DICT_KEYS = 40
_MAX_LIST_ITEMS = 20
_MAX_LOCATOR_HISTORY_MESSAGES = 8
_MAX_LOCATOR_MESSAGE_CHARS = 260
_MAX_LOCATOR_EXCERPT_CHARS = 2200


def _redact_trace_text(value: Any, *, limit: int = _MAX_PREVIEW_CHARS) -> str:
    return _redact_trace_text_impl(value, limit=limit)


def _trace_uuid(value: str | None) -> str:
    raw = str(value or uuid.uuid4())
    try:
        return str(uuid.UUID(raw))
    except Exception as exc:
        log_internal_exception(
            logger,
            "assistant.trace.uuid_projection_failed",
            exc,
            level=logging.DEBUG,
        )
        return str(uuid.uuid5(_TRACE_NAMESPACE, raw))


def _span_uuid(trace_id: str, key: str) -> str:
    return str(uuid.uuid5(_TRACE_NAMESPACE, f"{trace_id}:{key}"))


def _utc_from_timestamp(value: float | None) -> datetime:
    if value is None:
        return datetime.now(timezone.utc)
    return datetime.fromtimestamp(value, tz=timezone.utc)


def _duration_ms(started_at: float, ended_at: float | None = None) -> int:
    end = ended_at if ended_at is not None else time.time()
    return max(0, int((end - started_at) * 1000))


def _usage_int(usage: dict[str, Any] | None, key: str) -> int:
    if not usage:
        return 0
    value = usage.get(key)
    try:
        return max(0, int(value or 0))
    except (TypeError, ValueError):
        return 0


def _payload_float(data: dict[str, Any], key: str) -> float | None:
    value = data.get(key)
    try:
        return float(value)
    except (TypeError, ValueError):
        return None


def _json_dumps(value: Any) -> str:
    return json.dumps(value, ensure_ascii=False, separators=(",", ":"), default=str)


def _preview_text(value: Any) -> str:
    if isinstance(value, dict | list | tuple):
        return _redact_trace_text(_json_dumps(_sanitize_payload(value)))
    return _redact_trace_text(value)


def _message_role(value: Any) -> str:
    role = value.get("role") if isinstance(value, dict) else getattr(value, "role", None)
    role_text = str(role or "user").strip().lower()
    return role_text if role_text in {"system", "user", "assistant", "tool"} else "user"


def _message_content(value: Any) -> str:
    content = value.get("content") if isinstance(value, dict) else getattr(value, "content", "")
    if isinstance(content, list | tuple | dict):
        return _json_dumps(_sanitize_payload(content))
    return str(content or "")


def build_transcript_locator(
    *,
    session_id: str,
    run_id: str,
    request_id: str,
    message: Any,
    history: list[Any] | None,
) -> dict[str, Any]:
    """Build a bounded, redacted locator for multi-turn transcript search."""

    history_items = list(history or [])
    previous_user_turns = sum(1 for item in history_items if _message_role(item) == "user")
    turn_index = previous_user_turns + 1
    current_preview = _redact_trace_text(message, limit=_MAX_LOCATOR_MESSAGE_CHARS)

    recent_lines: list[str] = []
    for item in history_items[-_MAX_LOCATOR_HISTORY_MESSAGES:]:
        role = _message_role(item)
        content = _redact_trace_text(
            _message_content(item),
            limit=_MAX_LOCATOR_MESSAGE_CHARS,
        )
        if content:
            recent_lines.append(f"{role}: {content}")
    recent_lines.append(f"user: {current_preview}")
    excerpt = _redact_trace_text(
        "\n".join(recent_lines),
        limit=_MAX_LOCATOR_EXCERPT_CHARS,
    )
    fingerprint = hashlib.sha256(excerpt.encode("utf-8")).hexdigest()[:16] if excerpt else ""

    return {
        "locator_version": "assistant-transcript-v1",
        "session_id": session_id,
        "run_id": run_id,
        "request_id": request_id,
        "turn_index": turn_index,
        "turn_id": f"{session_id}:turn:{turn_index}",
        "previous_user_turns": previous_user_turns,
        "history_message_count": len(history_items),
        "message_index": len(history_items) + 1,
        "current_message_preview": current_preview,
        "transcript_excerpt": excerpt,
        "transcript_fingerprint": fingerprint,
        "excerpt_message_count": len(recent_lines),
        "bounded": True,
    }


def _sanitize_payload(value: Any, *, depth: int = 0) -> Any:
    if depth > 4:
        return _redact_trace_text(value, limit=200)
    if value is None or isinstance(value, bool | int | float):
        return value
    if isinstance(value, str):
        return _redact_trace_text(value, limit=_MAX_PAYLOAD_CHARS)
    if isinstance(value, dict):
        clean: dict[str, Any] = {}
        for index, (key, item) in enumerate(value.items()):
            if index >= _MAX_DICT_KEYS:
                clean["__truncated_keys__"] = len(value) - _MAX_DICT_KEYS
                break
            safe_key = str(key)[:120]
            if _SENSITIVE_KEY_RE.search(safe_key):
                clean[safe_key] = "[redacted]"
            else:
                clean[safe_key] = _sanitize_payload(item, depth=depth + 1)
        return clean
    if isinstance(value, list | tuple):
        clean_items = [_sanitize_payload(item, depth=depth + 1) for item in value[:_MAX_LIST_ITEMS]]
        if len(value) > _MAX_LIST_ITEMS:
            clean_items.append({"__truncated_items__": len(value) - _MAX_LIST_ITEMS})
        return clean_items
    return _redact_trace_text(value, limit=1000)


def _validated_startup_config_summary(value: Any) -> dict[str, Any] | None:
    """Project only the closed, non-secret startup-config/v1 trace schema."""

    if not isinstance(value, dict) or value.get("schema_version") != "assistant-startup-config/v1":
        return None
    digest = str(value.get("sha256") or "")
    if not digest.startswith("sha256:") or len(digest) > 96:
        return None

    projected: dict[str, Any] = {
        "schema_version": "assistant-startup-config/v1",
        "sha256": digest,
    }
    raw_settings = value.get("settings")
    if isinstance(raw_settings, dict):
        settings: dict[str, Any] = {}
        for name, item in raw_settings.items():
            if (
                not isinstance(name, str)
                or not name.startswith(("ASSISTANT_", "AGENT_STUDIO_", "DASHSCOPE_", "OPENAI_", "QUIZ_", "DEFAULT_MODEL"))
                or not isinstance(item, dict)
            ):
                continue
            raw_value = item.get("value")
            if isinstance(raw_value, dict):
                raw_value = {
                    "entry_count": max(0, int(raw_value.get("entry_count") or 0)),
                    "structure_sha256": str(raw_value.get("structure_sha256") or "")[:96],
                }
            elif not isinstance(raw_value, bool | int | str):
                continue
            settings[name[:120]] = {
                "value": raw_value,
                "source": str(item.get("source") or "")[:32],
                "parser": str(item.get("parser") or "")[:160],
                "valid": bool(item.get("valid")),
            }
        projected["settings"] = settings

    raw_runtime = value.get("runtime")
    if isinstance(raw_runtime, dict):
        runtime: dict[str, Any] = {}
        for name in sorted(fingerprinted_runtime_names()):
            item = raw_runtime.get(name)
            if not isinstance(item, dict):
                continue
            raw_value = item.get("value")
            if isinstance(raw_value, dict):
                raw_value = {
                    "configured": bool(raw_value.get("configured")),
                    "entry_count": max(0, int(raw_value.get("entry_count") or 0)),
                    "structure_sha256": str(
                        raw_value.get("structure_sha256") or ""
                    )[:96],
                }
            elif not isinstance(raw_value, bool | int | float | str) and raw_value is not None:
                continue
            runtime_item = {
                "value": raw_value,
                "source": str(item.get("source") or "")[:128],
                "parser": str(item.get("parser") or "")[:160],
                "valid": bool(item.get("valid")),
            }
            if item.get("scope") == "test_only":
                runtime_item["scope"] = "test_only"
            runtime[name] = runtime_item
        projected["runtime"] = runtime

    raw_providers = value.get("providers")
    if isinstance(raw_providers, dict):
        providers: dict[str, Any] = {}
        for name in ("openai", "anthropic", "deepseek", "dashscope", "google", "google-vertex"):
            item = raw_providers.get(name)
            if not isinstance(item, dict):
                continue
            provider = {
                "configured": bool(item.get("configured")),
                "credential_source": str(item.get("credential_source") or "unset")[:80],
                "endpoint_source": str(item.get("endpoint_source") or "code_default")[:80],
            }
            raw_endpoint = str(item.get("endpoint") or "")
            try:
                parsed_endpoint = urllib.parse.urlsplit(raw_endpoint)
                hostname = parsed_endpoint.hostname
                port = parsed_endpoint.port
            except ValueError:
                hostname = None
                port = None
            if hostname and parsed_endpoint.scheme:
                host = (
                    f"[{hostname}]"
                    if ":" in hostname and not hostname.startswith("[")
                    else hostname
                )
                netloc = f"{host}:{port}" if port is not None else host
                provider["endpoint"] = urllib.parse.urlunsplit(
                    (parsed_endpoint.scheme, netloc, parsed_endpoint.path, "", "")
                )[:512]
            elif raw_endpoint == "":
                provider["endpoint"] = ""
            else:
                provider["endpoint"] = "<invalid>"
            provider["endpoint_valid"] = bool(item.get("endpoint_valid"))
            for field_name in ("backend", "backend_source", "wire_protocol"):
                if isinstance(item.get(field_name), str):
                    provider[field_name] = str(item[field_name])[:64]
            if "backend_valid" in item:
                provider["backend_valid"] = bool(item.get("backend_valid"))
            providers[name] = provider
        projected["providers"] = providers

    raw_secrets = value.get("secrets")
    if isinstance(raw_secrets, dict):
        secrets: dict[str, Any] = {}
        for name in sorted(fingerprinted_secret_names()):
            item = raw_secrets.get(name)
            if isinstance(item, dict):
                secrets[name] = {"configured": bool(item.get("configured"))}
        projected["secrets"] = secrets

    raw_model = value.get("model")
    if isinstance(raw_model, dict) and isinstance(raw_model.get("default"), dict):
        model_default = raw_model["default"]
        projected["model"] = {
            "default": {
                "value": str(model_default.get("value") or "")[:120],
                "source": str(model_default.get("source") or "")[:32],
            }
        }

    raw_build = value.get("build")
    if isinstance(raw_build, dict):
        build: dict[str, Any] = {}
        for name in ("package_version", "image_version", "vcs_revision", "image_ref"):
            item = raw_build.get(name)
            if isinstance(item, dict):
                build[name] = {
                    "value": str(item.get("value") or "unknown")[:256],
                    "source": str(item.get("source") or "code_default")[:32],
                }
        projected["build"] = build
    return projected


def _bounded_context_retrieved_payload(payload: Any) -> Any:
    if not isinstance(payload, dict):
        return payload
    clean = dict(payload)
    chunks = clean.get("chunks")
    if not isinstance(chunks, list):
        return clean
    bounded_chunks = []
    for chunk in chunks[:12]:
        if not isinstance(chunk, dict):
            bounded_chunks.append(_redact_trace_text(chunk, limit=360))
            continue
        bounded_chunk = {key: value for key, value in chunk.items() if key != "content"}
        bounded_chunk["content_preview"] = _redact_trace_text(chunk.get("content"), limit=360)
        bounded_chunks.append(bounded_chunk)
    if len(chunks) > 12:
        bounded_chunks.append({"__truncated_items__": len(chunks) - 12})
    clean["chunks"] = bounded_chunks
    return clean


def _event_payload_for_storage(event_type: str, payload: Any) -> Any:
    if event_type == "context_retrieved":
        return _bounded_context_retrieved_payload(payload)
    return payload


def _runtime_trajectory_summary(
    *,
    ctx: AssistantTraceContext,
    status: str,
    terminal_envelope: dict[str, Any] | None,
    trace_writer_health: dict[str, int],
    redaction_state: dict[str, Any],
) -> dict[str, Any]:
    envelope = terminal_envelope if isinstance(terminal_envelope, dict) else {}
    context_snapshot = (
        envelope.get("context_snapshot")
        if isinstance(envelope.get("context_snapshot"), dict)
        else {}
    )
    memory = (
        context_snapshot.get("memory") if isinstance(context_snapshot.get("memory"), dict) else {}
    )
    tools = context_snapshot.get("tools") if isinstance(context_snapshot.get("tools"), dict) else {}
    policy = (
        context_snapshot.get("policy") if isinstance(context_snapshot.get("policy"), dict) else {}
    )
    surface = (
        context_snapshot.get("surface") if isinstance(context_snapshot.get("surface"), dict) else {}
    )
    trace_writer_issues = sum(
        int(trace_writer_health.get(key) or 0)
        for key in ("dropped_writes", "failed_writes", "timed_out_writes")
    )
    return _sanitize_payload(
        {
            "schema_version": "assistant-runtime-trajectory/v1",
            "trace_family": "assistant",
            "run_id": ctx.run_id,
            "thread_id": ctx.session_id,
            "session_id": ctx.session_id,
            "request_id": ctx.request_id,
            "status": status,
            "exit_reason": envelope.get("exit_reason") or status,
            "resume_ready": bool(envelope.get("resume_ready")),
            "checkpoint_id": envelope.get("checkpoint_id"),
            "approval_id": envelope.get("approval_id"),
            "context_snapshot_id": envelope.get("context_snapshot_id")
            or context_snapshot.get("snapshot_id"),
            "context_snapshot_hash": context_snapshot.get("snapshot_hash"),
            "memory": {
                "runtime_memory_snippets": memory.get("runtime_memory_snippets"),
                "runtime_memory_provenance_count": memory.get("runtime_memory_provenance_count"),
                "history_message_count": memory.get("history_message_count"),
                "has_session_memory": memory.get("has_session_memory"),
                "has_long_term_memory": memory.get("has_long_term_memory"),
            },
            "tools": {
                "tool_count": tools.get("tool_count"),
                "selected_tool_count": tools.get("selected_tool_count"),
                "prompt_exposed_count": tools.get("prompt_exposed_count"),
            },
            "policy": {
                "execution_profile": policy.get("execution_profile"),
                "memory_mode": policy.get("memory_mode"),
                "runtime_mode": policy.get("runtime_mode"),
                "queue_mode": policy.get("queue_mode"),
                "kb_mode": policy.get("kb_mode"),
                "web_search_enabled": policy.get("web_search_enabled"),
            },
            "surface": {
                "stream": surface.get("stream"),
                "task_id": surface.get("task_id"),
                "resume_run_id": surface.get("resume_run_id"),
                "resume_approval_id": surface.get("resume_approval_id"),
            },
            "transcript_locator": {
                "turn_index": ctx.transcript_locator.get("turn_index"),
                "turn_id": ctx.transcript_locator.get("turn_id"),
                "history_message_count": ctx.transcript_locator.get("history_message_count"),
                "message_index": ctx.transcript_locator.get("message_index"),
                "transcript_fingerprint": ctx.transcript_locator.get("transcript_fingerprint"),
                "bounded": bool(ctx.transcript_locator),
            },
            "trace_writer_health": {
                **trace_writer_health,
                "redacted_writes": 1,
                "queued_writes": trace_writer_health.get("pending_writes", 0),
                "issue_count": trace_writer_issues,
            },
            "redaction_state": redaction_state,
            "privacy": {"payloads": "bounded_redacted_preview"},
        }
    )


@dataclass(frozen=True)
class AssistantTraceContext:
    """Stable trace identity and tenant context for a single assistant run."""

    trace_id: str
    run_id: str
    tenant_id: str
    user_id: str
    session_id: str
    request_id: str
    model_id: str | None = None
    provider: str | None = None
    input_preview: str = ""
    transcript_locator: dict[str, Any] = field(default_factory=dict)
    mode: str = "streaming_first"
    workflow_kind: str = "ai_assistant_chat"
    started_at: float = field(default_factory=time.time)
    otel_trace_id: str | None = None
    traceparent: str | None = None
    agent_id: str | None = None
    agent_version_id: str | None = None
    agent_draft_revision: int | None = None
    publication_id: str | None = None
    channel: str | None = None
    runtime_fingerprint: str | None = None
    agent_spec_hash: str | None = None

    @classmethod
    def from_agent_context(cls, ctx: Any) -> AssistantTraceContext:
        config = getattr(ctx, "config", None)
        runtime = getattr(config, "agent_runtime", None)
        dimensions = runtime.trace_dimensions() if runtime is not None else {}
        run_id = str(getattr(ctx, "run_id", "") or uuid.uuid4())
        traceparent = str(getattr(ctx, "traceparent", "") or "") or None
        otel_trace_id = str(getattr(ctx, "otel_trace_id", "") or "") or None
        if not otel_trace_id and traceparent and traceparent.startswith("00-"):
            parts = traceparent.split("-")
            if len(parts) >= 2 and parts[1]:
                otel_trace_id = parts[1]
        return cls(
            trace_id=_trace_uuid(run_id),
            run_id=run_id,
            tenant_id=str(getattr(ctx, "tenant_id", "")),
            user_id=str(getattr(ctx, "user_id", "")),
            session_id=str(getattr(ctx, "session_id", "")),
            request_id=str(getattr(ctx, "request_id", "") or uuid.uuid4()),
            model_id=str(getattr(config, "model_id", "") or "") or None,
            provider=str(getattr(config, "model_provider", "") or "") or None,
            input_preview=_redact_trace_text(getattr(ctx, "message", "")),
            transcript_locator=dict(getattr(ctx, "transcript_locator", {}) or {}),
            mode="streaming_first",
            started_at=float(getattr(ctx, "trace_started_at", time.time())),
            otel_trace_id=otel_trace_id,
            traceparent=traceparent,
            agent_id=dimensions.get("agent_id"),
            agent_version_id=dimensions.get("agent_version_id"),
            agent_draft_revision=dimensions.get("agent_draft_revision"),
            publication_id=dimensions.get("publication_id"),
            channel=dimensions.get("channel"),
            runtime_fingerprint=dimensions.get("runtime_fingerprint"),
            agent_spec_hash=dimensions.get("agent_spec_hash"),
        )

    @classmethod
    def from_chat_request(
        cls,
        *,
        run_id: str,
        request_id: str,
        tenant_id: str,
        user_id: str,
        session_id: str,
        message: str,
        model_id: str | None,
        provider: str | None,
        started_at: float,
        transcript_locator: dict[str, Any] | None = None,
        traceparent: str | None = None,
        otel_trace_id: str | None = None,
        agent_runtime: Any | None = None,
    ) -> AssistantTraceContext:
        resolved_traceparent = str(traceparent or "") or None
        resolved_otel_trace_id = str(otel_trace_id or "") or None
        if (
            not resolved_otel_trace_id
            and resolved_traceparent
            and resolved_traceparent.startswith("00-")
        ):
            parts = resolved_traceparent.split("-")
            if len(parts) >= 2 and parts[1]:
                resolved_otel_trace_id = parts[1]
        dimensions = agent_runtime.trace_dimensions() if agent_runtime is not None else {}
        return cls(
            trace_id=_trace_uuid(run_id),
            run_id=run_id,
            tenant_id=tenant_id,
            user_id=user_id,
            session_id=session_id,
            request_id=request_id,
            model_id=model_id,
            provider=provider,
            input_preview=_redact_trace_text(message),
            transcript_locator=dict(transcript_locator or {}),
            mode="non_stream",
            started_at=started_at,
            traceparent=resolved_traceparent,
            otel_trace_id=resolved_otel_trace_id,
            agent_id=dimensions.get("agent_id"),
            agent_version_id=dimensions.get("agent_version_id"),
            agent_draft_revision=dimensions.get("agent_draft_revision"),
            publication_id=dimensions.get("publication_id"),
            channel=dimensions.get("channel"),
            runtime_fingerprint=dimensions.get("runtime_fingerprint"),
            agent_spec_hash=dimensions.get("agent_spec_hash"),
        )


class AssistantTraceWriter:
    """Bounded background writer for ATE-01 assistant trace tables."""

    def __init__(
        self,
        database: Any | None,
        *,
        max_pending: int = 256,
        write_timeout_s: float = 1.0,
        startup_config: StartupConfigSnapshot | None = None,
    ) -> None:
        self.database = database
        self.max_pending = max_pending
        self.write_timeout_s = write_timeout_s
        # Defense in depth: accept only the closed startup-config/v1 projection.
        # Unknown keys are dropped even when a future caller accidentally passes
        # a Settings/model dump containing credentials.
        self.startup_config_summary = (
            _validated_startup_config_summary(startup_config.safe_summary())
            if isinstance(startup_config, StartupConfigSnapshot)
            else None
        )
        self.startup_config_fingerprint = str(
            (self.startup_config_summary or {}).get("sha256") or ""
        )
        self._pending: set[asyncio.Task[str]] = set()
        self._submission_generation = 0
        self._pending_submissions: dict[asyncio.Task[str], tuple[int, str]] = {}
        self._submission_coroutines: dict[
            asyncio.Task[str],
            Coroutine[Any, Any, None],
        ] = {}
        self._failed_outcomes: dict[str, tuple[int, str]] = {}
        self._initialized_traces: set[str] = set()
        self._trace_init_locks: dict[str, asyncio.Lock] = {}
        # SPO-03 / A3: per-run event batching. Events accumulate per trace
        # until 25 pending, 50 ms, or finish/drain, then persist in one
        # executemany statement instead of one INSERT per event.
        self._event_buffers: dict[str, list[tuple[str, int, Any, str | None, float | None]]] = {}
        self._event_contexts: dict[str, AssistantTraceContext] = {}
        self._flush_timers: dict[str, asyncio.Task[None]] = {}
        # Traces whose finish submission is queued: the finish task flushes
        # its own buffer inline, so drain must not flush it again (that race
        # re-inserted the root/lifecycle pair).
        self._pending_finishes: set[str] = set()
        self.dropped_writes = 0
        self.failed_writes = 0
        self.timed_out_writes = 0

    _BATCH_MAX_EVENTS = 25
    _BATCH_FLUSH_DELAY_S = 0.05

    @property
    def pending_count(self) -> int:
        return len(self._pending)

    def telemetry_snapshot(self) -> dict[str, int]:
        return {
            "pending_writes": self.pending_count,
            "dropped_writes": self.dropped_writes,
            "failed_writes": self.failed_writes,
            "timed_out_writes": self.timed_out_writes,
        }

    def start_trace(self, ctx: AssistantTraceContext) -> bool:
        return self._submit(self._start_trace(ctx), trace_id=ctx.trace_id)

    def record_event(
        self,
        *,
        ctx: AssistantTraceContext,
        event_type: str,
        sequence_no: int,
        payload: Any,
        phase: str | None = None,
        occurred_at: float | None = None,
    ) -> bool:
        if self.database is None or not hasattr(self.database, "execute"):
            return self._drop_write(trace_id=ctx.trace_id)
        if self.max_pending <= 0:
            return self._drop_write(trace_id=ctx.trace_id)
        try:
            asyncio.get_running_loop()
        except RuntimeError:
            return self._drop_write(trace_id=ctx.trace_id)

        buffer = self._event_buffers.setdefault(ctx.trace_id, [])
        buffer.append((event_type, sequence_no, payload, phase, occurred_at))
        self._event_contexts.setdefault(ctx.trace_id, ctx)
        if len(buffer) >= self._BATCH_MAX_EVENTS:
            self._submit_flush(ctx.trace_id)
        elif len(buffer) == 1:
            self._arm_flush_timer(ctx.trace_id)
        return True

    def _drop_write(self, *, trace_id: str) -> bool:
        """Mirror ``_submit``'s drop accounting for synchronous rejections."""
        self._submission_generation += 1
        self.dropped_writes += 1
        self._record_failed_outcome(
            trace_id=trace_id,
            generation=self._submission_generation,
            outcome="dropped",
        )
        return False

    def _arm_flush_timer(self, trace_id: str) -> None:
        existing = self._flush_timers.get(trace_id)
        if existing is not None and not existing.done():
            return

        loop = asyncio.get_running_loop()

        async def _delayed_flush() -> None:
            try:
                await asyncio.sleep(self._BATCH_FLUSH_DELAY_S)
            except asyncio.CancelledError:
                return
            self._submit_flush(trace_id)

        timer = loop.create_task(_delayed_flush())
        self._flush_timers[trace_id] = timer

    def _submit_flush(self, trace_id: str) -> None:
        buffer = self._event_buffers.get(trace_id)
        if not buffer:
            return
        events = list(buffer[: self._BATCH_MAX_EVENTS])
        del buffer[: self._BATCH_MAX_EVENTS]
        ctx = self._event_contexts.get(trace_id)
        self._flush_timers.pop(trace_id, None)
        if ctx is not None:
            self._submit(
                self._flush_events(ctx=ctx, events=events),
                trace_id=trace_id,
            )
        if buffer:
            # More than one batch worth accumulated; drain the remainder
            # immediately so sequence order persists without a long tail.
            self._submit_flush(trace_id)

    def _submit_pending_flush(self, trace_id: str) -> bool:
        """Pop the pending buffer and submit it through the bounded writer."""
        if trace_id in self._pending_finishes:
            # The queued finish task flushes its own buffer inline; flushing
            # here would race it and re-insert the root/lifecycle pair.
            return False
        timer = self._flush_timers.pop(trace_id, None)
        if timer is not None and not timer.done():
            timer.cancel()
        buffer = self._event_buffers.pop(trace_id, None)
        if not buffer:
            return False
        ctx = self._event_contexts.pop(trace_id, None)
        if ctx is None:
            return False
        return self._submit(
            self._flush_events(ctx=ctx, events=list(buffer)),
            trace_id=trace_id,
        )

    async def _flush_pending_events(self, trace_id: str) -> None:
        """Inline flush for ``finish_trace`` (already inside a bounded submission)."""
        timer = self._flush_timers.pop(trace_id, None)
        if timer is not None and not timer.done():
            timer.cancel()
        buffer = self._event_buffers.pop(trace_id, None)
        if not buffer:
            return
        ctx = self._event_contexts.pop(trace_id, None)
        if ctx is not None:
            await self._flush_events(ctx=ctx, events=list(buffer))

    def record_span(
        self,
        *,
        ctx: AssistantTraceContext,
        span_key: str,
        span_kind: str,
        name: str,
        status: str,
        sequence_no: int,
        started_at: float,
        ended_at: float | None = None,
        input_preview: Any = "",
        output_preview: Any = "",
        attributes: dict[str, Any] | None = None,
        error_message: Any = None,
    ) -> bool:
        return self._submit(
            self._record_span(
                ctx=ctx,
                span_key=span_key,
                span_kind=span_kind,
                name=name,
                status=status,
                sequence_no=sequence_no,
                started_at=started_at,
                ended_at=ended_at,
                input_preview=input_preview,
                output_preview=output_preview,
                attributes=attributes or {},
                error_message=error_message,
            ),
            trace_id=ctx.trace_id,
        )

    def finish_trace(
        self,
        *,
        ctx: AssistantTraceContext,
        status: str,
        output_preview: Any = "",
        usage: dict[str, Any] | None = None,
        error: Any = None,
        total_latency_ms: int | None = None,
        terminal_event_type: str | None = None,
        terminal_sequence_no: int | None = None,
        terminal_envelope: dict[str, Any] | None = None,
    ) -> bool:
        self._pending_finishes.add(ctx.trace_id)
        accepted = self._submit(
            self._finish_trace(
                ctx=ctx,
                status=status,
                output_preview=output_preview,
                usage=usage,
                error=error,
                total_latency_ms=total_latency_ms,
                terminal_event_type=terminal_event_type,
                terminal_sequence_no=terminal_sequence_no,
                terminal_envelope=terminal_envelope,
            ),
            trace_id=ctx.trace_id,
        )
        if not accepted:
            self._pending_finishes.discard(ctx.trace_id)
        return accepted

    async def drain(
        self,
        *,
        timeout_s: float = 1.0,
        strict: bool = False,
        trace_id: str | None = None,
    ) -> None:
        # Persist any still-buffered events before waiting on the submission
        # barrier, so a drain never reports done while deltas are in memory.
        # The flush goes through the bounded submission machinery (write
        # timeout + generation barrier) rather than blocking the caller.
        if trace_id is not None:
            self._submit_pending_flush(trace_id)
        else:
            for buffered_trace_id in list(self._event_buffers):
                self._submit_pending_flush(buffered_trace_id)

        def latest_relevant_generation() -> int:
            if trace_id is None:
                return self._submission_generation
            generations = [
                generation
                for generation, pending_trace_id in self._pending_submissions.values()
                if pending_trace_id == trace_id
            ]
            failed = self._failed_outcomes.get(trace_id)
            if failed is not None:
                generations.append(failed[0])
            return max(generations, default=0)

        loop = asyncio.get_running_loop()
        deadline = loop.time() + max(0.0, timeout_s)
        barrier_generation = latest_relevant_generation()
        done: set[asyncio.Task[str]] = set()
        pending: set[asyncio.Task[str]] = set()
        while True:
            pending_snapshot = tuple(
                task
                for task, (generation, pending_trace_id) in self._pending_submissions.items()
                if generation <= barrier_generation
                and (trace_id is None or pending_trace_id == trace_id)
            )
            if pending_snapshot:
                remaining = max(0.0, deadline - loop.time())
                done, pending = await asyncio.wait(pending_snapshot, timeout=remaining)
                for task in done:
                    self._finalize_task_outcome(task)
                if pending:
                    break
            # A completing producer can synchronously submit its terminal trace
            # write from a done callback.  Advance the barrier to a fixed point
            # so resume/finish cannot observe a stale sequence after ``drain``.
            await asyncio.sleep(0)
            latest_generation = latest_relevant_generation()
            if latest_generation <= barrier_generation:
                break
            barrier_generation = latest_generation
            if loop.time() >= deadline:
                pending = {
                    task
                    for task, (generation, pending_trace_id) in self._pending_submissions.items()
                    if generation <= barrier_generation
                    and (trace_id is None or pending_trace_id == trace_id)
                }
                break
        if not strict:
            return
        if pending:
            raise TimeoutError("assistant trace persistence barrier timed out")
        barrier_outcomes = [
            outcome
            for outcome_trace_id, (generation, outcome) in self._failed_outcomes.items()
            if generation <= barrier_generation
            and (trace_id is None or outcome_trace_id == trace_id)
        ]
        if "timed_out" in barrier_outcomes:
            raise TimeoutError("assistant trace persistence barrier timed out")
        if barrier_outcomes:
            raise RuntimeError("assistant trace persistence barrier failed")

    async def resume_sequence(self, ctx: AssistantTraceContext) -> int:
        if self.database is None:
            return 0
        await self.drain(
            timeout_s=self.write_timeout_s,
            strict=True,
            trace_id=ctx.trace_id,
        )
        if not hasattr(self.database, "fetchrow"):
            raise RuntimeError("trace resume sequence lookup is unavailable")
        row = await self._trace_fetchrow(
            """
            SELECT COALESCE(MAX(sequence_no), 0)::int AS max_sequence_no
            FROM (
                SELECT sequence_no
                FROM agent_trace_events
                WHERE trace_id = $1
                UNION ALL
                SELECT sequence_no
                FROM agent_trace_spans
                WHERE trace_id = $1
            ) persisted_sequences;
            """,
            ctx.trace_id,
        )
        if not row:
            return 0
        try:
            return max(0, int(row.get("max_sequence_no") or 0))
        except (AttributeError, TypeError, ValueError) as exc:
            raise RuntimeError("trace resume sequence lookup returned invalid data") from exc

    def _submit(self, coro: Coroutine[Any, Any, None], *, trace_id: str) -> bool:
        self._submission_generation += 1
        generation = self._submission_generation
        if self.database is None:
            self._close_coro(coro)
            return False
        if not hasattr(self.database, "execute"):
            self._record_failed_outcome(
                trace_id=trace_id,
                generation=generation,
                outcome="rejected",
            )
            self._close_coro(coro)
            return False
        if self.max_pending <= 0 or len(self._pending) >= self.max_pending:
            self.dropped_writes += 1
            self._record_failed_outcome(
                trace_id=trace_id,
                generation=generation,
                outcome="dropped",
            )
            self._close_coro(coro)
            return False
        try:
            loop = asyncio.get_running_loop()
        except RuntimeError:
            self.dropped_writes += 1
            self._record_failed_outcome(
                trace_id=trace_id,
                generation=generation,
                outcome="dropped",
            )
            self._close_coro(coro)
            return False

        task = loop.create_task(self._run(coro))
        self._pending.add(task)
        self._pending_submissions[task] = (generation, trace_id)
        self._submission_coroutines[task] = coro

        def _done(done_task: asyncio.Task[str]) -> None:
            self._finalize_task_outcome(done_task)

        task.add_done_callback(_done)
        return True

    def _finalize_task_outcome(self, task: asyncio.Task[str]) -> None:
        submission = self._pending_submissions.pop(task, None)
        submitted_coro = self._submission_coroutines.pop(task, None)
        self._pending.discard(task)
        if submitted_coro is not None:
            self._close_coro(submitted_coro)
        if submission is None:
            return
        generation, trace_id = submission
        if task.cancelled():
            outcome = "cancelled"
        else:
            try:
                outcome = task.result()
            except Exception as exc:  # noqa: BLE001 - persist only a generic failed outcome.
                log_internal_exception(
                    logger,
                    "assistant.trace.background_task_failed",
                    exc,
                )
                outcome = "failed"
        if outcome != "succeeded":
            self._record_failed_outcome(
                trace_id=trace_id,
                generation=generation,
                outcome=outcome,
            )

    def _record_failed_outcome(
        self,
        *,
        trace_id: str,
        generation: int,
        outcome: str,
    ) -> None:
        previous = self._failed_outcomes.get(trace_id)
        if previous is None or generation < previous[0]:
            self._failed_outcomes[trace_id] = (generation, outcome)

    async def _run(self, coro: Coroutine[Any, Any, None]) -> str:
        try:
            await asyncio.wait_for(coro, timeout=self.write_timeout_s)
            return "succeeded"
        except TimeoutError:
            self.timed_out_writes += 1
            logger.warning("Assistant trace write timed out after %.2fs", self.write_timeout_s)
            return "timed_out"
        except Exception as exc:  # noqa: BLE001 - trace writes are best-effort.
            self.failed_writes += 1
            log_internal_exception(
                logger,
                "assistant.trace.write_failed",
                exc,
                level=logging.WARNING,
            )
            return "failed"

    def _close_coro(self, coro: Coroutine[Any, Any, None]) -> None:
        try:
            coro.close()
        except Exception as exc:
            log_internal_exception(
                logger,
                "assistant.trace.coroutine_close_failed",
                exc,
                level=logging.WARNING,
            )

    async def _trace_execute(self, sql: str, *args: Any) -> Any:
        trace_writer_metrics.sql_statements += 1
        return await self.database.execute(sql, *args)

    async def _trace_executemany(self, sql: str, rows: list[tuple[Any, ...]]) -> Any:
        if not rows:
            return None
        if hasattr(self.database, "executemany"):
            trace_writer_metrics.sql_statements += 1
            return await self.database.executemany(sql, rows)
        # Adapter fallback: one statement per row, still inside one flush task.
        for row in rows:
            trace_writer_metrics.sql_statements += 1
            await self.database.execute(sql, *row)
        return None

    async def _trace_fetchrow(self, sql: str, *args: Any) -> Any:
        trace_writer_metrics.sql_statements += 1
        return await self.database.fetchrow(sql, *args)

    async def _start_trace(self, ctx: AssistantTraceContext) -> None:
        await self._ensure_trace_started(ctx)

    async def _ensure_trace_started(self, ctx: AssistantTraceContext) -> None:
        """Persist the immutable root/lifecycle pair once per active trace.

        ``record_event`` tasks are intentionally concurrent.  The per-trace
        lock prevents each task from paying two defensive upserts while still
        supporting callers that record an event without an explicit
        ``start_trace`` submission.
        """
        if ctx.trace_id in self._initialized_traces:
            return
        lock = self._trace_init_locks.setdefault(ctx.trace_id, asyncio.Lock())
        async with lock:
            if ctx.trace_id in self._initialized_traces:
                return
            await self._upsert_trace_root(ctx)
            await self._upsert_lifecycle_span(ctx, status="running")
            self._initialized_traces.add(ctx.trace_id)

    async def _flush_events(
        self,
        *,
        ctx: AssistantTraceContext,
        events: list[tuple[str, int, Any, str | None, float | None]],
    ) -> None:
        """Persist one buffered batch of trace events (SPO-03 / A3).

        Root/lifecycle are ensured once for the batch, span-producing events
        still upsert their spans inline, and all event rows go out as a
        single ``executemany`` statement.
        """
        if not events:
            return
        await self._ensure_trace_started(ctx)
        rows: list[tuple[Any, ...]] = []
        ttft_ms: int | None = None
        for event_type, sequence_no, payload, phase, occurred_at in events:
            span_id = await self._record_span_for_event(
                ctx=ctx,
                event_type=event_type,
                sequence_no=sequence_no,
                payload=payload,
                occurred_at=occurred_at,
            )
            sanitized_payload = {
                "phase": phase,
                "data": _sanitize_payload(_event_payload_for_storage(event_type, payload)),
            }
            if ctx.transcript_locator:
                sanitized_payload["locator"] = _sanitize_payload(
                    {
                        "turn_index": ctx.transcript_locator.get("turn_index"),
                        "turn_id": ctx.transcript_locator.get("turn_id"),
                        "request_id": ctx.request_id,
                        "run_id": ctx.run_id,
                        "session_id": ctx.session_id,
                    }
                )
            payload_json = _json_dumps(sanitized_payload)
            rows.append(
                (
                    ctx.trace_id,
                    span_id,
                    event_type,
                    sequence_no,
                    _utc_from_timestamp(occurred_at),
                    payload_json,
                    len(payload_json.encode("utf-8")),
                )
            )
            if event_type == "ttft" and isinstance(payload, dict):
                ttft_ms = max(ttft_ms or 0, int(float(payload.get("ttft_ms") or 0)))
        await self._trace_executemany(
            """
            INSERT INTO agent_trace_events (
                trace_id, span_id, event_type, sequence_no, occurred_at,
                payload, payload_size_bytes, redacted
            ) VALUES ($1, $2, $3, $4, $5, $6::jsonb, $7, TRUE)
            ON CONFLICT (trace_id, sequence_no)
            DO UPDATE SET
                event_type = EXCLUDED.event_type,
                span_id = EXCLUDED.span_id,
                payload = EXCLUDED.payload,
                payload_size_bytes = EXCLUDED.payload_size_bytes,
                redacted = TRUE;
            """,
            rows,
        )
        if ttft_ms is not None:
            await self._trace_execute(
                """
                UPDATE agent_traces
                SET first_token_latency_ms = GREATEST(first_token_latency_ms, $2),
                    updated_at = NOW()
                WHERE trace_id = $1;
                """,
                ctx.trace_id,
                max(0, ttft_ms),
            )

    async def _record_span(
        self,
        *,
        ctx: AssistantTraceContext,
        span_key: str,
        span_kind: str,
        name: str,
        status: str,
        sequence_no: int,
        started_at: float,
        ended_at: float | None,
        input_preview: Any,
        output_preview: Any,
        attributes: dict[str, Any],
        error_message: Any,
    ) -> None:
        await self._ensure_trace_started(ctx)
        await self._upsert_span(
            span_id=_span_uuid(ctx.trace_id, span_key),
            trace_id=ctx.trace_id,
            span_kind=span_kind,
            name=name,
            status=status,
            sequence_no=sequence_no,
            started_at=started_at,
            ended_at=ended_at,
            input_preview=input_preview,
            output_preview=output_preview,
            attributes=attributes,
            error_message=error_message,
            parent_span_id=self._lifecycle_parent_id(ctx.trace_id),
        )

    async def _finish_trace(
        self,
        *,
        ctx: AssistantTraceContext,
        status: str,
        output_preview: Any,
        usage: dict[str, Any] | None,
        error: Any,
        total_latency_ms: int | None,
        terminal_event_type: str | None,
        terminal_sequence_no: int | None,
        terminal_envelope: dict[str, Any] | None,
    ) -> None:
        try:
            await self._finish_trace_impl(
                ctx=ctx,
                status=status,
                output_preview=output_preview,
                usage=usage,
                error=error,
                total_latency_ms=total_latency_ms,
                terminal_event_type=terminal_event_type,
                terminal_sequence_no=terminal_sequence_no,
                terminal_envelope=terminal_envelope,
            )
        finally:
            self._pending_finishes.discard(ctx.trace_id)

    async def _finish_trace_impl(
        self,
        *,
        ctx: AssistantTraceContext,
        status: str,
        output_preview: Any,
        usage: dict[str, Any] | None,
        error: Any,
        total_latency_ms: int | None,
        terminal_event_type: str | None,
        terminal_sequence_no: int | None,
        terminal_envelope: dict[str, Any] | None,
    ) -> None:
        ended_at = time.time()
        # Persist every buffered event before the terminal update so the run
        # is never marked finished while delta events are still in memory.
        await self._flush_pending_events(ctx.trace_id)
        await self._ensure_trace_started(ctx)
        total = (
            total_latency_ms
            if total_latency_ms is not None
            else _duration_ms(ctx.started_at, ended_at)
        )
        input_tokens = _usage_int(usage, "input_tokens")
        output_tokens = _usage_int(usage, "output_tokens")
        total_tokens = _usage_int(usage, "total_tokens") or input_tokens + output_tokens
        total_cost_cents = 0
        pricing_status = "unknown"
        if ctx.model_id:
            total_cost_cents, pricing_status = calculate_token_cost_cents(
                ctx.model_id,
                input_tokens,
                output_tokens,
            )
        trace_writer_health = self.telemetry_snapshot()
        redaction_state = {
            "input_preview": "redacted_truncated",
            "output_preview": "redacted_truncated",
            "payloads": "redacted_truncated",
        }
        metadata = {
            "mode": ctx.mode,
            "trace_writer": "assistant-service",
            **trace_writer_health,
            "pricing_status": pricing_status,
            "runtime_trajectory": _runtime_trajectory_summary(
                ctx=ctx,
                status=status,
                terminal_envelope=terminal_envelope,
                trace_writer_health=trace_writer_health,
                redaction_state=redaction_state,
            ),
        }
        if self.startup_config_summary:
            metadata["startup_config_fingerprint"] = self.startup_config_fingerprint
            metadata["startup_config"] = self.startup_config_summary
        if ctx.transcript_locator:
            metadata["transcript_locator"] = _sanitize_payload(ctx.transcript_locator)
        if terminal_envelope:
            metadata["terminal_envelope"] = _sanitize_payload(terminal_envelope)
        await self._trace_execute(
            """
            UPDATE agent_traces
            SET status = $2,
                ended_at = $3,
                total_latency_ms = GREATEST(total_latency_ms, $4),
                input_tokens = GREATEST(input_tokens, $5),
                output_tokens = GREATEST(output_tokens, $6),
                total_tokens = GREATEST(total_tokens, $7),
                total_cost_cents = GREATEST(total_cost_cents, $8),
                output_preview = $9,
                metadata = metadata || $10::jsonb,
                updated_at = NOW()
            WHERE trace_id = $1;
            """,
            ctx.trace_id,
            status,
            _utc_from_timestamp(ended_at),
            max(0, int(total)),
            input_tokens,
            output_tokens,
            total_tokens,
            max(0, int(total_cost_cents)),
            _preview_text(output_preview),
            _json_dumps(metadata),
        )
        await self._upsert_lifecycle_span(
            ctx,
            status=status,
            ended_at=ended_at,
            error_message=error,
            output_preview=output_preview,
        )
        if terminal_event_type and terminal_sequence_no is not None:
            await self._flush_events(
                ctx=ctx,
                events=[
                    (
                        terminal_event_type,
                        terminal_sequence_no,
                        {
                            "run_id": ctx.run_id,
                            "thread_id": ctx.session_id,
                            "status": status,
                            "error": _redact_trace_text(error) if error else None,
                            "usage": usage or {},
                            "terminal_envelope": _sanitize_payload(terminal_envelope)
                            if terminal_envelope
                            else None,
                        },
                        "generation_storage",
                        ended_at,
                    )
                ],
            )
        await self._enqueue_trace_ingested(ctx, status=status)
        self._initialized_traces.discard(ctx.trace_id)
        lock = self._trace_init_locks.get(ctx.trace_id)
        if lock is not None and not lock.locked():
            self._trace_init_locks.pop(ctx.trace_id, None)

    async def _enqueue_trace_ingested(self, ctx: AssistantTraceContext, *, status: str) -> None:
        if not self.database or not hasattr(self.database, "execute"):
            return
        payload = _json_dumps(
            {
                "trace_id": ctx.trace_id,
                "trace_family": "assistant",
                "status": status,
                "source_adapter": "assistant-service",
            }
        )
        try:
            await self._trace_execute(
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
                """,
                ctx.tenant_id,
                payload,
                ctx.trace_id,
            )
        except Exception as exc:
            log_internal_exception(
                logger,
                "assistant.trace.outbox_enqueue_failed",
                exc,
                level=logging.DEBUG,
            )

    async def _upsert_trace_root(self, ctx: AssistantTraceContext) -> None:
        redaction_state = {
            "input_preview": "redacted_truncated",
            "payloads": "redacted_truncated",
        }
        metadata = {
            "mode": ctx.mode,
            "trace_writer": "assistant-service",
            "schema_version": "ate-02",
        }
        if self.startup_config_summary:
            metadata["startup_config_fingerprint"] = self.startup_config_fingerprint
            metadata["startup_config"] = self.startup_config_summary
        if ctx.transcript_locator:
            metadata["transcript_locator"] = _sanitize_payload(ctx.transcript_locator)
        await self._trace_execute(
            """
            INSERT INTO agent_traces (
                trace_id, trace_family, workflow_kind, tenant_id, user_id,
                session_id, run_id, request_id, otel_trace_id, traceparent,
                model_id, provider, agent_id, agent_version_id,
                agent_draft_revision, publication_id, channel,
                runtime_fingerprint, agent_spec_hash,
                status, started_at, input_preview, redaction_state, metadata
            ) VALUES (
                $1, 'assistant', $2, $3, $4,
                $5, $6, $7, $8, $9,
                $10, $11, $12, $13, $14, $15, $16, $17, $18,
                'running', $19, $20, $21::jsonb, $22::jsonb
            )
            ON CONFLICT (trace_id)
            DO UPDATE SET
                tenant_id = EXCLUDED.tenant_id,
                user_id = EXCLUDED.user_id,
                session_id = EXCLUDED.session_id,
                run_id = EXCLUDED.run_id,
                request_id = EXCLUDED.request_id,
                otel_trace_id = COALESCE(EXCLUDED.otel_trace_id, agent_traces.otel_trace_id),
                traceparent = COALESCE(EXCLUDED.traceparent, agent_traces.traceparent),
                model_id = COALESCE(EXCLUDED.model_id, agent_traces.model_id),
                provider = COALESCE(EXCLUDED.provider, agent_traces.provider),
                status = CASE
                    WHEN agent_traces.status IN ('succeeded', 'failed', 'cancelled', 'timeout')
                    THEN agent_traces.status
                    ELSE EXCLUDED.status
                END,
                input_preview = CASE
                    WHEN agent_traces.input_preview = '' THEN EXCLUDED.input_preview
                    ELSE agent_traces.input_preview
                END,
                redaction_state = agent_traces.redaction_state || EXCLUDED.redaction_state,
                metadata = agent_traces.metadata || EXCLUDED.metadata,
                updated_at = NOW()
            WHERE agent_traces.tenant_id = EXCLUDED.tenant_id
              AND agent_traces.user_id = EXCLUDED.user_id
              AND agent_traces.agent_id IS NOT DISTINCT FROM EXCLUDED.agent_id
              AND agent_traces.agent_version_id IS NOT DISTINCT FROM EXCLUDED.agent_version_id
              AND agent_traces.agent_draft_revision IS NOT DISTINCT FROM EXCLUDED.agent_draft_revision
              AND agent_traces.publication_id IS NOT DISTINCT FROM EXCLUDED.publication_id
              AND agent_traces.channel IS NOT DISTINCT FROM EXCLUDED.channel
              AND agent_traces.runtime_fingerprint IS NOT DISTINCT FROM EXCLUDED.runtime_fingerprint
              AND agent_traces.agent_spec_hash IS NOT DISTINCT FROM EXCLUDED.agent_spec_hash;
            """,
            ctx.trace_id,
            ctx.workflow_kind,
            ctx.tenant_id,
            ctx.user_id,
            ctx.session_id,
            ctx.run_id,
            ctx.request_id,
            ctx.otel_trace_id,
            ctx.traceparent,
            ctx.model_id,
            ctx.provider,
            ctx.agent_id,
            ctx.agent_version_id,
            ctx.agent_draft_revision,
            ctx.publication_id,
            ctx.channel,
            ctx.runtime_fingerprint,
            ctx.agent_spec_hash,
            _utc_from_timestamp(ctx.started_at),
            ctx.input_preview,
            _json_dumps(redaction_state),
            _json_dumps(metadata),
        )

    def _lifecycle_parent_id(self, trace_id: str) -> str:
        return _span_uuid(trace_id, "lifecycle")

    async def _upsert_lifecycle_span(
        self,
        ctx: AssistantTraceContext,
        *,
        status: str,
        ended_at: float | None = None,
        error_message: Any = None,
        output_preview: Any = "",
    ) -> None:
        await self._upsert_span(
            span_id=_span_uuid(ctx.trace_id, "lifecycle"),
            trace_id=ctx.trace_id,
            span_kind="lifecycle",
            name="assistant_run",
            status=status,
            sequence_no=0,
            started_at=ctx.started_at,
            ended_at=ended_at,
            input_preview=ctx.input_preview,
            output_preview=output_preview,
            attributes={
                "mode": ctx.mode,
                "request_id": ctx.request_id,
                "transcript_locator": ctx.transcript_locator,
            },
            error_message=error_message,
        )

    async def _record_span_for_event(
        self,
        *,
        ctx: AssistantTraceContext,
        event_type: str,
        sequence_no: int,
        payload: Any,
        occurred_at: float | None,
    ) -> str | None:
        data = payload if isinstance(payload, dict) else {}
        now = occurred_at or time.time()
        if event_type in {"run_started", "run_finished", "run_error"}:
            status = (
                "failed"
                if event_type == "run_error"
                else "succeeded"
                if event_type == "run_finished"
                else "running"
            )
            await self._upsert_lifecycle_span(
                ctx,
                status=status,
                ended_at=now if status in _TERMINAL_STATUSES else None,
                error_message=data.get("error") or data.get("message"),
            )
            return _span_uuid(ctx.trace_id, "lifecycle")
        if event_type in {
            "rag_retrieval_started",
            "rag_retrieval_completed",
            "rag_retrieval_failed",
        }:
            status = (
                "failed"
                if event_type == "rag_retrieval_failed"
                else "succeeded"
                if event_type == "rag_retrieval_completed"
                else "running"
            )
            started_at = _payload_float(data, "started_at") or ctx.started_at
            ended_at = None if status == "running" else (_payload_float(data, "ended_at") or now)
            query = (
                data.get("gen_ai.retrieval.query.text") or data.get("query") or ctx.input_preview
            )
            dataset_ids = data.get("retrieval.dataset_ids") or data.get("dataset_ids") or []
            document_count = data.get("retrieval.document_count", data.get("document_count", 0))
            attributes = {
                "source_adapter": data.get("source_adapter") or "assistant_service.rag",
                "openinference.span.kind": data.get("openinference.span.kind") or "RETRIEVER",
                "gen_ai.operation.name": "retrieve",
                "gen_ai.retrieval.query.text": query,
                "retrieval.dataset_ids": dataset_ids,
                "retrieval.dataset_count": data.get("dataset_count"),
                "retrieval.document_count": document_count,
                "retrieval.documents": data.get("retrieval.documents") or [],
                "retrieval.top_k": data.get("top_k"),
                "retrieval.score_threshold": data.get("score_threshold"),
                "retrieval.include_images": data.get("include_images"),
                "retrieval.context_count": data.get("context_count"),
                "retrieval.top_score": data.get("retrieval.top_score"),
                "retrieval.avg_score": data.get("retrieval.avg_score"),
                "duration_ms": data.get("duration_ms"),
                "privacy": data.get("privacy") or {"payloads": "bounded_redacted_preview"},
            }
            retrieval_key = str(
                data.get("retrieval_id")
                or data.get("tool_id")
                or data.get("tool_call_id")
                or sequence_no
            )
            span_id = _span_uuid(ctx.trace_id, f"rag_retrieval:{retrieval_key}")
            await self._upsert_span(
                span_id=span_id,
                trace_id=ctx.trace_id,
                span_kind="retriever",
                name="rag_retrieval",
                status=status,
                sequence_no=sequence_no,
                started_at=started_at,
                ended_at=ended_at,
                input_preview=query,
                output_preview=(
                    f"{document_count} retrieved documents" if status != "running" else ""
                ),
                attributes=attributes,
                error_message=data.get("error"),
                parent_span_id=self._lifecycle_parent_id(ctx.trace_id),
            )
            return span_id
        if event_type == "context_retrieved":
            chunks = data.get("chunks") if isinstance(data.get("chunks"), list) else []
            documents = []
            for rank, chunk in enumerate(chunks[:12], start=1):
                chunk_data = chunk if isinstance(chunk, dict) else {}
                metadata = (
                    chunk_data.get("metadata")
                    if isinstance(chunk_data.get("metadata"), dict)
                    else {}
                )
                documents.append(
                    {
                        "rank": rank,
                        "dataset_id": data.get("dataset_id"),
                        "dataset_name": data.get("dataset_name"),
                        "chunk_id": chunk_data.get("segment_id") or chunk_data.get("chunk_id"),
                        "document_id": chunk_data.get("document_id"),
                        "score": chunk_data.get("score"),
                        "source_url": (
                            chunk_data.get("source_url")
                            or metadata.get("source_url")
                            or metadata.get("source_uri")
                        ),
                        "content_preview": _redact_trace_text(
                            chunk_data.get("content"),
                            limit=360,
                        ),
                    }
                )
            span_id = _span_uuid(ctx.trace_id, f"rag_document_fetch:{sequence_no}")
            await self._upsert_span(
                span_id=span_id,
                trace_id=ctx.trace_id,
                span_kind="document_fetch",
                name="rag_document_fetch",
                status="succeeded",
                sequence_no=sequence_no,
                started_at=now,
                ended_at=now,
                input_preview=data.get("query") or ctx.input_preview,
                output_preview=f"{len(documents)} retrieved chunks",
                attributes={
                    "source_adapter": "assistant_service.rag",
                    "openinference.span.kind": "RETRIEVER",
                    "gen_ai.operation.name": "retrieve",
                    "gen_ai.retrieval.query.text": data.get("query") or ctx.input_preview,
                    "retrieval.dataset_ids": [data.get("dataset_id")]
                    if data.get("dataset_id")
                    else [],
                    "retrieval.dataset_count": 1 if data.get("dataset_id") else 0,
                    "retrieval.document_count": len(documents),
                    "retrieval.documents": documents,
                    "privacy": {"payloads": "bounded_redacted_preview"},
                },
                error_message=None,
                parent_span_id=self._lifecycle_parent_id(ctx.trace_id),
            )
            return span_id
        if event_type == "context_budget":
            span_id = _span_uuid(ctx.trace_id, "context_building")
            await self._upsert_span(
                span_id=span_id,
                trace_id=ctx.trace_id,
                span_kind="context_building",
                name="context_building",
                status="succeeded",
                sequence_no=sequence_no,
                started_at=now,
                ended_at=now,
                input_preview=ctx.input_preview,
                output_preview="",
                attributes=data,
                error_message=None,
                parent_span_id=self._lifecycle_parent_id(ctx.trace_id),
            )
            return span_id
        if event_type in {"streaming_first_started", "streaming_first_completed"}:
            status = "succeeded" if event_type == "streaming_first_completed" else "running"
            span_id = _span_uuid(ctx.trace_id, "model_invocation:streaming_first")
            await self._upsert_span(
                span_id=span_id,
                trace_id=ctx.trace_id,
                span_kind="model_invocation",
                name="streaming_first_generation",
                status=status,
                sequence_no=sequence_no,
                started_at=ctx.started_at,
                ended_at=now if status == "succeeded" else None,
                input_preview=ctx.input_preview,
                output_preview=data.get("content_preview") or "",
                attributes={
                    "model_id": ctx.model_id,
                    "usage": data.get("usage") or {},
                    "iterations": data.get("iterations"),
                },
                error_message=None,
                parent_span_id=self._lifecycle_parent_id(ctx.trace_id),
            )
            return span_id
        if event_type in {
            "tool_call_start",
            "tool_call_started",
            "tool_call_result",
            "tool_call_end",
            "tool_call_completed",
            "tool_call_cancelled",
        }:
            tool_id = str(data.get("tool_id") or data.get("tool_call_id") or "unknown")
            status = "running"
            if event_type in {"tool_call_result", "tool_call_end", "tool_call_completed"}:
                event_status = str(data.get("status") or "").lower()
                status = (
                    "failed"
                    if data.get("error") or event_status in {"error", "failed"}
                    else "succeeded"
                )
            elif event_type == "tool_call_cancelled":
                status = "cancelled"
            span_id = _span_uuid(ctx.trace_id, f"tool:{tool_id}")
            await self._upsert_span(
                span_id=span_id,
                trace_id=ctx.trace_id,
                span_kind="tool_execution",
                name=f"tool:{data.get('tool_name') or data.get('name') or 'unknown'}",
                status=status,
                sequence_no=sequence_no,
                started_at=now,
                ended_at=now if status != "running" else None,
                input_preview=data.get("arguments") or "",
                output_preview=data.get("result_preview") or data.get("result") or "",
                attributes={
                    "tool_id": tool_id,
                    "step_id": data.get("step_id"),
                    "gateway_policy_decision": data.get("gateway_policy_decision"),
                    "sandbox_decision": data.get("sandbox_decision"),
                    "approval_consumed": data.get("approval_consumed"),
                    "direct_registry_denied": data.get("direct_registry_denied"),
                    "risk_level": data.get("risk_level"),
                    "requires_confirmation": data.get("requires_confirmation"),
                    "audit_shape": data.get("audit_shape"),
                    "redaction_policy": data.get("redaction_policy"),
                },
                error_message=data.get("error"),
                parent_span_id=self._lifecycle_parent_id(ctx.trace_id),
            )
            return span_id
        if event_type == "error":
            span_id = _span_uuid(ctx.trace_id, f"error:{sequence_no}")
            await self._upsert_span(
                span_id=span_id,
                trace_id=ctx.trace_id,
                span_kind="error",
                name="streaming_error",
                status="failed",
                sequence_no=sequence_no,
                started_at=now,
                ended_at=now,
                input_preview="",
                output_preview="",
                attributes={"code": data.get("code")},
                error_message=data.get("message") or data.get("error"),
                parent_span_id=self._lifecycle_parent_id(ctx.trace_id),
            )
            return span_id
        return None

    async def _upsert_span(
        self,
        *,
        span_id: str,
        trace_id: str,
        span_kind: str,
        name: str,
        status: str,
        sequence_no: int,
        started_at: float,
        ended_at: float | None,
        input_preview: Any,
        output_preview: Any,
        attributes: dict[str, Any],
        error_message: Any,
        parent_span_id: str | None = None,
    ) -> None:
        safe_error = _redact_trace_text(error_message) if error_message else None
        await self._trace_execute(
            """
            INSERT INTO agent_trace_spans (
                span_id, trace_id, parent_span_id, span_kind, name, status, sequence_no,
                started_at, ended_at, duration_ms, input_preview, output_preview,
                attributes, error_type, error_message
            ) VALUES (
                $1, $2, $3::uuid, $4, $5, $6, $7,
                $8, $9, $10, $11, $12,
                $13::jsonb, $14, $15
            )
            ON CONFLICT (span_id)
            DO UPDATE SET
                status = CASE
                    WHEN agent_trace_spans.status IN ('succeeded', 'failed', 'cancelled', 'skipped')
                         AND EXCLUDED.status = 'running'
                    THEN agent_trace_spans.status
                    ELSE EXCLUDED.status
                END,
                ended_at = CASE
                    WHEN agent_trace_spans.status IN ('succeeded', 'failed', 'cancelled', 'skipped')
                         AND EXCLUDED.status = 'running'
                    THEN agent_trace_spans.ended_at
                    ELSE COALESCE(EXCLUDED.ended_at, agent_trace_spans.ended_at)
                END,
                duration_ms = CASE
                    WHEN agent_trace_spans.status IN ('succeeded', 'failed', 'cancelled', 'skipped')
                         AND EXCLUDED.status = 'running'
                    THEN agent_trace_spans.duration_ms
                    ELSE GREATEST(agent_trace_spans.duration_ms, EXCLUDED.duration_ms)
                END,
                output_preview = CASE
                    WHEN EXCLUDED.output_preview <> '' THEN EXCLUDED.output_preview
                    ELSE agent_trace_spans.output_preview
                END,
                attributes = agent_trace_spans.attributes || EXCLUDED.attributes,
                error_type = COALESCE(EXCLUDED.error_type, agent_trace_spans.error_type),
                error_message = COALESCE(EXCLUDED.error_message, agent_trace_spans.error_message);
            """,
            span_id,
            trace_id,
            parent_span_id,
            span_kind,
            name[:160],
            status,
            sequence_no,
            _utc_from_timestamp(started_at),
            _utc_from_timestamp(ended_at) if ended_at is not None else None,
            _duration_ms(started_at, ended_at) if ended_at is not None else 0,
            _preview_text(input_preview),
            _preview_text(output_preview),
            _json_dumps(_sanitize_payload(attributes)),
            "runtime_error" if safe_error else None,
            safe_error,
        )
