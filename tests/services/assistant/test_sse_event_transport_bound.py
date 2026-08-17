from __future__ import annotations

import hashlib
import json
from types import SimpleNamespace
from typing import Any

import pytest
from assistant_service.core.assistant_models import AssistantStreamEvent
from assistant_service.core.sse_event_transport import (
    SSE_DATA_PAYLOAD_MAX_BYTES,
    SSE_EVENT_ARTIFACT_SOURCE,
    SSEEventTransportError,
    bound_sse_event,
    sse_data_payload_size,
)
from assistant_service.core.turn_event_collector import TurnEventCollector

_SCOPE = {
    "tenant_id": "tenant-a",
    "user_id": "user-a",
    "session_id": "session-a",
}


class _ScopedArtifactStorage:
    def __init__(self, *, scope_safe: bool = True, fail_create: bool = False) -> None:
        self.scope_safe = scope_safe
        self.fail_create = fail_create
        self.verify_created = True
        self.created: dict[str, Any] | None = None
        self.deleted: list[str] = []

    def supports_scoped_artifact_reads(self) -> bool:
        return self.scope_safe

    async def create_artifact(self, **fields: Any) -> Any:
        if self.fail_create:
            raise RuntimeError("storage unavailable")
        self.created = dict(fields)
        return SimpleNamespace(
            artifact_id="art-sse-1",
            source=fields["source"],
            turn_id=fields["turn_id"],
            tenant_id=fields["tenant_id"],
            user_id=fields["user_id"],
            session_id=fields["session_id"],
            metadata=dict(fields["metadata"]),
        )

    async def read_artifact_scoped(
        self,
        artifact_id: str,
        *,
        tenant_id: str,
        session_id: str,
        user_id: str,
        max_bytes: int,
        expected_source: str = "tool_output_spill",
    ) -> tuple[Any, bytes] | None:
        if not self.verify_created or self.created is None:
            return None
        if (
            artifact_id != "art-sse-1"
            or {"tenant_id": tenant_id, "user_id": user_id, "session_id": session_id} != _SCOPE
            or expected_source != SSE_EVENT_ARTIFACT_SOURCE
        ):
            return None
        content = self.created["content"]
        if len(content) > max_bytes:
            return None
        return (
            SimpleNamespace(
                artifact_id=artifact_id,
                source=self.created["source"],
                turn_id=self.created["turn_id"],
                tenant_id=self.created["tenant_id"],
                user_id=self.created["user_id"],
                session_id=self.created["session_id"],
                metadata=dict(self.created["metadata"]),
            ),
            content,
        )

    async def delete_artifact(self, artifact_id: str) -> None:
        self.deleted.append(artifact_id)


def _large_context_snapshot() -> dict[str, Any]:
    return {
        "schema_version": "assistant-context-snapshot/v1",
        "snapshot_id": "ctx-1",
        "run_id": "run-1",
        "session_id": _SCOPE["session_id"],
        "tenant_id": _SCOPE["tenant_id"],
        "user_id": _SCOPE["user_id"],
        "provenance": [
            {
                "dataset_id": f"kb-{index}",
                "evidence": "多字节企业依据" * 160,
            }
            for index in range(64)
        ],
    }


def _turn_state(*, blocked: bool) -> dict[str, Any]:
    return {
        "state": "approval_paused" if blocked else "succeeded",
        "terminal": not blocked,
        "run_id": "run-1",
        "request_id": "request-1",
        "attempt_id": "attempt-1",
        "attempt_number": 1,
    }


def _oversized_event(event_type: str) -> AssistantStreamEvent:
    snapshot = _large_context_snapshot()
    if event_type == "context_budget":
        return AssistantStreamEvent(
            event_type=event_type,
            data={
                "run_id": "run-1",
                "thread_id": _SCOPE["session_id"],
                "session_id": _SCOPE["session_id"],
                "context_packet": {"provenance": snapshot["provenance"]},
                "context_snapshot": snapshot,
            },
            timestamp=123.5,
        )

    blocked = event_type == "approval_required"
    turn_state = _turn_state(blocked=blocked)
    compact_snapshot = {
        key: snapshot[key]
        for key in ("schema_version", "snapshot_id", "run_id", "session_id", "tenant_id", "user_id")
    }
    envelope = {
        "schema_version": "assistant-turn-contract/v1",
        "run_id": "run-1",
        "request_id": "request-1",
        "session_id": _SCOPE["session_id"],
        "tenant_id": _SCOPE["tenant_id"],
        "user_id": _SCOPE["user_id"],
        "status": "blocked" if blocked else "succeeded",
        "exit_reason": "approval_pending" if blocked else "succeeded",
        "attempt_id": "attempt-1",
        "attempt_number": 1,
        "turn_state": turn_state,
        "context_snapshot": compact_snapshot,
    }
    return AssistantStreamEvent(
        event_type=event_type,
        data={
            "run_id": "run-1",
            "thread_id": _SCOPE["session_id"],
            "session_id": _SCOPE["session_id"],
            "approval_id": "approval-1" if blocked else None,
            "turn_state": turn_state,
            "terminal_envelope": envelope,
            "context_snapshot": compact_snapshot,
            "metadata": {"terminal_envelope": envelope},
            "debug": {"provenance": snapshot["provenance"]},
        },
        timestamp=123.5,
    )


