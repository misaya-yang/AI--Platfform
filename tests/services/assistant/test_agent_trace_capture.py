from __future__ import annotations

import asyncio
import json
import time
from collections.abc import AsyncIterator
from dataclasses import dataclass
from typing import Any

import pytest
from assistant_service.core.assistant_service import AssistantConfig, AssistantService, RAGMode
from assistant_service.core.trace_writer import AssistantTraceContext, AssistantTraceWriter


@dataclass
class MockUserContext:
    user_id: str = "user-a"
    tenant_id: str = "tenant-a"
    tier: str = "normal"
    is_authenticated: bool = True
    ip: str = "127.0.0.1"
    roles: list[str] | None = None


class RecordingDB:
    def __init__(self) -> None:
        self.calls: list[tuple[str, tuple[Any, ...]]] = []

    async def execute(self, query: str, *args: Any) -> str:
        self.calls.append((query, args))
        return "OK"

    def serialized_calls(self) -> str:
        return json.dumps(self.calls, default=str)

    def queries(self) -> str:
        return "\n".join(query for query, _args in self.calls)

    def span_rows(self) -> list[dict[str, Any]]:
        rows: list[dict[str, Any]] = []
        for query, args in self.calls:
            if "INSERT INTO agent_trace_spans" not in query:
                continue
            rows.append(
                {
                    "span_id": args[0],
                    "trace_id": args[1],
                    "parent_span_id": args[2],
                    "span_kind": args[3],
                    "name": args[4],
                }
            )
        return rows


class BlockingDB(RecordingDB):
    def __init__(self) -> None:
        super().__init__()
        self.started = asyncio.Event()
        self.release = asyncio.Event()

    async def execute(self, query: str, *args: Any) -> str:
        self.calls.append((query, args))
        self.started.set()
        await self.release.wait()
        return "OK"


class FailingDB:
    async def execute(self, _query: str, *_args: Any) -> str:
        raise RuntimeError("database password=super-secret unavailable")


class FakeModelInfo:
    supports_vision = False
    provider = type("Provider", (), {"value": "dashscope"})()


class FakeModelRegistry:
    def __init__(self, *, content: str = "assistant reply") -> None:
        self.content = content
        self.closed = False

    def get_model(self, _model_id: str) -> FakeModelInfo:
        return FakeModelInfo()

    async def chat(self, **_kwargs: Any) -> tuple[str, dict[str, int]]:
        return self.content, {"input_tokens": 3, "output_tokens": 5}

    async def chat_stream(self, *_args: Any, **_kwargs: Any) -> AsyncIterator[Any]:
        from assistant_service.core.models.model_registry import StreamDelta

        yield StreamDelta(
            content=self.content,
            usage={"input_tokens": 3, "output_tokens": 5},
        )

    async def close(self) -> None:
        self.closed = True


class FakeExecutionGateway:
    enabled = True

    def __init__(self) -> None:
        self.started: list[dict[str, Any]] = []
        self.finished: list[dict[str, Any]] = []

    async def start_run(self, **kwargs: Any) -> None:
        self.started.append(kwargs)

    async def finish_run(self, **kwargs: Any) -> None:
        self.finished.append(kwargs)


@dataclass
class FakeKnowledgeResult:
    text: str
    score: float
    metadata: dict[str, Any]
    segment_id: str
    document_id: str
    image_url: str | None = None


class FakeKnowledgeService:
    def __init__(self) -> None:
        self.calls: list[dict[str, Any]] = []

    async def retrieve(self, **kwargs: Any) -> tuple[list[FakeKnowledgeResult], dict[str, Any]]:
        self.calls.append(kwargs)
        return (
            [
                FakeKnowledgeResult(
                    text="RAG source content " + ("x" * 900),
                    score=0.92,
                    metadata={
                        "citation_text": "Source A",
                        "source_url": "https://example.test/source-a",
                    },
                    segment_id="chunk-a",
                    document_id="doc-a",
                ),
                FakeKnowledgeResult(
                    text="Second source content",
                    score=0.81,
                    metadata={"source_uri": "kb://source-b"},
                    segment_id="chunk-b",
                    document_id="doc-b",
                ),
            ],
            {"dataset_name": "Support KB"},
        )


def _trace_ctx() -> AssistantTraceContext:
    return AssistantTraceContext.from_chat_request(
        run_id="11111111-1111-4111-8111-111111111111",
        request_id="request-a",
        tenant_id="tenant-a",
        user_id="user-a",
        session_id="session-a",
        message="hello",
        model_id="test-model",
        provider="test-provider",
        started_at=time.time(),
    )


