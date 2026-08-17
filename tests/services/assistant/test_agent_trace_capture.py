from __future__ import annotations

import asyncio
import json
import logging
import time
from collections.abc import AsyncIterator
from dataclasses import dataclass
from typing import Any

import pytest
from assistant_service.core.agent.runtime_context import AgentRuntimeExecutionContext
from assistant_service.core.assistant_service import AssistantConfig, AssistantService, RAGMode
from assistant_service.core.gateway.execution_gateway import AssistantExecutionGateway
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
                    "status": args[5],
                    "sequence_no": args[6],
                }
            )
        return rows


class ResumeCursorDB(RecordingDB):
    def __init__(self, max_sequence_no: int | None) -> None:
        super().__init__()
        self.max_sequence_no = max_sequence_no
        self.operations: list[str] = []
        self.fetchrow_calls: list[tuple[str, tuple[Any, ...]]] = []

    async def execute(self, query: str, *args: Any) -> str:
        self.operations.append("execute")
        return await super().execute(query, *args)

    async def fetchrow(self, query: str, *args: Any) -> dict[str, Any] | None:
        self.operations.append("fetchrow")
        self.fetchrow_calls.append((query, args))
        if self.max_sequence_no is None:
            return None
        return {"max_sequence_no": self.max_sequence_no}


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


class BlockingCursorDB(BlockingDB):
    def __init__(self) -> None:
        super().__init__()
        self.fetchrow_calls: list[tuple[str, tuple[Any, ...]]] = []

    async def fetchrow(self, query: str, *args: Any) -> dict[str, int]:
        self.fetchrow_calls.append((query, args))
        return {"max_sequence_no": 41}


class TraceBlockingCursorDB(ResumeCursorDB):
    def __init__(self, block_trace_id: str) -> None:
        super().__init__(41)
        self.block_trace_id = block_trace_id
        self.started = asyncio.Event()
        self.release = asyncio.Event()

    async def execute(self, query: str, *args: Any) -> str:
        if self.block_trace_id in args:
            self.started.set()
            await self.release.wait()
        return await super().execute(query, *args)


class FailingDB:
    async def execute(self, _query: str, *_args: Any) -> str:
        raise RuntimeError("database password=super-secret unavailable")


class FailingCursorDB(FailingDB):
    def __init__(self) -> None:
        self.fetchrow_calls: list[tuple[str, tuple[Any, ...]]] = []

    async def fetchrow(self, query: str, *args: Any) -> dict[str, int]:
        self.fetchrow_calls.append((query, args))
        return {"max_sequence_no": 41}


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


def _trace_ctx(
    *,
    run_id: str = "11111111-1111-4111-8111-111111111111",
    request_id: str = "request-a",
) -> AssistantTraceContext:
    return AssistantTraceContext.from_chat_request(
        run_id=run_id,
        request_id=request_id,
        tenant_id="tenant-a",
        user_id="user-a",
        session_id="session-a",
        message="hello",
        model_id="test-model",
        provider="test-provider",
        started_at=time.time(),
    )