@pytest.mark.asyncio
@pytest.mark.parametrize("event_type", ["context_budget", "approval_required", "run_finished"])
async def test_oversized_multibyte_final_sse_payload_spills_with_complete_scoped_receipt(
    event_type: str,
) -> None:
    storage = _ScopedArtifactStorage()
    original = _oversized_event(event_type)
    original_data = json.loads(json.dumps(original.data, ensure_ascii=False))
    assert sse_data_payload_size(original) > SSE_DATA_PAYLOAD_MAX_BYTES

    bounded = await bound_sse_event(
        original,
        artifact_storage=storage,
        **_SCOPE,
    )

    assert sse_data_payload_size(bounded) <= SSE_DATA_PAYLOAD_MAX_BYTES
    assert storage.created is not None
    receipt = bounded.data["payload_artifact"]
    assert receipt["artifact_id"] == "art-sse-1"
    assert receipt["host_verified"] is True
    assert receipt["complete_redacted"] is True
    assert receipt["content_sha256"] == hashlib.sha256(storage.created["content"]).hexdigest()

    verified = await storage.read_artifact_scoped(
        receipt["artifact_id"],
        **_SCOPE,
        max_bytes=2_000_000,
        expected_source=SSE_EVENT_ARTIFACT_SOURCE,
    )
    assert verified is not None
    artifact, content = verified
    assert artifact.source == SSE_EVENT_ARTIFACT_SOURCE
    assert json.loads(content) == original_data
    for wrong_scope in (
        {**_SCOPE, "tenant_id": "tenant-b"},
        {**_SCOPE, "user_id": "user-b"},
        {**_SCOPE, "session_id": "session-b"},
    ):
        assert (
            await storage.read_artifact_scoped(
                receipt["artifact_id"],
                **wrong_scope,
                max_bytes=2_000_000,
                expected_source=SSE_EVENT_ARTIFACT_SOURCE,
            )
            is None
        )

    if event_type in {"approval_required", "run_finished"}:
        envelope = bounded.data["terminal_envelope"]
        assert envelope == original_data["terminal_envelope"]
        assert json.dumps(envelope, sort_keys=True) == json.dumps(
            bounded.data["metadata"]["terminal_envelope"], sort_keys=True
        )
        assert bounded.data["turn_state"] == envelope["turn_state"]
        assert bounded.data["context_snapshot"] == envelope["context_snapshot"]
        collector = TurnEventCollector()
        collector.accept(bounded)
        turn = collector.finalize()
        assert turn.terminal_envelope == envelope
        assert turn.status == ("blocked" if event_type == "approval_required" else "succeeded")


@pytest.mark.asyncio
@pytest.mark.parametrize("failure", ["missing", "unsafe_scope", "create", "verify"])
async def test_oversized_sse_payload_storage_failure_is_fail_closed(failure: str) -> None:
    storage: _ScopedArtifactStorage | None
    if failure == "missing":
        storage = None
    else:
        storage = _ScopedArtifactStorage(
            scope_safe=failure != "unsafe_scope",
            fail_create=failure == "create",
        )
        storage.verify_created = failure != "verify"

    with pytest.raises(SSEEventTransportError, match="oversized SSE event"):
        await bound_sse_event(
            _oversized_event("context_budget"),
            artifact_storage=storage,
            **_SCOPE,
        )

    if failure == "verify":
        assert storage is not None
        assert storage.deleted == ["art-sse-1"]


@pytest.mark.asyncio
async def test_spilled_sse_artifact_redacts_secret_and_preserves_remaining_payload() -> None:
    storage = _ScopedArtifactStorage()
    event = _oversized_event("context_budget")
    event.data["debug"] = {
        "authorization": "Bearer secret-token-value",
        "marker": "public-complete-tail",
    }

    bounded = await bound_sse_event(event, artifact_storage=storage, **_SCOPE)

    assert storage.created is not None
    content = storage.created["content"].decode("utf-8")
    assert "secret-token-value" not in content
    assert "Bearer [redacted]" in content
    assert "public-complete-tail" in content
    assert bounded.data["payload_artifact"]["complete_redacted"] is True


@pytest.mark.asyncio
async def test_inline_sse_payload_under_limit_does_not_touch_artifact_storage() -> None:
    storage = _ScopedArtifactStorage(fail_create=True)
    event = AssistantStreamEvent(
        event_type="status",
        data={"message": "ready"},
        timestamp=123.5,
    )

    bounded = await bound_sse_event(event, artifact_storage=storage, **_SCOPE)

    assert bounded is event
    assert sse_data_payload_size(bounded) <= SSE_DATA_PAYLOAD_MAX_BYTES
    assert storage.created is None