def _json_args_containing(db: RecordingDB, key: str) -> list[dict[str, Any]]:
    docs: list[dict[str, Any]] = []
    for _query, args in db.calls:
        for arg in args:
            if not isinstance(arg, str) or key not in arg:
                continue
            try:
                value = json.loads(arg)
            except json.JSONDecodeError:
                continue
            if isinstance(value, dict):
                docs.append(value)
    return docs


@pytest.mark.asyncio
async def test_trace_writer_persists_root_span_events_and_terminal_conflict() -> None:
    db = RecordingDB()
    writer = AssistantTraceWriter(db, write_timeout_s=1.0)
    ctx = _trace_ctx()

    assert writer.start_trace(ctx)
    assert writer.record_event(
        ctx=ctx,
        event_type="run_started",
        sequence_no=1,
        payload={"run_id": ctx.run_id},
        phase="memory_loading",
    )
    assert writer.finish_trace(
        ctx=ctx,
        status="succeeded",
        output_preview="done",
        usage={"input_tokens": 1, "output_tokens": 2},
        terminal_event_type="run_finished",
        terminal_sequence_no=2,
        terminal_envelope={
            "schema_version": "assistant-turn-contract/v1",
            "run_id": ctx.run_id,
            "status": "succeeded",
            "exit_reason": "succeeded",
        },
    )
    await writer.drain(timeout_s=1.0)

    queries = db.queries()
    assert "agent_traces" in queries
    assert "agent_trace_spans" in queries
    assert "agent_trace_events" in queries
    assert "ON CONFLICT (trace_id, sequence_no)" in queries
    assert "run_started" in db.serialized_calls()
    assert "run_finished" in db.serialized_calls()
    assert "assistant-turn-contract/v1" in db.serialized_calls()
    assert "failed_writes" in db.serialized_calls()
    runtime_docs = _json_args_containing(db, "runtime_trajectory")
    runtime = next(doc["runtime_trajectory"] for doc in runtime_docs if "runtime_trajectory" in doc)
    assert runtime["schema_version"] == "assistant-runtime-trajectory/v1"
    assert runtime["status"] == "succeeded"
    assert runtime["exit_reason"] == "succeeded"
    assert runtime["context_snapshot_id"] is None
    assert runtime["trace_writer_health"]["redacted_writes"] == 1
    assert runtime["redaction_state"]["payloads"] == "redacted_truncated"
    assert writer.telemetry_snapshot()["pending_writes"] == 0


@pytest.mark.asyncio
async def test_non_stream_chat_returns_turn_contract_and_trace_metadata() -> None:
    db = RecordingDB()
    writer = AssistantTraceWriter(db, write_timeout_s=1.0)
    service = AssistantService(
        model_registry=FakeModelRegistry(content="done"),
        db=db,
        trace_writer=writer,
    )

    result = await service.chat(
        user=MockUserContext(),
        session_id="session-a",
        message="hello Authorization: Bearer super-secret-value",
        config=AssistantConfig(model_id="test-model"),
        history=[],
        persist_messages=False,
    )
    await writer.drain(timeout_s=1.0)

    assert result["terminal_envelope"]["schema_version"] == "assistant-turn-contract/v1"
    assert result["terminal_envelope"]["exit_reason"] == "succeeded"
    assert result["terminal_envelope"]["context_snapshot_id"].startswith("ctx_")
    assert result["context_snapshot"]["mode"] == "non_stream"
    assert "super-secret-value" not in json.dumps(result, default=str)
    assert "terminal_envelope" in db.serialized_calls()


@pytest.mark.asyncio
async def test_trace_writer_redacts_and_bounds_payloads() -> None:
    db = RecordingDB()
    writer = AssistantTraceWriter(db, write_timeout_s=1.0)
    ctx = _trace_ctx()

    writer.record_event(
        ctx=ctx,
        event_type="tool_call_started",
        sequence_no=1,
        payload={
            "tool_id": "tool-1",
            "tool_name": "fetch",
            "arguments": {
                "Authorization": "Bearer abc123SECRET",
                "password": "super-secret",
                "text": "x" * 10_000,
            },
        },
        phase="generation_storage",
    )
    await writer.drain(timeout_s=1.0)

    serialized = db.serialized_calls()
    assert "abc123SECRET" not in serialized
    assert "super-secret" not in serialized
    assert "[truncated]" in serialized


