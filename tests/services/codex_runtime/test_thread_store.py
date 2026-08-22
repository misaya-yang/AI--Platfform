from __future__ import annotations

import pytest

from src.services.codex_runtime.thread_store import CodexThreadStore, ThreadStoreError


class _Database:
    def __init__(self) -> None:
        self.thread: dict[str, object] | None = None
        self.events: list[dict[str, object]] = []

    async def fetchrow(self, query: str, *args):
        if "FROM assistant_runtime_snapshots" in query:
            assert "snapshot->>'kernel_revision'" in query
            return {
                "snapshot": {
                    "kernel_revision": "kernel-1",
                    "reasoning": {"requested_option": "auto", "effective_option": "minimal"},
                },
                "capability_revision": 7,
                "kernel_revision": "kernel-1",
            }
        if "ensure_assistant_runtime_thread" in query:
            if self.thread is None:
                self.thread = {
                    "runtime_thread_id": args[0], "tenant_id": args[1],
                    "user_id": args[2], "session_id": args[3],
                    "kernel_owner": "codex", "source_kind": "native",
                    "import_status": "not_required", "last_sequence": 0,
                }
            return {"ok": True}
        if "import_assistant_legacy_session" in query:
            if self.thread and self.thread["import_status"] == "ready":
                return {"import_status": "ready"}
            self.thread = {
                "runtime_thread_id": args[0], "tenant_id": args[1],
                "user_id": args[2], "session_id": args[3],
                "kernel_owner": "codex", "source_kind": "legacy_import",
                "import_status": "ready", "last_sequence": 1,
            }
            return {"import_status": "ready"}
        if "FROM assistant_runtime_threads" in query and "session_id = $3" in query:
            return self.thread
        if "FROM assistant_runtime_threads" in query:
            return self.thread
        if "append_assistant_runtime_item" in query:
            self.events.append({
                "sequence": len(self.events) + 1,
                "event_id": args[5], "event_key": args[6], "turn_id": args[7],
                "item_id": args[8], "event_type": args[9], "item_type": args[10],
                "status": args[11], "payload": args[12],
            })
            if self.thread:
                self.thread["last_sequence"] = len(self.events)
            return {"sequence": len(self.events)}
        raise AssertionError(query)

    async def fetch(self, _query: str, *_args):
        return self.events


@pytest.mark.asyncio
async def test_legacy_import_is_idempotent_and_scoped() -> None:
    db = _Database()
    store = CodexThreadStore(db)
    first = await store.import_legacy(
        tenant_id="tenant-a", user_id="user-a", session_id="session-a"
    )
    second = await store.import_legacy(
        tenant_id="tenant-a", user_id="user-a", session_id="session-a",
        runtime_thread_id=first.runtime_thread_id,
    )
    assert first == second
    assert first.source_kind == "legacy_import"
    assert first.import_status == "ready"


@pytest.mark.asyncio
async def test_append_event_uses_stable_event_key_and_cursor() -> None:
    db = _Database()
    store = CodexThreadStore(db)
    thread = await store.ensure_native(
        tenant_id="tenant-a", user_id="user-a", session_id="session-a"
    )
    sequence = await store.append_event(
        thread=thread, event_key="turn-1:item-1", event_type="text",
        item_id="item-1", payload={"text": "ok"}, status="completed",
    )
    assert sequence == 1
    assert (await store.events(
        tenant_id="tenant-a", user_id="user-a",
        runtime_thread_id=thread.runtime_thread_id, after_sequence=0, limit=10,
    ))[0]["event_key"] == "turn-1:item-1"


def test_thread_store_error_has_stable_http_contract() -> None:
    error = ThreadStoreError("CODEX_RUNTIME_IMPORT_IN_FLIGHT", status_code=409)
    assert error.code == "CODEX_RUNTIME_IMPORT_IN_FLIGHT"
    assert error.status_code == 409


@pytest.mark.asyncio
async def test_turn_metadata_reads_kernel_revision_from_immutable_snapshot() -> None:
    store = CodexThreadStore(_Database())
    metadata = await store.turn_metadata(
        tenant_id="tenant-a",
        user_id="user-a",
        session_id="session-a",
        runtime_thread_id="00000000-0000-0000-0000-000000000001",
        turn_id="00000000-0000-0000-0000-000000000002",
    )
    assert metadata is not None
    assert metadata["kernel_revision"] == "kernel-1"