def _agent_runtime_ctx(
    *,
    agent_id: str = "11111111-1111-4111-8111-111111111111",
    version_id: str = "22222222-2222-4222-8222-222222222222",
) -> AgentRuntimeExecutionContext:
    return AgentRuntimeExecutionContext(
        tenant_id="tenant-a",
        caller_principal="user-a",
        agent_id=agent_id,
        agent_version_id=version_id,
        agent_draft_revision=None,
        publication_id="33333333-3333-4333-8333-333333333333",
        channel="api",
        session_id="session-a",
        runtime_fingerprint=f"sha256:{agent_id}:{version_id}",
        agent_spec_hash="sha256:spec",
        prompt_hash="sha256:prompt",
        tool_schema_hash="sha256:tools",
        skills_hash="sha256:skills",
        knowledge_revision_hash="sha256:knowledge",
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
async def test_text_delta_trace_events_do_not_repeat_root_and_lifecycle_upserts() -> None:
    db = RecordingDB()
    writer = AssistantTraceWriter(db, write_timeout_s=1.0)
    ctx = _trace_ctx()

    assert writer.start_trace(ctx)
    for sequence_no in range(1, 26):
        assert writer.record_event(
            ctx=ctx,
            event_type="text_delta",
            sequence_no=sequence_no,
            payload={"content": f"chunk-{sequence_no}"},
            phase="model_generation",
        )
    await writer.drain(timeout_s=1.0, strict=True, trace_id=ctx.trace_id)

    root_writes = [query for query, _args in db.calls if "INSERT INTO agent_traces" in query]
    lifecycle_writes = [
        query
        for query, args in db.calls
        if "INSERT INTO agent_trace_spans" in query and len(args) > 4 and args[4] == "assistant_run"
    ]
    event_writes = [
        query for query, _args in db.calls if "INSERT INTO agent_trace_events" in query
    ]

    assert len(root_writes) == 1
    assert len(lifecycle_writes) == 1
    assert len(event_writes) == 25
    assert len(db.calls) == 27


@pytest.mark.asyncio
async def test_trace_writer_persists_safe_startup_config_fingerprint_only_in_metadata() -> None:
    from assistant_service.config.startup_fingerprint import resolve_startup_config

    db = RecordingDB()
    unknown_secret = "unknown-secret-must-not-persist"
    startup_config = resolve_startup_config(
        {"DASHSCOPE_CHAT_API_KEY": unknown_secret}
    )
    writer = AssistantTraceWriter(
        db,
        write_timeout_s=1.0,
        startup_config=startup_config,
    )
    ctx = _trace_ctx()

    assert writer.start_trace(ctx)
    await writer.drain(timeout_s=1.0)

    metadata = _json_args_containing(db, "startup_config_fingerprint")
    persisted = next(doc for doc in metadata if "startup_config_fingerprint" in doc)
    assert persisted["startup_config_fingerprint"] == startup_config.sha256
    assert persisted["startup_config"]["schema_version"] == "assistant-startup-config/v1"
    assert persisted["startup_config"]["settings"] == startup_config.safe_summary()["settings"]
    assert "unknown" not in persisted["startup_config"]
    assert unknown_secret not in db.serialized_calls()
    event_payloads = [
        args
        for query, args in db.calls
        if "INSERT INTO agent_trace_events" in query
    ]
    assert "startup_config" not in json.dumps(event_payloads, default=str)

    rejected = AssistantTraceWriter(
        db,
        startup_config={"schema_version": "assistant-startup-config/v1", "api_key": unknown_secret},  # type: ignore[arg-type]
    )
    assert rejected.startup_config_summary is None


@pytest.mark.asyncio
async def test_agent_trace_root_persists_explicit_runtime_dimensions() -> None:
    db = RecordingDB()
    writer = AssistantTraceWriter(db, write_timeout_s=1.0)
    runtime = _agent_runtime_ctx()
    ctx = AssistantTraceContext.from_chat_request(
        run_id="aaaaaaaa-aaaa-4aaa-8aaa-aaaaaaaaaaaa",
        request_id="request-agent",
        tenant_id="tenant-a",
        user_id="user-a",
        session_id="session-a",
        message="hello",
        model_id="qwen3.7-plus",
        provider="dashscope",
        started_at=time.time(),
        agent_runtime=runtime,
    )

    assert writer.start_trace(ctx)
    await writer.drain(timeout_s=1.0)

    root_calls = [call for call in db.calls if "INSERT INTO agent_traces" in call[0]]
    assert len(root_calls) == 1
    query, args = root_calls[0]
    for column in (
        "agent_id",
        "agent_version_id",
        "publication_id",
        "channel",
        "runtime_fingerprint",
        "agent_spec_hash",
    ):
        assert column in query
    assert runtime.agent_id in args
    assert runtime.agent_version_id in args
    assert runtime.publication_id in args
    assert runtime.runtime_fingerprint in args
    assert ctx.agent_id == runtime.agent_id
    assert "owner-only Agent prompt text" not in db.serialized_calls()


@pytest.mark.asyncio
async def test_run_checkpoint_and_resume_are_pinned_to_agent_runtime() -> None:
    gateway = AssistantExecutionGateway(
        tool_invoker=object(),  # type: ignore[arg-type]
        database=None,
        enabled=True,
    )
    runtime = _agent_runtime_ctx()
    dimensions = runtime.trace_dimensions()
    run_id = "aaaaaaaa-aaaa-4aaa-8aaa-aaaaaaaaaaaa"

    await gateway.start_run(
        run_id=run_id,
        tenant_id="tenant-a",
        user_id="user-a",
        session_id="session-a",
        engine="agent_loop",
        execution_profile="safe",
        memory_mode="strict",
        os_agent_enabled=False,
        request_preview="hello",
        agent_runtime=dimensions,
    )
    checkpoint = await gateway.save_run_checkpoint(
        run_id=run_id,
        tenant_id="tenant-a",
        user_id="user-a",
        session_id="session-a",
        phase="tool_call_pending",
        agent_runtime=dimensions,
    )
    run = await gateway.get_run(
        run_id=run_id,
        tenant_id="tenant-a",
        user_id="user-a",
    )

    assert run is not None
    assert run["agent_id"] == runtime.agent_id
    assert run["agent_version_id"] == runtime.agent_version_id
    assert checkpoint["runtime_fingerprint"] == runtime.runtime_fingerprint

    other = _agent_runtime_ctx(
        agent_id="44444444-4444-4444-8444-444444444444",
        version_id="55555555-5555-4555-8555-555555555555",
    )
    resume = await gateway.prepare_run_resume(
        run_id=run_id,
        tenant_id="tenant-a",
        user_id="user-a",
        session_id="session-a",
        agent_runtime=other.trace_dimensions(),
    )
    assert resume is not None
    assert resume["status"] == "blocked"
    assert resume["reason"] == "run_agent_runtime_mismatch"

    with pytest.raises(PermissionError, match="different Agent runtime"):
        await gateway.start_run(
            run_id=run_id,
            tenant_id="tenant-a",
            user_id="user-a",
            session_id="session-a",
            engine="agent_loop",
            execution_profile="safe",
            memory_mode="strict",
            os_agent_enabled=False,
            request_preview="forged",
            agent_runtime=other.trace_dimensions(),
        )


@pytest.mark.asyncio
async def test_checkpoint_reuses_one_message_digest_for_hash_and_receipt() -> None:
    gateway = AssistantExecutionGateway(
        tool_invoker=object(),  # type: ignore[arg-type]
        database=None,
        enabled=True,
    )
    digest_calls = 0
    original_digest = gateway._message_state_digest

    def counting_digest(messages: list[dict[str, object]]) -> list[dict[str, object]]:
        nonlocal digest_calls
        digest_calls += 1
        return original_digest(messages)

    gateway._message_state_digest = counting_digest  # type: ignore[method-assign]
    await gateway.save_run_checkpoint(
        run_id="aaaaaaaa-aaaa-4aaa-8aaa-aaaaaaaaaaab",
        tenant_id="tenant-a",
        user_id="user-a",
        session_id="session-a",
        phase="model_turn_started",
        messages=[{"role": "user", "content": "large checkpoint input"}],
    )

    assert digest_calls == 1

@pytest.mark.asyncio
async def test_agent_run_resume_rejects_missing_or_cross_session_context() -> None:
    gateway = AssistantExecutionGateway(
        tool_invoker=object(),  # type: ignore[arg-type]
        database=None,
        enabled=True,
    )
    runtime = _agent_runtime_ctx()
    dimensions = runtime.trace_dimensions()
    run_id = "dddddddd-dddd-4ddd-8ddd-dddddddddddd"

    await gateway.start_run(
        run_id=run_id,
        tenant_id="tenant-a",
        user_id="user-a",
        session_id="session-a",
        engine="agent_loop",
        execution_profile="safe",
        memory_mode="strict",
        os_agent_enabled=False,
        request_preview="hello",
        agent_runtime=dimensions,
    )
    await gateway.save_run_checkpoint(
        run_id=run_id,
        tenant_id="tenant-a",
        user_id="user-a",
        session_id="session-a",
        phase="tool_call_pending",
        agent_runtime=dimensions,
    )

    missing = await gateway.prepare_run_resume(
        run_id=run_id,
        tenant_id="tenant-a",
        user_id="user-a",
        agent_runtime=dimensions,
    )
    cross_session = await gateway.prepare_run_resume(
        run_id=run_id,
        tenant_id="tenant-a",
        user_id="user-a",
        session_id="session-b",
        agent_runtime=dimensions,
    )
    same_session = await gateway.prepare_run_resume(
        run_id=run_id,
        tenant_id="tenant-a",
        user_id="user-a",
        session_id="session-a",
        agent_runtime=dimensions,
    )

    assert missing is not None
    assert missing["status"] == "blocked"
    assert missing["reason"] == "run_session_required"
    assert cross_session is not None
    assert cross_session["status"] == "blocked"
    assert cross_session["reason"] == "run_session_mismatch"
    assert same_session is not None
    assert same_session["status"] == "blocked"
    assert same_session["reason"] == "checkpoint_not_restorable"
    assert same_session["execution_authorized"] is False
    assert same_session["checkpoint"]["session_id"] == "session-a"


@pytest.mark.asyncio
async def test_agent_run_resume_rejects_checkpoint_session_drift() -> None:
    gateway = AssistantExecutionGateway(
        tool_invoker=object(),  # type: ignore[arg-type]
        database=None,
        enabled=True,
    )
    runtime = _agent_runtime_ctx()
    dimensions = runtime.trace_dimensions()
    run_id = "eeeeeeee-eeee-4eee-8eee-eeeeeeeeeeee"

    await gateway.start_run(
        run_id=run_id,
        tenant_id="tenant-a",
        user_id="user-a",
        session_id="session-a",
        engine="agent_loop",
        execution_profile="safe",
        memory_mode="strict",
        os_agent_enabled=False,
        request_preview="hello",
        agent_runtime=dimensions,
    )
    await gateway.save_run_checkpoint(
        run_id=run_id,
        tenant_id="tenant-a",
        user_id="user-a",
        session_id="session-b",
        phase="tool_call_pending",
        agent_runtime=dimensions,
    )

    resume = await gateway.prepare_run_resume(
        run_id=run_id,
        tenant_id="tenant-a",
        user_id="user-a",
        session_id="session-a",
        agent_runtime=dimensions,
    )

    assert resume is not None
    assert resume["status"] == "blocked"
    assert resume["reason"] == "checkpoint_session_mismatch"


@pytest.mark.asyncio
async def test_agent_loop_conflicting_session_cannot_finalize_existing_run(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    from assistant_service.core.agent import agent_loop as agent_loop_module
    from assistant_service.core.agent.agent_loop import AgentLoop, AgentLoopConfig

    gateway = AssistantExecutionGateway(
        tool_invoker=object(),  # type: ignore[arg-type]
        database=None,
        enabled=True,
    )
    runtime = _agent_runtime_ctx()
    dimensions = runtime.trace_dimensions()
    run_id = "ffffffff-ffff-4fff-8fff-ffffffffffff"
    await gateway.start_run(
        run_id=run_id,
        tenant_id="tenant-a",
        user_id="user-a",
        session_id="session-a",
        engine="agent_loop",
        execution_profile="safe",
        memory_mode="strict",
        os_agent_enabled=False,
        request_preview="correct session",
        agent_runtime=dimensions,
    )
    baseline_checkpoint = await gateway.save_run_checkpoint(
        run_id=run_id,
        tenant_id="tenant-a",
        user_id="user-a",
        session_id="session-a",
        phase="run_started",
        agent_runtime=dimensions,
    )

    original_context = agent_loop_module.AgentLoopContext

    def _fixed_run_context(**kwargs: Any) -> Any:
        return original_context(**kwargs, run_id=run_id)

    monkeypatch.setattr(agent_loop_module, "AgentLoopContext", _fixed_run_context)
    loop = AgentLoop(
        model_registry=FakeModelRegistry(),
        execution_gateway=gateway,
    )

    events = [
        event
        async for event in loop.execute(
            session_id="session-b",
            user=MockUserContext(),  # type: ignore[arg-type]
            message="conflicting caller",
            config=AgentLoopConfig(
                model_id="test",
                max_tool_iterations=1,
                agent_runtime=runtime,
            ),
            history=[],
        )
    ]
    terminals = [event for event in events if event.event_type in {"run_finished", "run_error"}]
    assert len(terminals) == 1
    assert terminals[0].event_type == "run_error"
    assert "different session" in terminals[0].data["error"]
    assert terminals[0].data["terminal_envelope"]["status"] == "failed"

    with pytest.raises(PermissionError, match="different session"):
        await gateway.finish_run(
            run_id=run_id,
            status="failed",
            usage={"output_tokens": 999},
            error="wrong session",
            tenant_id="tenant-a",
            user_id="user-a",
            session_id="session-b",
            agent_runtime=dimensions,
        )
    with pytest.raises(PermissionError, match="different Agent runtime"):
        await gateway.finish_run(
            run_id=run_id,
            status="failed",
            error="wrong Agent",
            tenant_id="tenant-a",
            user_id="user-a",
            session_id="session-a",
            agent_runtime=_agent_runtime_ctx(
                agent_id="44444444-4444-4444-8444-444444444444",
                version_id="55555555-5555-4555-8555-555555555555",
            ).trace_dimensions(),
        )
    with pytest.raises(PermissionError, match="requires tenant, user, session"):
        await gateway.finish_run(
            run_id=run_id,
            status="failed",
            error="missing runtime",
            tenant_id="tenant-a",
            user_id="user-a",
            session_id="session-a",
        )
    with pytest.raises(PermissionError, match="requires tenant, user, session"):
        await gateway.finish_run(
            run_id=run_id,
            status="failed",
            error="missing session",
            tenant_id="tenant-a",
            user_id="user-a",
            agent_runtime=dimensions,
        )

    persisted = await gateway.get_run(
        run_id=run_id,
        tenant_id="tenant-a",
        user_id="user-a",
    )
    checkpoint = await gateway.get_run_checkpoint(
        run_id=run_id,
        tenant_id="tenant-a",
        user_id="user-a",
    )
    assert persisted is not None
    assert persisted["session_id"] == "session-a"
    assert persisted["status"] == "running"
    assert persisted["error"] is None
    assert persisted["usage"] == {}
    assert persisted["finished_at"] is None
    assert checkpoint is not None
    assert checkpoint["checkpoint_id"] == baseline_checkpoint["checkpoint_id"]
    assert len(gateway._checkpoints[run_id]) == 1  # AUDIT-OK: isolation assertion


@pytest.mark.asyncio
async def test_finish_run_sql_is_bound_to_session_and_agent_runtime() -> None:
    database = RecordingDB()
    gateway = AssistantExecutionGateway(
        tool_invoker=object(),  # type: ignore[arg-type]
        database=database,
        enabled=True,
    )
    dimensions = _agent_runtime_ctx().trace_dimensions()

    await gateway.finish_run(
        run_id="ffffffff-ffff-4fff-8fff-ffffffffffff",
        status="succeeded",
        usage={"output_tokens": 5},
        tenant_id="tenant-a",
        user_id="user-a",
        session_id="session-a",
        agent_runtime=dimensions,
    )

    query, args = database.calls[-1]
    assert "tenant_id =" in query
    assert "user_id =" in query
    assert "session_id =" in query
    for column in (
        "agent_id",
        "agent_version_id",
        "agent_draft_revision",
        "publication_id",
        "channel",
        "runtime_fingerprint",
        "agent_spec_hash",
    ):
        assert f"{column} IS NOT DISTINCT FROM" in query
    assert "session-a" in args
    assert dimensions["agent_id"] in args
    assert dimensions["agent_version_id"] in args
    assert dimensions["publication_id"] in args
    assert dimensions["runtime_fingerprint"] in args
    assert dimensions["agent_spec_hash"] in args

    await gateway.finish_run(
        run_id="aaaaaaaa-aaaa-4aaa-8aaa-aaaaaaaaaaaa",
        status="succeeded",
    )
    legacy_query, _legacy_args = database.calls[-1]
    assert "agent_id IS NULL" in legacy_query


@pytest.mark.asyncio
@pytest.mark.parametrize(
    ("max_sequence_no", "expected"),
    [(41, 41), (None, 0), (-3, 0)],
)
async def test_resume_sequence_drains_pending_writes_and_reads_persisted_maximum(
    max_sequence_no: int | None,
    expected: int,
) -> None:
    db = ResumeCursorDB(max_sequence_no)
    writer = AssistantTraceWriter(db, write_timeout_s=1.0)
    ctx = _trace_ctx()

    writer.record_event(
        ctx=ctx,
        event_type="approval_required",
        sequence_no=1,
        payload={"approval_id": "approval-1"},
        phase="execution",
    )

    sequence_no = await writer.resume_sequence(ctx)

    assert sequence_no == expected
    assert db.operations[-1] == "fetchrow"
    assert writer.pending_count == 0
    query, args = db.fetchrow_calls[0]
    assert "agent_trace_events" in query
    assert "agent_trace_spans" in query
    assert "$1" in query
    assert args == (ctx.trace_id,)


@pytest.mark.asyncio
async def test_resume_sequence_without_trace_database_returns_zero() -> None:
    writer = AssistantTraceWriter(None, write_timeout_s=1.0)
    ctx = _trace_ctx()

    assert (
        writer.record_event(
            ctx=ctx,
            event_type="approval_required",
            sequence_no=1,
            payload={"approval_id": "approval-1"},
            phase="execution",
        )
        is False
    )

    assert await writer.resume_sequence(ctx) == 0


@pytest.mark.asyncio
async def test_resume_sequence_timeout_never_queries_persisted_cursor() -> None:
    db = BlockingCursorDB()
    writer = AssistantTraceWriter(db, write_timeout_s=0.02)
    ctx = _trace_ctx()
    writer.record_event(
        ctx=ctx,
        event_type="approval_required",
        sequence_no=1,
        payload={"approval_id": "approval-1"},
        phase="execution",
    )
    await asyncio.wait_for(db.started.wait(), timeout=1.0)

    try:
        with pytest.raises(TimeoutError, match="trace persistence barrier timed out"):
            await writer.resume_sequence(ctx)
    finally:
        db.release.set()
        await writer.drain(timeout_s=0.1)

    assert db.fetchrow_calls == []


@pytest.mark.asyncio
async def test_resume_sequence_failed_write_never_queries_persisted_cursor() -> None:
    db = FailingCursorDB()
    writer = AssistantTraceWriter(db, write_timeout_s=1.0)
    ctx_a = _trace_ctx()
    ctx_b = _trace_ctx(
        run_id="22222222-2222-4222-8222-222222222222",
        request_id="request-b",
    )
    writer.record_event(
        ctx=ctx_a,
        event_type="approval_required",
        sequence_no=1,
        payload={"approval_id": "approval-1", "password": "super-secret"},
        phase="execution",
    )
    await writer.drain(timeout_s=1.0)
    assert writer.pending_count == 0
    assert writer.failed_writes == 1

    for _attempt in range(2):
        with pytest.raises(RuntimeError, match="trace persistence barrier failed") as exc_info:
            await writer.resume_sequence(ctx_a)

        assert "super-secret" not in str(exc_info.value)
    assert db.fetchrow_calls == []

    assert await writer.resume_sequence(ctx_b) == 41
    assert db.fetchrow_calls[0][1] == (ctx_b.trace_id,)


@pytest.mark.asyncio
async def test_resume_sequence_only_waits_for_pending_writes_in_same_trace() -> None:
    ctx_a = _trace_ctx()
    ctx_b = _trace_ctx(
        run_id="22222222-2222-4222-8222-222222222222",
        request_id="request-b",
    )
    db = TraceBlockingCursorDB(ctx_a.trace_id)
    writer = AssistantTraceWriter(db, write_timeout_s=0.02)
    writer.record_event(
        ctx=ctx_a,
        event_type="approval_required",
        sequence_no=1,
        payload={"approval_id": "approval-a"},
        phase="execution",
    )
    await asyncio.wait_for(db.started.wait(), timeout=1.0)

    try:
        assert await writer.resume_sequence(ctx_b) == 41
    finally:
        db.release.set()
        await writer.drain(timeout_s=1.0)

    assert db.fetchrow_calls[0][1] == (ctx_b.trace_id,)


@pytest.mark.asyncio
async def test_scoped_trace_drain_ignores_continuous_unrelated_submissions() -> None:
    writer = AssistantTraceWriter(None, write_timeout_s=1.0)
    target = _trace_ctx()
    unrelated = _trace_ctx(
        run_id="22222222-2222-4222-8222-222222222222",
        request_id="request-b",
    )
    stop = asyncio.Event()
    started = asyncio.Event()

    async def submit_unrelated_traces() -> None:
        started.set()
        while not stop.is_set():
            writer.start_trace(unrelated)
            await asyncio.sleep(0)

    submitter = asyncio.create_task(submit_unrelated_traces())
    await started.wait()
    try:
        await asyncio.wait_for(
            writer.drain(timeout_s=0.2, trace_id=target.trace_id),
            timeout=0.05,
        )
    finally:
        stop.set()
        await submitter


@pytest.mark.asyncio
async def test_repeated_trace_failures_keep_sticky_state_bounded() -> None:
    db = FailingCursorDB()
    writer = AssistantTraceWriter(db, write_timeout_s=1.0)
    ctx = _trace_ctx()

    for sequence_no in range(1, 6):
        writer.record_event(
            ctx=ctx,
            event_type="approval_required",
            sequence_no=sequence_no,
            payload={"approval_id": f"approval-{sequence_no}"},
            phase="execution",
        )
    await writer.drain(timeout_s=1.0)

    assert writer.failed_writes == 5
    assert len(writer._failed_outcomes) == 1
    with pytest.raises(RuntimeError, match="trace persistence barrier failed"):
        await writer.resume_sequence(ctx)
    assert db.fetchrow_calls == []


@pytest.mark.asyncio
async def test_resume_sequence_dropped_write_never_queries_persisted_cursor() -> None:
    db = ResumeCursorDB(41)
    writer = AssistantTraceWriter(db, max_pending=0, write_timeout_s=1.0)
    ctx = _trace_ctx()

    accepted = writer.record_event(
        ctx=ctx,
        event_type="approval_required",
        sequence_no=1,
        payload={"approval_id": "approval-1"},
        phase="execution",
    )

    assert accepted is False
    assert writer.pending_count == 0
    assert writer.dropped_writes == 1
    for _attempt in range(2):
        with pytest.raises(RuntimeError, match="trace persistence barrier failed"):
            await writer.resume_sequence(ctx)
    assert db.fetchrow_calls == []


@pytest.mark.asyncio
@pytest.mark.parametrize(
    ("events", "expected_status"),
    [
        (
            [
                ("tool_call_start", {"tool_call_id": "tool-1", "name": "generate_image"}),
                (
                    "tool_call_result",
                    {
                        "tool_call_id": "tool-1",
                        "name": "generate_image",
                        "status": "completed",
                        "result": "ok",
                    },
                ),
                (
                    "tool_call_end",
                    {"tool_call_id": "tool-1", "name": "generate_image", "status": "completed"},
                ),
            ],
            "succeeded",
        ),
        (
            [
                ("tool_call_started", {"tool_id": "tool-1", "tool_name": "generate_image"}),
                (
                    "tool_call_result",
                    {
                        "tool_call_id": "tool-1",
                        "name": "generate_image",
                        "status": "error",
                        "error": "tool failed",
                    },
                ),
                (
                    "tool_call_end",
                    {"tool_call_id": "tool-1", "name": "generate_image", "status": "error"},
                ),
            ],
            "failed",
        ),
    ],
    ids=["middleware_resume_success", "gateway_resume_failure"],
)
async def test_resume_tool_event_aliases_converge_on_one_stable_span(
    events: list[tuple[str, dict[str, Any]]],
    expected_status: str,
) -> None:
    db = RecordingDB()
    writer = AssistantTraceWriter(db, write_timeout_s=1.0)
    ctx = _trace_ctx()

    for sequence_no, (event_type, payload) in enumerate(events, start=1):
        writer.record_event(
            ctx=ctx,
            event_type=event_type,
            sequence_no=sequence_no,
            payload=payload,
            phase="execution",
        )
    await writer.drain(timeout_s=1.0)

    tool_spans = [row for row in db.span_rows() if row["span_kind"] == "tool_execution"]
    assert tool_spans
    assert len({row["span_id"] for row in tool_spans}) == 1
    assert tool_spans[-1]["status"] == expected_status


@pytest.mark.asyncio
async def test_non_stream_chat_returns_turn_contract_and_trace_metadata(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    # RecordingDB implements the trace sink only, not the Gateway run store.
    # Keep this test on the direct path so it exercises the intended contract.
    monkeypatch.setenv("ASSISTANT_GATEWAY_ENABLED", "false")
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
    assert result["terminal_envelope"]["attempt_id"].startswith("att_")
    assert result["terminal_envelope"]["attempt_number"] == 1
    assert result["terminal_envelope"]["turn_state"]["state"] == "succeeded"
    assert result["terminal_envelope"]["turn_state"]["terminal"] is True
    assert result["context_snapshot"]["attempt_id"] == result["terminal_envelope"]["attempt_id"]
    assert result["context_snapshot"]["mode"] == "streaming_first"
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
async def test_trace_writer_persistence_failure_is_tolerated(
    caplog: pytest.LogCaptureFixture,
) -> None:
    writer = AssistantTraceWriter(FailingDB(), write_timeout_s=1.0)

    with caplog.at_level(
        logging.WARNING,
        logger="assistant_service.core.trace_writer",
    ):
        assert writer.record_event(
            ctx=_trace_ctx(),
            event_type="run_started",
            sequence_no=1,
            payload={"password": "super-secret"},
            phase="memory_loading",
        )
        await writer.drain(timeout_s=1.0)

    assert writer.failed_writes >= 1
    records = [
        record
        for record in caplog.records
        if record.getMessage().startswith("assistant.trace.write_failed")
    ]
    assert records
    assert all(record.exc_info is None for record in records)
    assert all(record.internal_exception["frames"] for record in records)
    assert "super-secret" not in caplog.text


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
async def test_non_stream_pre_model_failure_finishes_failed_trace(
    monkeypatch: pytest.MonkeyPatch,
    caplog: pytest.LogCaptureFixture,
) -> None:
    db = RecordingDB()
    writer = AssistantTraceWriter(db, write_timeout_s=1.0)
    service = AssistantService(
        model_registry=FakeModelRegistry(content="unused"),
        trace_writer=writer,
    )

    async def fail_ensure_session(**_kwargs: Any) -> None:
        raise RuntimeError("session password=super-secret unavailable")

    monkeypatch.setattr(service, "_ensure_session_exists", fail_ensure_session)

    with caplog.at_level(
        logging.ERROR,
        logger="assistant_service.core.assistant_service",
    ), pytest.raises(RuntimeError, match="session"):
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
    records = [
        record
        for record in caplog.records
        if record.getMessage().startswith("assistant.turn.preflight_failed")
    ]
    assert len(records) == 1
    assert records[0].exc_info is None
    assert records[0].internal_exception["frames"]
    assert "super-secret" not in caplog.text


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
            session_id="trace-concurrent-a",
            message="hello from a",
            config=AssistantConfig(model_id="test", kb_mode=RAGMode.DISABLED),
            history=[],
            persist_messages=False,
        ),
        service.chat(
            user=MockUserContext(user_id="user-b", tenant_id="tenant-b"),  # type: ignore[arg-type]
            session_id="trace-concurrent-b",
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
    assert "trace-concurrent-a" in serialized
    assert "trace-concurrent-b" in serialized


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
    assert writer._submission_coroutines == {}


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