@pytest.mark.asyncio
async def test_trace_writer_records_tool_safety_attributes_for_eval_detail() -> None:
    db = RecordingDB()
    writer = AssistantTraceWriter(db, write_timeout_s=1.0)
    ctx = _trace_ctx()

    writer.record_event(
        ctx=ctx,
        event_type="tool_call_completed",
        sequence_no=3,
        payload={
            "tool_id": "tool-1",
            "tool_name": "code_executor",
            "gateway_policy_decision": {"decision": "allow", "policy_source": "tenant_policy"},
            "sandbox_decision": {"profile": "docker-gvisor-no-network", "available": True},
            "approval_consumed": True,
            "risk_level": "high",
            "requires_confirmation": True,
            "audit_shape": {"input": "code_hash_and_redacted_summary"},
            "redaction_policy": "redact_secrets_and_provider_env",
            "result_preview": "completed",
        },
        phase="tool_execution",
    )
    await writer.drain(timeout_s=1.0)

    serialized = db.serialized_calls()
    assert "gateway_policy_decision" in serialized
    assert "sandbox_decision" in serialized
    assert "approval_consumed" in serialized
    assert "docker-gvisor-no-network" in serialized
    assert "redact_secrets_and_provider_env" in serialized


@pytest.mark.asyncio
async def test_trace_writer_persistence_failure_is_tolerated() -> None:
    writer = AssistantTraceWriter(FailingDB(), write_timeout_s=1.0)

    assert writer.record_event(
        ctx=_trace_ctx(),
        event_type="run_started",
        sequence_no=1,
        payload={"password": "super-secret"},
        phase="memory_loading",
    )
    await writer.drain(timeout_s=1.0)

    assert writer.failed_writes >= 1


@pytest.mark.asyncio
async def test_trace_writer_non_blocking_latency_does_not_wait_for_blocked_db() -> None:
    db = BlockingDB()
    writer = AssistantTraceWriter(db, write_timeout_s=30.0)

    started = time.perf_counter()
    accepted = writer.start_trace(_trace_ctx())
    elapsed_ms = (time.perf_counter() - started) * 1000

    assert accepted
    assert elapsed_ms < 20
    await asyncio.wait_for(db.started.wait(), timeout=1.0)
    assert writer.pending_count >= 1
    db.release.set()
    await writer.drain(timeout_s=1.0)


@pytest.mark.asyncio
async def test_non_stream_final_response_does_not_wait_for_trace_persistence() -> None:
    db = BlockingDB()
    writer = AssistantTraceWriter(db, write_timeout_s=30.0)
    service = AssistantService(
        model_registry=FakeModelRegistry(content="fast reply"),
        trace_writer=writer,
    )

    result = await asyncio.wait_for(
        service.chat(
            user=MockUserContext(),  # type: ignore[arg-type]
            session_id="session-a",
            message="hello",
            config=AssistantConfig(model_id="test", kb_mode=RAGMode.DISABLED),
            history=[],
        ),
        timeout=0.5,
    )

    assert result["content"] == "fast reply"
    assert result["run_id"]
    await asyncio.wait_for(db.started.wait(), timeout=1.0)
    db.release.set()
    await writer.drain(timeout_s=1.0)


@pytest.mark.asyncio
async def test_non_stream_pre_model_failure_finishes_failed_trace(monkeypatch: pytest.MonkeyPatch) -> None:
    db = RecordingDB()
    writer = AssistantTraceWriter(db, write_timeout_s=1.0)
    service = AssistantService(
        model_registry=FakeModelRegistry(content="unused"),
        trace_writer=writer,
    )

    async def fail_ensure_session(**_kwargs: Any) -> None:
        raise RuntimeError("session password=super-secret unavailable")

    monkeypatch.setattr(service, "_ensure_session_exists", fail_ensure_session)

    with pytest.raises(RuntimeError, match="session"):
        await service.chat(
            user=MockUserContext(),  # type: ignore[arg-type]
            session_id="session-a",
            message="hello",
            config=AssistantConfig(model_id="test", kb_mode=RAGMode.DISABLED),
            history=[],
        )
    await writer.drain(timeout_s=1.0)

    serialized = db.serialized_calls()
    assert "run_error" in serialized
    assert "failed" in serialized
    assert "super-secret" not in serialized
    assert "password=[redacted]" in serialized