@pytest.mark.asyncio
async def test_canonical_assistant_service_bounds_before_every_transport_adapter(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    from assistant_service.core.assistant_models import AssistantConfig
    from assistant_service.core.assistant_service import AssistantService

    storage = _ScopedArtifactStorage()
    service = AssistantService(
        model_registry=object(),  # type: ignore[arg-type]
        tool_invoker=object(),  # type: ignore[arg-type]
        artifact_storage=storage,  # type: ignore[arg-type]
    )

    async def no_session(**_kwargs: Any) -> None:
        return None

    async def no_policy(_user: Any, _datasets: Any) -> tuple[None, list[Any]]:
        return None, []

    async def oversized_loop(**_kwargs: Any):
        yield _oversized_event("context_budget")

    monkeypatch.setattr(service, "_ensure_session_exists", no_session)
    monkeypatch.setattr(service, "_resolve_domain_policy", no_policy)
    monkeypatch.setattr(service, "_execute_agent_loop", oversized_loop)
    user = SimpleNamespace(**_SCOPE)

    events = [
        event
        async for event in service.chat_stream(
            user=user,
            session_id=_SCOPE["session_id"],
            message="test",
            config=AssistantConfig(model_id="test-model"),
            history=[],
            persist_messages=False,
        )
    ]

    assert len(events) == 1
    assert sse_data_payload_size(events[0]) <= SSE_DATA_PAYLOAD_MAX_BYTES
    assert events[0].data["payload_artifact"]["host_verified"] is True


@pytest.mark.asyncio
async def test_oversized_safety_critical_terminal_envelope_fails_closed_without_rewriting() -> None:
    storage = _ScopedArtifactStorage()
    event = _oversized_event("run_finished")
    event.data["terminal_envelope"]["error"] = "终态不可改写" * 10_000
    event.data["metadata"]["terminal_envelope"] = event.data["terminal_envelope"]

    with pytest.raises(SSEEventTransportError, match="safety-critical terminal envelope"):
        await bound_sse_event(event, artifact_storage=storage, **_SCOPE)


def _tool_completed_with_huge_metadata() -> AssistantStreamEvent:
    return AssistantStreamEvent(
        event_type="tool_call_completed",
        data={
            "run_id": "run-1",
            "thread_id": _SCOPE["session_id"],
            "tool_call_id": "tool-1",
            "tool_name": "search_knowledge_base",
            "result": "kb result",
            "metadata": {
                "total_results": 8,
                "contexts": [
                    {
                        "dataset_id": "kb-1",
                        "chunks": [
                            {"content": "检索内容全文" * 8_000, "score": 0.9}
                        ],
                    }
                ],
            },
        },
        timestamp=123.5,
    )


@pytest.mark.asyncio
async def test_tool_completed_oversized_metadata_spills_under_wire_budget() -> None:
    """SPO-03 / A4: non-terminal metadata is spillable → frame ≤ 64 KB."""
    storage = _ScopedArtifactStorage()
    event = _tool_completed_with_huge_metadata()
    assert sse_data_payload_size(event) > SSE_DATA_PAYLOAD_MAX_BYTES

    bounded = await bound_sse_event(event, artifact_storage=storage, **_SCOPE)

    assert sse_data_payload_size(bounded) <= SSE_DATA_PAYLOAD_MAX_BYTES
    assert "metadata" in bounded.data["payload_artifact"]["replaced_fields"]
    assert "metadata" not in {
        key for key in bounded.data if key != "payload_artifact"
    }


@pytest.mark.asyncio
async def test_terminal_metadata_is_never_spilled_fails_closed() -> None:
    """Approval flows read metadata in-frame; spilling it is forbidden."""
    storage = _ScopedArtifactStorage()
    event = _oversized_event("approval_required")
    event.data["metadata"] = {"approval_proof": "审批凭据" * 12_000}
    event.data["terminal_envelope"]["error"] = None

    with pytest.raises(SSEEventTransportError):
        await bound_sse_event(event, artifact_storage=storage, **_SCOPE)


def test_bounded_context_item_caps_chunk_content() -> None:
    from assistant_service.core.tools.builtin_tools import _bounded_context_item

    item = {
        "content": "x" * 50_000,
        "score": 0.9,
        "dataset_id": "kb-1",
        "segment_id": "seg-1",
        "_cross_dataset_rrf_score": 1.0,
        "metadata": {"text": "y" * 50_000, "source_url": "https://example.test/doc"},
    }
    bounded = _bounded_context_item(item)

    assert bounded["score"] == 0.9
    assert bounded["dataset_id"] == "kb-1"
    assert bounded["segment_id"] == "seg-1"
    assert len(bounded["content"]) <= 400
    assert "metadata" not in bounded
    assert item["content"] == "x" * 50_000  # the model-facing copy is untouched
    assert item["metadata"]["text"] == "y" * 50_000


def test_bounded_context_item_keeps_short_content() -> None:
    from assistant_service.core.tools.builtin_tools import _bounded_context_item

    bounded = _bounded_context_item({"content": "short", "score": 0.5})
    assert bounded == {"content": "short", "score": 0.5}
