from __future__ import annotations

import asyncio
from datetime import datetime, timezone
from types import SimpleNamespace

import pytest
from assistant_service.core.runtime.memory.background_sync import OrderedBackgroundSync
from assistant_service.core.runtime.memory.lifecycle import (
    MemoryProviderLifecycle,
    should_sync_turn_to_memory,
)
from assistant_service.core.runtime.memory.retriever import HybridMemoryRetriever
from assistant_service.core.runtime.memory.source_store import MemorySourceStore
from assistant_service.core.runtime.memory.turn_sync import CompletedTurnMemorySync


class _NoPII:
    def redact(self, text: str):
        return text, []


def _completed(run_id: str) -> dict[str, str]:
    return {"status": "succeeded", "exit_reason": "succeeded", "run_id": run_id}


def _turn_sync(
    tmp_path,
    indexer,
    *,
    lifecycle: MemoryProviderLifecycle | None = None,
) -> CompletedTurnMemorySync:
    return CompletedTurnMemorySync(
        memory_store=MemorySourceStore(tmp_path),
        memory_indexer=indexer,
        pii_filter=_NoPII(),
        lifecycle=lifecycle or MemoryProviderLifecycle(),
    )


def test_explicit_opt_in_cannot_promote_an_incomplete_turn() -> None:
    allowed, reason = should_sync_turn_to_memory(
        {"status": "blocked", "exit_reason": "approval_pending"},
        explicit_opt_in=True,
    )

    assert allowed is False
    assert reason == "terminal_exit_reason_approval_pending"


def test_conversation_history_context_is_ephemeral_and_isolated_per_turn() -> None:
    from assistant_service.core.agent.agent_loop_models import (
        AgentLoopConfig,
        AgentLoopContext,
    )

    first = AgentLoopContext(
        session_id="session-a",
        user_id="user-a",
        tenant_id="tenant-a",
        message="first",
        config=AgentLoopConfig(),
    )
    second = AgentLoopContext(
        session_id="session-b",
        user_id="user-a",
        tenant_id="tenant-a",
        message="second",
        config=AgentLoopConfig(),
    )
    first.conversation_history.append({"role": "user", "content": "private-sentinel"})

    assert second.conversation_history == []
    assert "private-sentinel" not in repr(first)


@pytest.mark.asyncio
async def test_completed_envelope_without_final_assistant_message_is_not_persisted(
    tmp_path,
) -> None:
    sync = _turn_sync(
        tmp_path,
        SimpleNamespace(index_source=lambda **_kwargs: None),
    )

    result = await sync.sync(
        tenant_id="tenant-a",
        user_id="user-a",
        session_id="session-a",
        user_message="unfinished request",
        assistant_message="",
        terminal_envelope=_completed("run-a"),
    )

    assert result.skipped is True
    assert result.reason == "completed_turn_assistant_message_missing"
    assert list(tmp_path.rglob("*.md")) == []


@pytest.mark.asyncio
async def test_slow_derivative_index_never_blocks_source_commit(tmp_path) -> None:
    started = asyncio.Event()
    release = asyncio.Event()

    class _SlowIndexer:
        async def index_source(self, **_kwargs):
            started.set()
            await release.wait()
            return SimpleNamespace(fallback_reason=None)

    sync = _turn_sync(tmp_path, _SlowIndexer())
    result = await asyncio.wait_for(
        sync.sync(
            tenant_id="tenant-a",
            user_id="user-a",
            session_id="session-a",
            user_message="remember this",
            assistant_message="saved",
            terminal_envelope=_completed("run-a"),
        ),
        timeout=0.2,
    )

    assert result.synced is True
    assert result.source_committed is True
    assert result.index_pending is True
    await asyncio.wait_for(started.wait(), timeout=0.2)
    assert sync.status(result.background_operation_id or "")["status"] == "running"

    release.set()
    flushed = await sync.flush_pending()
    assert flushed["status"] == "completed"
    assert sync.status(result.background_operation_id or "")["status"] == "completed"


@pytest.mark.asyncio
async def test_same_source_derivatives_preserve_completed_turn_order(tmp_path) -> None:
    first_started = asyncio.Event()
    release_first = asyncio.Event()
    indexed_contents: list[str] = []

    class _OrderedIndexer:
        async def index_source(self, **kwargs):
            indexed_contents.append(kwargs["content"])
            if len(indexed_contents) == 1:
                first_started.set()
                await release_first.wait()
            return SimpleNamespace(fallback_reason=None)

    sync = _turn_sync(tmp_path, _OrderedIndexer())
    first = await sync.sync(
        tenant_id="tenant-a",
        user_id="user-a",
        session_id="session-a",
        user_message="first fact",
        assistant_message="first saved",
        terminal_envelope=_completed("run-1"),
    )
    second = await sync.sync(
        tenant_id="tenant-a",
        user_id="user-a",
        session_id="session-a",
        user_message="second fact",
        assistant_message="second saved",
        terminal_envelope=_completed("run-2"),
    )

    await asyncio.wait_for(first_started.wait(), timeout=0.2)
    assert len(indexed_contents) == 1
    release_first.set()
    await sync.flush_pending()

    assert first.background_operation_id != second.background_operation_id
    assert "first fact" in indexed_contents[0]
    assert "second fact" not in indexed_contents[0]
    assert "first fact" in indexed_contents[1]
    assert "second fact" in indexed_contents[1]