@pytest.mark.asyncio
async def test_span_upsert_does_not_regress_terminal_status() -> None:
    db = RecordingDB()
    writer = AssistantTraceWriter(db, write_timeout_s=1.0)
    ctx = _trace_ctx()

    writer.record_span(
        ctx=ctx,
        span_key="tool:lookup",
        span_kind="tool_execution",
        name="tool:lookup",
        status="succeeded",
        sequence_no=2,
        started_at=time.time(),
        ended_at=time.time(),
        output_preview="done",
    )
    writer.record_span(
        ctx=ctx,
        span_key="tool:lookup",
        span_kind="tool_execution",
        name="tool:lookup",
        status="running",
        sequence_no=1,
        started_at=time.time(),
    )
    await writer.drain(timeout_s=1.0)

    span_queries = [query for query, _args in db.calls if "agent_trace_spans" in query]
    assert span_queries
    assert "WHEN agent_trace_spans.status IN" in span_queries[-1]
    assert "THEN agent_trace_spans.status" in span_queries[-1]


@pytest.mark.asyncio
async def test_concurrent_non_stream_users_get_distinct_trace_contexts() -> None:
    db = RecordingDB()
    writer = AssistantTraceWriter(db, write_timeout_s=1.0)
    service = AssistantService(
        model_registry=FakeModelRegistry(content="fast reply"),
        trace_writer=writer,
    )

    result_a, result_b = await asyncio.gather(
        service.chat(
            user=MockUserContext(user_id="user-a", tenant_id="tenant-a"),  # type: ignore[arg-type]
            session_id="session-a",
            message="hello from a",
            config=AssistantConfig(model_id="test", kb_mode=RAGMode.DISABLED),
            history=[],
            persist_messages=False,
        ),
        service.chat(
            user=MockUserContext(user_id="user-b", tenant_id="tenant-b"),  # type: ignore[arg-type]
            session_id="session-b",
            message="hello from b",
            config=AssistantConfig(model_id="test", kb_mode=RAGMode.DISABLED),
            history=[],
            persist_messages=False,
        ),
    )
    await writer.drain(timeout_s=1.0)

    assert result_a["run_id"] != result_b["run_id"]
    serialized = db.serialized_calls()
    assert "tenant-a" in serialized
    assert "tenant-b" in serialized
    assert "user-a" in serialized
    assert "user-b" in serialized
    assert "session-a" in serialized
    assert "session-b" in serialized


@pytest.mark.asyncio
async def test_same_session_multi_turn_creates_distinct_trace_runs() -> None:
    db = RecordingDB()
    writer = AssistantTraceWriter(db, write_timeout_s=1.0)
    service = AssistantService(
        model_registry=FakeModelRegistry(content="turn reply"),
        trace_writer=writer,
    )
    user = MockUserContext(user_id="user-a", tenant_id="tenant-a")

    first = await service.chat(
        user=user,  # type: ignore[arg-type]
        session_id="session-a",
        message="first turn",
        config=AssistantConfig(model_id="test", kb_mode=RAGMode.DISABLED),
        history=[],
        persist_messages=False,
    )
    second = await service.chat(
        user=user,  # type: ignore[arg-type]
        session_id="session-a",
        message="second turn",
        config=AssistantConfig(model_id="test", kb_mode=RAGMode.DISABLED),
        history=[
            {"role": "user", "content": "first turn"},
            {"role": "assistant", "content": "turn reply"},
        ],
        persist_messages=False,
    )
    await writer.drain(timeout_s=1.0)

    assert first["run_id"] != second["run_id"]
    serialized = db.serialized_calls()
    assert "first turn" in serialized
    assert "second turn" in serialized
    assert serialized.count("session-a") >= 2
    locators = [
        doc["transcript_locator"]
        for doc in _json_args_containing(db, "transcript_locator")
        if isinstance(doc.get("transcript_locator"), dict)
    ]
    assert any(locator.get("turn_index") == 1 for locator in locators)
    assert any(
        locator.get("turn_index") == 2
        and locator.get("history_message_count") == 2
        and "first turn" in str(locator.get("transcript_excerpt"))
        and "second turn" in str(locator.get("current_message_preview"))
        for locator in locators
    )


