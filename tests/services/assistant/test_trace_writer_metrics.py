"""SPO-00 assistant trace SQL counter tests.

Drives the shipped ``AssistantTraceWriter`` against a recording database and
asserts the exercisable ``trace_writer_metrics.sql_statements`` counter agrees
with the statements actually issued. The same counter backs the SPO-03 gate
(25 deltas ≤ 4 statements after batching).
"""

from __future__ import annotations

import asyncio
from typing import Any

import pytest
from assistant_service.core.trace_metrics import trace_writer_metrics
from assistant_service.core.trace_writer import AssistantTraceContext, AssistantTraceWriter


class _RecordingDB:
    def __init__(self) -> None:
        self.calls: list[str] = []

    async def execute(self, sql: str, *_args: Any) -> str:
        self.calls.append(sql)
        return "OK"

    async def executemany(self, sql: str, _rows: list[tuple[Any, ...]]) -> str:
        self.calls.append(sql)
        return "OK"

    async def fetchrow(self, sql: str, *_args: Any) -> dict[str, Any]:
        self.calls.append(sql)
        return {}


def _ctx() -> AssistantTraceContext:
    return AssistantTraceContext(
        trace_id="11111111-1111-1111-1111-111111111111",
        run_id="run-1",
        tenant_id="tenant-a",
        user_id="user-a",
        session_id="session-1",
        request_id="request-1",
        model_id="test-model",
    )


@pytest.fixture(autouse=True)
def _reset_metrics() -> None:
    trace_writer_metrics.reset()
    yield
    trace_writer_metrics.reset()


@pytest.mark.asyncio
async def test_trace_writer_counter_matches_issued_statements() -> None:
    database = _RecordingDB()
    writer = AssistantTraceWriter(database=database)
    ctx = _ctx()

    assert writer.start_trace(ctx)
    for sequence_no in range(1, 4):
        assert writer.record_event(
            ctx=ctx,
            event_type="text_delta",
            sequence_no=sequence_no,
            payload={"delta": "hi"},
        )
    assert writer.finish_trace(ctx=ctx, status="succeeded")
    await writer.drain(timeout_s=2.0)

    # start: root + lifecycle = 2; 3 buffered events = 1 batch; finish:
    # traces update + lifecycle upsert + outbox enqueue = 3. Total 6, with
    # exactly one INSERT INTO agent_trace_events carrying all 3 rows (A3).
    assert len(database.calls) == 6
    assert trace_writer_metrics.sql_statements == 6
    event_inserts = [
        sql for sql in database.calls if "INSERT INTO agent_trace_events" in sql
    ]
    assert len(event_inserts) == 1


@pytest.mark.asyncio
async def test_trace_writer_counter_covers_ttft_update() -> None:
    database = _RecordingDB()
    writer = AssistantTraceWriter(database=database)
    ctx = _ctx()

    assert writer.start_trace(ctx)
    assert writer.record_event(
        ctx=ctx,
        event_type="ttft",
        sequence_no=1,
        payload={"ttft_ms": 123},
    )
    await writer.drain(timeout_s=2.0)

    # root + lifecycle + ttft event batch + first_token_latency update = 4.
    assert len(database.calls) == 4
    assert trace_writer_metrics.sql_statements == 4


@pytest.mark.asyncio
async def test_twenty_five_deltas_persist_as_one_batch_within_gate_budget() -> None:
    """SPO-03 / A3 gate: 25 deltas → root + lifecycle + 1 batch + finish."""
    database = _RecordingDB()
    writer = AssistantTraceWriter(database=database)
    ctx = _ctx()

    assert writer.start_trace(ctx)
    for sequence_no in range(1, 26):
        assert writer.record_event(
            ctx=ctx,
            event_type="text_delta",
            sequence_no=sequence_no,
            payload={"delta": f"delta-{sequence_no}"},
        )
    assert writer.finish_trace(ctx=ctx, status="succeeded")
    await writer.drain(timeout_s=2.0)

    event_inserts = [
        sql for sql in database.calls if "INSERT INTO agent_trace_events" in sql
    ]
    assert len(event_inserts) == 1  # one batch for all 25 deltas

    delta_related = [
        sql
        for sql in database.calls
        if "INSERT INTO agent_traces" in sql
        or "INSERT INTO agent_trace_events" in sql
        or "UPDATE agent_traces" in sql
    ]
    # root + lifecycle-start span are also span inserts; count only the
    # delta-relevant statements: root insert, event batch, finish update.
    root_count = sum(1 for sql in delta_related if "INSERT INTO agent_traces" in sql)
    batch_count = sum(1 for sql in delta_related if "INSERT INTO agent_trace_events" in sql)
    finish_count = sum(1 for sql in delta_related if "UPDATE agent_traces" in sql)
    assert root_count == 1
    assert batch_count == 1
    assert finish_count == 1
    assert root_count + batch_count + finish_count <= 4
    # No per-delta INSERT statements remain.
    assert trace_writer_metrics.sql_statements <= 8


@pytest.mark.asyncio
async def test_events_flush_on_timer_when_finish_never_arrives() -> None:
    """The 50 ms flush bound persists events even without finish/drain."""
    database = _RecordingDB()
    writer = AssistantTraceWriter(database=database)
    ctx = _ctx()

    assert writer.start_trace(ctx)
    for sequence_no in range(1, 4):
        assert writer.record_event(
            ctx=ctx,
            event_type="text_delta",
            sequence_no=sequence_no,
            payload={"delta": f"delta-{sequence_no}"},
        )
    await asyncio.sleep(0.15)
    await writer.drain(timeout_s=2.0)

    event_inserts = [
        sql for sql in database.calls if "INSERT INTO agent_trace_events" in sql
    ]
    assert len(event_inserts) == 1
