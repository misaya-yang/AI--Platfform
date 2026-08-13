"""Bound canonical Assistant SSE data payloads without changing turn semantics.

The canonical turn stream is also consumed by the non-stream collector and the
Responses adapter.  This projector therefore runs once in ``AssistantService``
before any adapter observes an event.  It may replace only bulky informational
fields with a host-verified, owner-scoped artifact receipt; safety-critical
approval and terminal fields are never rewritten.
"""

from __future__ import annotations

import hashlib
import json
import logging
import uuid
from dataclasses import replace
from typing import Any

from ai_gateway_core.logging import record_internal_exception
from ai_gateway_core.security import redact_trace_text

from .assistant_models import AssistantStreamEvent

SSE_DATA_PAYLOAD_MAX_BYTES = 64 * 1024
SSE_EVENT_ARTIFACT_MAX_BYTES = 2_000_000
SSE_EVENT_ARTIFACT_SOURCE = "sse_event_spill"

_SPILLABLE_FIELDS = (
    "context_packet",
    "context_detail",
    "provenance",
    "debug",
    "tool_result",
    "result",
    "context_snapshot",
)
_TERMINAL_EVENTS = frozenset(
    {"approval_required", "side_effect_unknown", "run_finished", "run_error"}
)


class SSEEventTransportError(RuntimeError):
    """An event cannot be delivered within the fail-closed wire contract."""


def _json_bytes(value: Any) -> bytes:
    return json.dumps(
        value,
        ensure_ascii=False,
        sort_keys=True,
        separators=(",", ":"),
        default=str,
    ).encode("utf-8")


def sse_data_payload_size(event: AssistantStreamEvent) -> int:
    """Return UTF-8 bytes of the JSON value written after one SSE ``data:``."""

    return len(
        _json_bytes(
            {
                "event_type": str(getattr(event.event_type, "value", event.event_type)),
                "data": event.data,
                "timestamp": event.timestamp,
            }
        )
    )


def _validate_scope(*, tenant_id: str, user_id: str, session_id: str) -> None:
    if not all(
        isinstance(value, str) and value.strip() for value in (tenant_id, user_id, session_id)
    ):
        raise SSEEventTransportError("oversized SSE event has no complete owner scope")


def _replace_spillable_fields(
    value: dict[str, Any],
    *,
    receipt: dict[str, Any],
    event_type: str,
) -> dict[str, Any]:
    bounded = dict(value)
    replaced: list[str] = []
    for field in _SPILLABLE_FIELDS:
        if field not in bounded:
            continue
        if event_type in _TERMINAL_EVENTS and field in {"context_snapshot"}:
            # The collector compares/copies this receipt together with the
            # terminal envelope. It must stay byte-for-byte identical.
            continue
        bounded.pop(field, None)
        replaced.append(field)
    bounded["payload_artifact"] = {**receipt, "replaced_fields": replaced}
    return bounded