@pytest.mark.asyncio
async def test_non_stream_rag_retrieval_records_semantic_trace_span_and_events() -> None:
    db = RecordingDB()
    writer = AssistantTraceWriter(db, write_timeout_s=1.0)
    kb_service = FakeKnowledgeService()
    service = AssistantService(
        model_registry=FakeModelRegistry(content="grounded answer"),
        kb_service=kb_service,  # type: ignore[arg-type]
        trace_writer=writer,
    )

    result = await service.chat(
        user=MockUserContext(),  # type: ignore[arg-type]
        session_id="session-a",
        message="What does the refund policy say?",
        config=AssistantConfig(
            model_id="test",
            kb_mode=RAGMode.AUTO,
            kb_dataset_ids=["support-kb"],
            kb_top_k=2,
            kb_score_threshold=0.35,
        ),
        history=[],
        persist_messages=False,
    )
    await writer.drain(timeout_s=1.0)

    assert result["content"] == "grounded answer"
    assert kb_service.calls and kb_service.calls[0]["dataset_id"] == "support-kb"
    serialized = db.serialized_calls()
    assert "rag_retrieval_started" in serialized
    assert "rag_retrieval_completed" in serialized
    assert "retriever" in serialized
    assert "rag_retrieval" in serialized
    assert "openinference.span.kind" in serialized
    assert "RETRIEVER" in serialized
    assert "gen_ai.retrieval.query.text" in serialized
    assert "retrieval.documents" in serialized
    assert "support-kb" in serialized
    assert "chunk-a" in serialized
    assert "doc-a" in serialized
    assert "RAG source content " in serialized
    assert "x" * 700 not in serialized


@pytest.mark.asyncio
async def test_trace_writer_persists_parent_span_id_and_otel_fields() -> None:
    db = RecordingDB()
    writer = AssistantTraceWriter(db, write_timeout_s=1.0)
    traceparent = "00-4bf92f3577b34da6a3ce929d0e0e4736-00f067aa0ba902b7-01"
    ctx = AssistantTraceContext.from_chat_request(
        run_id="11111111-1111-4111-8111-111111111111",
        request_id="request-a",
        tenant_id="tenant-a",
        user_id="user-a",
        session_id="session-a",
        message="hello",
        model_id="test-model",
        provider="test-provider",
        started_at=time.time(),
        traceparent=traceparent,
    )

    writer.start_trace(ctx)
    writer.record_event(
        ctx=ctx,
        event_type="run_started",
        sequence_no=0,
        payload={"run_id": ctx.run_id},
        phase="lifecycle",
    )
    writer.record_event(
        ctx=ctx,
        event_type="tool_call_started",
        sequence_no=1,
        payload={"tool_id": "tool-1", "tool_name": "search_knowledge_base"},
        phase="execution",
    )
    await writer.drain(timeout_s=1.0)

    spans = db.span_rows()
    assert spans, "expected assistant trace spans to be persisted"
    lifecycle = next(row for row in spans if row["span_kind"] == "lifecycle")
    child_spans = [row for row in spans if row["span_kind"] != "lifecycle"]
    assert lifecycle["parent_span_id"] is None
    assert child_spans, "expected at least one child span under lifecycle root"
    assert all(row["parent_span_id"] == lifecycle["span_id"] for row in child_spans)
    assert all(row["trace_id"] == lifecycle["trace_id"] for row in child_spans)

    trace_inserts = [args for query, args in db.calls if "INSERT INTO agent_traces" in query]
    assert trace_inserts
    assert trace_inserts[0][7] == "4bf92f3577b34da6a3ce929d0e0e4736"
    assert trace_inserts[0][8] == traceparent


@pytest.mark.asyncio
async def test_trace_writer_maps_streaming_context_retrieved_to_rag_document_span() -> None:
    db = RecordingDB()
    writer = AssistantTraceWriter(db, write_timeout_s=1.0)
    ctx = _trace_ctx()

    writer.record_event(
        ctx=ctx,
        event_type="context_retrieved",
        sequence_no=1,
        payload={
            "dataset_id": "support-kb",
            "dataset_name": "Support KB",
            "query": "refund policy",
            "chunks": [
                {
                    "content": "streaming RAG source " + ("y" * 900),
                    "score": 0.88,
                    "segment_id": "chunk-stream-a",
                    "document_id": "doc-stream-a",
                    "source_url": "https://example.test/stream-a",
                }
            ],
        },
        phase="generation_storage",
    )
    await writer.drain(timeout_s=1.0)

    serialized = db.serialized_calls()
    assert "context_retrieved" in serialized
    assert "document_fetch" in serialized
    assert "rag_document_fetch" in serialized
    assert "openinference.span.kind" in serialized
    assert "RETRIEVER" in serialized
    assert "retrieval.documents" in serialized
    assert "chunk-stream-a" in serialized
    assert "doc-stream-a" in serialized
    assert "streaming RAG source " in serialized
    assert "y" * 700 not in serialized