@pytest.mark.asyncio
async def test_provider_failure_is_partial_and_does_not_undo_local_commit(tmp_path) -> None:
    class _Indexer:
        async def index_source(self, **_kwargs):
            return SimpleNamespace(fallback_reason=None)

    class _FailingProvider(MemoryProviderLifecycle):
        async def sync_turn(self, **_kwargs):
            raise RuntimeError("provider unavailable")

    sync = _turn_sync(tmp_path, _Indexer(), lifecycle=_FailingProvider())
    result = await sync.sync(
        tenant_id="tenant-a",
        user_id="user-a",
        session_id="session-a",
        user_message="durable fact",
        assistant_message="saved",
        terminal_envelope=_completed("run-a"),
    )
    flushed = await sync.flush_pending()
    receipt = sync.status(result.background_operation_id or "")

    assert result.synced is True
    assert next(tmp_path.rglob("*.md")).is_file()
    assert flushed["status"] == "partial"
    assert receipt is not None
    assert receipt["result"]["errors"] == ["memory_provider_sync_pending"]


@pytest.mark.asyncio
async def test_background_sync_releases_tasks_and_bounds_unreported_receipts() -> None:
    queue = OrderedBackgroundSync(max_pending=4, max_retained_receipts=8)

    async def work():
        return {"status": "completed"}

    for index in range(20):
        queue.enqueue(
            key=("tenant-a", "user-a", index),
            operation_id=f"operation-{index}",
            work=work,
        )
        await asyncio.sleep(0)
    await asyncio.sleep(0)

    assert queue._operation_tasks == {}
    assert len(queue._receipts) <= 8
    assert len(queue._unreported) <= 8


@pytest.mark.asyncio
async def test_background_sync_reports_cancelled_chain_as_partial() -> None:
    queue = OrderedBackgroundSync(max_pending=4, max_retained_receipts=8)
    release = asyncio.Event()

    async def blocked_work():
        await release.wait()
        return {"status": "completed"}

    queue.enqueue(key="source-a", operation_id="first", work=blocked_work)
    queue.enqueue(key="source-a", operation_id="second", work=blocked_work)
    queue._operation_tasks["first"].cancel()
    await asyncio.sleep(0)
    await asyncio.sleep(0)

    flushed = await queue.flush_pending(timeout=0)

    assert queue.receipt("first")["status"] == "cancelled"
    assert queue.receipt("second")["status"] == "cancelled"
    assert flushed["status"] == "partial"
    assert flushed["counts"] == {"cancelled": 2}


@pytest.mark.asyncio
async def test_background_sync_defers_derivatives_at_capacity() -> None:
    queue = OrderedBackgroundSync(max_pending=1, max_retained_receipts=4)
    release = asyncio.Event()

    async def blocked_work():
        await release.wait()
        return {"status": "completed"}

    queue.enqueue(key="source-a", operation_id="active", work=blocked_work)
    deferred = queue.enqueue(key="source-b", operation_id="deferred", work=blocked_work)

    assert deferred.status == "deferred"
    assert deferred.result["source_committed"] is True
    assert deferred.result["index_pending"] is True
    assert queue.receipt("deferred")["error_code"] == "memory_background_sync_capacity"

    release.set()
    flushed = await queue.flush_pending()
    assert flushed["status"] == "partial"


@pytest.mark.asyncio
async def test_equal_relevance_prefers_newer_memory_source_and_line() -> None:
    old_id = "11111111-1111-1111-1111-111111111111"
    new_id = "22222222-2222-2222-2222-222222222222"

    class _Database:
        async def fetch(self, sql: str, *_args):
            if "WITH ranked" in sql:
                return [
                    {"chunk_id": old_id, "text_score": 1.0},
                    {"chunk_id": new_id, "text_score": 1.0},
                ]
            return [
                {
                    "chunk_id": old_id,
                    "content": "project value is OLD",
                    "start_line": 1,
                    "end_line": 2,
                    "metadata": {},
                    "source_id": old_id,
                    "source_path": "/memory/old.md",
                    "source_type": "daily",
                    "source_updated_at": datetime(2026, 1, 1, tzinfo=timezone.utc),
                },
                {
                    "chunk_id": new_id,
                    "content": "project value is NEW",
                    "start_line": 10,
                    "end_line": 11,
                    "metadata": {},
                    "source_id": new_id,
                    "source_path": "/memory/new.md",
                    "source_type": "daily",
                    "source_updated_at": datetime(2026, 1, 2, tzinfo=timezone.utc),
                },
            ]

    hits = await HybridMemoryRetriever(_Database()).search(
        tenant_id="tenant-a",
        user_id="user-a",
        query="project value",
        max_results=2,
    )

    assert [hit.content for hit in hits] == [
        "project value is NEW",
        "project value is OLD",
    ]