async def bound_sse_event(
    event: AssistantStreamEvent,
    *,
    artifact_storage: Any,
    tenant_id: str,
    user_id: str,
    session_id: str,
) -> AssistantStreamEvent:
    """Ensure one final canonical event fits the wire limit or fail closed.

    Oversized content is stored as the complete redacted ``event.data`` JSON.
    A scoped read is performed immediately and its bytes/source/receipt are
    verified before a compact event is returned.  Storage or verification
    failure never falls back to emitting the oversized event.
    """

    if sse_data_payload_size(event) <= SSE_DATA_PAYLOAD_MAX_BYTES:
        return event
    if not isinstance(event.data, dict):
        raise SSEEventTransportError("oversized SSE event data is not an object")
    _validate_scope(tenant_id=tenant_id, user_id=user_id, session_id=session_id)

    event_type = str(getattr(event.event_type, "value", event.event_type))
    scope_gate = getattr(artifact_storage, "supports_scoped_artifact_reads", None)
    scoped_reader = getattr(artifact_storage, "read_artifact_scoped", None)
    creator = getattr(artifact_storage, "create_artifact", None)
    if not (callable(scope_gate) and callable(scoped_reader) and callable(creator)):
        raise SSEEventTransportError("oversized SSE event artifact storage is unavailable")
    try:
        scope_safe = bool(scope_gate())
    except Exception as exc:
        record_internal_exception(
            __name__, "assistant.core.sse_event_transport.internal_failure", exc
        )
        raise SSEEventTransportError(
            "oversized SSE event artifact scope verification is unavailable"
        ) from exc
    if not scope_safe:
        raise SSEEventTransportError("oversized SSE event artifact scope is not fail-closed")

    # Serialize after applying the shared secret redactor. This artifact is
    # downloadable by the owning user, but it must not become a side channel
    # for credentials that were already prohibited from traces/SSE.
    original = _json_bytes(event.data)
    redacted = redact_trace_text(original.decode("utf-8"))
    encoded = redacted.encode("utf-8")
    if len(encoded) > SSE_EVENT_ARTIFACT_MAX_BYTES:
        raise SSEEventTransportError("oversized SSE event exceeds artifact hard limit")

    receipt_id = f"sse_{uuid.uuid4().hex}"
    digest = hashlib.sha256(encoded).hexdigest()
    artifact: Any | None = None
    try:
        artifact = await creator(
            session_id=session_id,
            tenant_id=tenant_id,
            user_id=user_id,
            type="file",
            format="json",
            title="Assistant event payload",
            filename=f"assistant-event-{receipt_id.removeprefix('sse_')[:16]}.json",
            content=encoded,
            source=SSE_EVENT_ARTIFACT_SOURCE,
            metadata={
                "schema_version": "assistant-sse-event-artifact/v1",
                "event_type": event_type,
                "redacted": True,
                "complete_redacted": True,
                "content_sha256": digest,
                "size_bytes": len(encoded),
                "host_receipt_id": receipt_id,
            },
            turn_id=receipt_id,
        )
        artifact_id = str(getattr(artifact, "artifact_id", "") or "")
        if not artifact_id:
            raise RuntimeError("artifact id missing")
        verified = await scoped_reader(
            artifact_id,
            tenant_id=tenant_id,
            session_id=session_id,
            user_id=user_id,
            max_bytes=SSE_EVENT_ARTIFACT_MAX_BYTES,
            expected_source=SSE_EVENT_ARTIFACT_SOURCE,
        )
        if verified is None:
            raise RuntimeError("scoped artifact verification failed")
        verified_artifact, verified_content = verified
        metadata = dict(getattr(verified_artifact, "metadata", None) or {})
        if (
            verified_content != encoded
            or getattr(verified_artifact, "source", None) != SSE_EVENT_ARTIFACT_SOURCE
            or getattr(verified_artifact, "turn_id", None) != receipt_id
            or getattr(verified_artifact, "tenant_id", tenant_id) != tenant_id
            or getattr(verified_artifact, "user_id", user_id) != user_id
            or getattr(verified_artifact, "session_id", session_id) != session_id
            or metadata.get("host_receipt_id") != receipt_id
            or metadata.get("content_sha256") != digest
        ):
            raise RuntimeError("scoped artifact integrity verification failed")
    except Exception as exc:
        record_internal_exception(
            __name__, "assistant.core.sse_event_transport.internal_failure", exc
        )
        if artifact is not None:
            deleter = getattr(artifact_storage, "delete_artifact", None)
            if callable(deleter):
                try:
                    await deleter(str(getattr(artifact, "artifact_id", "") or ""))
                except Exception as exc:
                    record_internal_exception(
                        __name__,
                        "assistant.core.sse_event_transport.suppressed_failure",
                        exc,
                        level=logging.DEBUG,
                    )
        raise SSEEventTransportError("oversized SSE event artifact persistence failed") from exc

    receipt = {
        "artifact_id": str(artifact.artifact_id),
        "download_path": f"/api/v1/assistant/artifacts/{artifact.artifact_id}/download",
        "size_bytes": len(encoded),
        "content_sha256": digest,
        "complete_redacted": True,
        "host_verified": True,
        "redacted": True,
    }
    bounded_data = _replace_spillable_fields(event.data, receipt=receipt, event_type=event_type)
    bounded = replace(event, data=bounded_data)
    if sse_data_payload_size(bounded) > SSE_DATA_PAYLOAD_MAX_BYTES:
        # Never rewrite approval/terminal envelopes; the collector's exact
        # equality contract is stronger than successful transport of a bloated
        # terminal. The persisted artifact is intentionally deleted because no
        # receipt can be safely delivered to its owner.
        deleter = getattr(artifact_storage, "delete_artifact", None)
        if callable(deleter):
            try:
                await deleter(str(artifact.artifact_id))
            except Exception as exc:
                record_internal_exception(
                    __name__,
                    "assistant.core.sse_event_transport.suppressed_failure",
                    exc,
                    level=logging.DEBUG,
                )
        if event_type in _TERMINAL_EVENTS:
            raise SSEEventTransportError(
                "oversized safety-critical terminal envelope cannot be rewritten"
            )
        raise SSEEventTransportError("oversized SSE event remains above hard limit")
    return bounded


__all__ = [
    "SSE_DATA_PAYLOAD_MAX_BYTES",
    "SSE_EVENT_ARTIFACT_SOURCE",
    "SSEEventTransportError",
    "bound_sse_event",
    "sse_data_payload_size",
]