@pytest.mark.asyncio
async def test_streaming_first_trace_locator_captures_multi_turn_history() -> None:
    from assistant_service.core.agent.agent_loop import AgentLoop, AgentLoopConfig

    db = RecordingDB()
    writer = AssistantTraceWriter(db, write_timeout_s=1.0)
    loop = AgentLoop(model_registry=FakeModelRegistry(content="stream reply"), trace_writer=writer)

    events = []
    async for event in loop.execute(
        session_id="session-a",
        user=MockUserContext(),  # type: ignore[arg-type]
        message="third turn needs the order id",
        config=AgentLoopConfig(model_id="test", max_tool_iterations=1),
        history=[
            {"role": "user", "content": "first turn order id is A-001"},
            {"role": "assistant", "content": "noted A-001"},
            {"role": "user", "content": "second turn asks for status"},
            {"role": "assistant", "content": "status is pending"},
        ],
    ):
        events.append(event.event_type)
    await writer.drain(timeout_s=1.0)

    assert "run_finished" in events
    locators = [
        doc["transcript_locator"]
        for doc in _json_args_containing(db, "transcript_locator")
        if isinstance(doc.get("transcript_locator"), dict)
    ]
    assert any(
        locator.get("turn_index") == 3
        and locator.get("history_message_count") == 4
        and "A-001" in str(locator.get("transcript_excerpt"))
        and "third turn" in str(locator.get("current_message_preview"))
        for locator in locators
    )


@pytest.mark.asyncio
async def test_streaming_first_event_does_not_wait_for_trace_persistence() -> None:
    from assistant_service.core.agent.agent_loop import AgentLoop, AgentLoopConfig

    db = BlockingDB()
    writer = AssistantTraceWriter(db, write_timeout_s=30.0)
    loop = AgentLoop(model_registry=FakeModelRegistry(), trace_writer=writer)
    stream = loop.execute(
        session_id="session-a",
        user=MockUserContext(),  # type: ignore[arg-type]
        message="hello",
        config=AgentLoopConfig(model_id="test", max_tool_iterations=1),
        history=[],
    )

    first_event = await asyncio.wait_for(stream.__anext__(), timeout=0.5)

    assert first_event.event_type in {"gateway_decision", "run_started"}
    await asyncio.wait_for(db.started.wait(), timeout=1.0)
    await stream.aclose()
    db.release.set()
    await writer.drain(timeout_s=1.0)


@pytest.mark.asyncio
async def test_run_status_update_does_not_wait_for_trace_persistence() -> None:
    from assistant_service.core.agent.agent_loop import AgentLoop, AgentLoopConfig

    db = BlockingDB()
    writer = AssistantTraceWriter(db, write_timeout_s=30.0)
    gateway = FakeExecutionGateway()
    loop = AgentLoop(
        model_registry=FakeModelRegistry(content="done"),
        execution_gateway=gateway,  # type: ignore[arg-type]
        trace_writer=writer,
    )

    async def _consume() -> list[str]:
        events: list[str] = []
        async for event in loop.execute(
            session_id="session-a",
            user=MockUserContext(),  # type: ignore[arg-type]
            message="hello",
            config=AgentLoopConfig(model_id="test", max_tool_iterations=1),
            history=[],
        ):
            events.append(event.event_type)
        return events

    events = await asyncio.wait_for(_consume(), timeout=1.0)

    assert "run_finished" in events
    assert gateway.finished
    await asyncio.wait_for(db.started.wait(), timeout=1.0)
    db.release.set()
    await writer.drain(timeout_s=1.0)


@pytest.mark.asyncio
async def test_duplicate_terminal_behavior_is_idempotent() -> None:
    db = RecordingDB()
    writer = AssistantTraceWriter(db, write_timeout_s=1.0)
    ctx = _trace_ctx()

    for _ in range(2):
        writer.finish_trace(
            ctx=ctx,
            status="succeeded",
            output_preview="done",
            usage={},
            terminal_event_type="run_finished",
            terminal_sequence_no=99,
        )
    await writer.drain(timeout_s=1.0)

    event_queries = [query for query, _args in db.calls if "agent_trace_events" in query]
    assert len(event_queries) == 2
    assert all("ON CONFLICT (trace_id, sequence_no)" in query for query in event_queries)
