from __future__ import annotations

import asyncio
import hashlib
import json
import os
import uuid
from collections.abc import AsyncIterator
from pathlib import Path

import asyncpg
import pytest
import pytest_asyncio
from dotenv import dotenv_values

ROOT = Path(__file__).resolve().parents[2]
MIGRATION = ROOT / "database" / "migrations" / "089_codex_runtime_thread_store.sql"


def _postgres_config() -> dict[str, object]:
    file_values = dotenv_values(ROOT / ".env")
    required = ("POSTGRES_USER", "POSTGRES_PASSWORD", "POSTGRES_DB", "POSTGRES_PORT")
    values = {key: os.environ.get(key) or file_values.get(key) for key in required}
    missing = [key for key, value in values.items() if not value]
    if missing:
        pytest.fail(f"local PostgreSQL test configuration missing keys: {', '.join(missing)}")
    return {
        "host": "127.0.0.1",
        "port": int(str(values["POSTGRES_PORT"])),
        "user": str(values["POSTGRES_USER"]),
        "password": str(values["POSTGRES_PASSWORD"]),
        "database": str(values["POSTGRES_DB"]),
    }


@pytest_asyncio.fixture
async def codex_runtime_pool() -> AsyncIterator[asyncpg.Pool]:
    config = _postgres_config()
    schema_name = f"codex_runtime_test_{uuid.uuid4().hex}"
    admin = await asyncpg.connect(**config)
    await admin.execute(f'CREATE SCHEMA "{schema_name}"')
    await admin.close()

    pool = await asyncpg.create_pool(
        **config,
        min_size=1,
        max_size=8,
        server_settings={"search_path": f'"{schema_name}",public'},
    )
    try:
        async with pool.acquire() as conn:
            await conn.execute(
                """
                CREATE TABLE sessions (
                    session_id VARCHAR(255) PRIMARY KEY,
                    service_id VARCHAR(255),
                    user_id VARCHAR(255),
                    tenant_id VARCHAR(255),
                    state JSONB NOT NULL DEFAULT '{}'::jsonb,
                    history JSONB NOT NULL DEFAULT '[]'::jsonb,
                    metadata JSONB NOT NULL DEFAULT '{}'::jsonb,
                    config JSONB NOT NULL DEFAULT '{}'::jsonb,
                    status VARCHAR(50) NOT NULL DEFAULT 'active',
                    expires_at TIMESTAMPTZ,
                    created_at TIMESTAMPTZ NOT NULL DEFAULT NOW(),
                    updated_at TIMESTAMPTZ NOT NULL DEFAULT NOW()
                );
                CREATE TABLE assistant_runs (
                    run_id UUID PRIMARY KEY,
                    tenant_id VARCHAR(255) NOT NULL,
                    user_id VARCHAR(255) NOT NULL,
                    session_id VARCHAR(255) NOT NULL,
                    status VARCHAR(32) NOT NULL DEFAULT 'running',
                    engine VARCHAR(32) NOT NULL DEFAULT 'agent_loop',
                    execution_profile VARCHAR(16) NOT NULL DEFAULT 'safe',
                    memory_mode VARCHAR(16) NOT NULL DEFAULT 'auto',
                    os_agent_enabled BOOLEAN NOT NULL DEFAULT FALSE,
                    request_preview TEXT NOT NULL DEFAULT '',
                    usage JSONB NOT NULL DEFAULT '{}'::jsonb,
                    error TEXT,
                    started_at TIMESTAMPTZ NOT NULL DEFAULT NOW(),
                    finished_at TIMESTAMPTZ,
                    created_at TIMESTAMPTZ NOT NULL DEFAULT NOW(),
                    updated_at TIMESTAMPTZ NOT NULL DEFAULT NOW()
                );
                CREATE OR REPLACE FUNCTION update_assistant_gateway_timestamp()
                RETURNS TRIGGER AS $$
                BEGIN
                    NEW.updated_at = NOW();
                    RETURN NEW;
                END;
                $$ LANGUAGE plpgsql;
                """
            )
            sql = MIGRATION.read_text(encoding="utf-8")
            await conn.execute(sql)
            await conn.execute(sql)
        yield pool
    finally:
        await pool.close()
        admin = await asyncpg.connect(**config)
        await admin.execute(f'DROP SCHEMA "{schema_name}" CASCADE')
        await admin.close()


def test_migration_is_additive_and_has_atomic_append_contract() -> None:
    sql = MIGRATION.read_text(encoding="utf-8")
    upper = sql.upper()
    assert "DROP TABLE" not in upper
    assert "TRUNCATE" not in upper
    assert "CREATE OR REPLACE FUNCTION APPEND_ASSISTANT_RUNTIME_ITEM" in upper
    assert "FOR UPDATE" in upper
    assert "ASSISTANT_RUNTIME_EVENT_KEY_CONFLICT" in upper
    assert "ASSISTANT_RUNTIME_THREAD_SCOPE_MISMATCH" in upper
    assert "ASSISTANT_RUNTIME_MEMBER_SCOPE_MISMATCH" in upper
    assert "ASSISTANT_RUNTIME_ASSIGNMENT_IMMUTABLE" in upper


@pytest.mark.asyncio
async def test_session_runtime_assignment_is_scope_bound_and_immutable(
    codex_runtime_pool: asyncpg.Pool,
) -> None:
    tenant_id = "tenant-assignment"
    user_id = "user-assignment"
    session_id = f"session-{uuid.uuid4()}"
    async with codex_runtime_pool.acquire() as conn:
        await conn.execute(
            """
            INSERT INTO sessions (session_id, service_id, user_id, tenant_id)
            VALUES ($1, '__builtin_assistant__', $2, $3)
            """,
            session_id,
            user_id,
            tenant_id,
        )
        await conn.execute(
            """
            INSERT INTO assistant_session_runtime_assignments (
                tenant_id, user_id, session_id, runtime_owner
            ) VALUES ($1, $2, $3, 'python_control')
            """,
            tenant_id,
            user_id,
            session_id,
        )
        assignment = await conn.fetchrow(
            """
            SELECT runtime_owner, kernel_revision
            FROM assistant_session_runtime_assignments
            WHERE tenant_id = $1 AND user_id = $2 AND session_id = $3
            """,
            tenant_id,
            user_id,
            session_id,
        )
        assert dict(assignment) == {
            "runtime_owner": "python_control",
            "kernel_revision": None,
        }

        with pytest.raises(asyncpg.ObjectNotInPrerequisiteStateError):
            await conn.execute(
                """
                UPDATE assistant_session_runtime_assignments
                SET runtime_owner = 'codex_candidate', kernel_revision = 'fork-sha'
                WHERE tenant_id = $1 AND user_id = $2 AND session_id = $3
                """,
                tenant_id,
                user_id,
                session_id,
            )
        invalid_session_id = f"session-{uuid.uuid4()}"
        await conn.execute(
            """
            INSERT INTO sessions (session_id, service_id, user_id, tenant_id)
            VALUES ($1, '__builtin_assistant__', $2, $3)
            """,
            invalid_session_id,
            user_id,
            tenant_id,
        )
        with pytest.raises(asyncpg.CheckViolationError):
            await conn.execute(
                """
                INSERT INTO assistant_session_runtime_assignments (
                    tenant_id, user_id, session_id, runtime_owner
                ) VALUES ($1, $2, $3, 'codex_candidate')
                """,
                tenant_id,
                user_id,
                invalid_session_id,
            )


async def _seed_thread(pool: asyncpg.Pool) -> tuple[uuid.UUID, str, str, str]:
    thread_id = uuid.uuid4()
    tenant_id = "tenant-a"
    user_id = "user-a"
    session_id = f"session-{uuid.uuid4()}"
    async with pool.acquire() as conn:
        await conn.execute(
            """
            INSERT INTO sessions (session_id, service_id, user_id, tenant_id)
            VALUES ($1, '__builtin_assistant__', $2, $3)
            """,
            session_id,
            user_id,
            tenant_id,
        )
        await conn.execute(
            """
            INSERT INTO assistant_runtime_threads (
                runtime_thread_id, tenant_id, user_id, session_id
            ) VALUES ($1, $2, $3, $4)
            """,
            thread_id,
            tenant_id,
            user_id,
            session_id,
        )
        await conn.execute(
            """
            INSERT INTO assistant_runtime_thread_members (
                kernel_thread_id, runtime_thread_id, kernel_session_id,
                relation_kind, tenant_id, user_id, session_id
            ) VALUES ($1, $1, $1, 'root', $2, $3, $4)
            """,
            thread_id,
            tenant_id,
            user_id,
            session_id,
        )
    return thread_id, tenant_id, user_id, session_id


async def _append(
    pool: asyncpg.Pool,
    *,
    thread_id: uuid.UUID,
    tenant_id: str,
    user_id: str,
    session_id: str,
    event_id: uuid.UUID,
    event_key: str,
    payload: dict[str, object],
) -> int:
    encoded = json.dumps(payload, ensure_ascii=False, separators=(",", ":"), sort_keys=True)
    payload_hash = hashlib.sha256(encoded.encode("utf-8")).hexdigest()
    async with pool.acquire() as conn:
        return int(
            await conn.fetchval(
                """
                SELECT append_assistant_runtime_item(
                    $1, $1, $2, $3, $4, $5, $6, $7, $8, $9, $10, $11,
                    $12::jsonb, $13
                )
                """,
                thread_id,
                tenant_id,
                user_id,
                session_id,
                event_id,
                event_key,
                "turn-1",
                "item-1",
                "item/started",
                "agent_message",
                "in_progress",
                encoded,
                payload_hash,
            )
        )


@pytest.mark.asyncio
async def test_atomic_append_is_idempotent_gap_free_and_scope_bound(
    codex_runtime_pool: asyncpg.Pool,
) -> None:
    thread_id, tenant_id, user_id, session_id = await _seed_thread(codex_runtime_pool)
    event_id = uuid.uuid4()
    payload = {"text": "hello"}

    first = await _append(
        codex_runtime_pool,
        thread_id=thread_id,
        tenant_id=tenant_id,
        user_id=user_id,
        session_id=session_id,
        event_id=event_id,
        event_key="event-1",
        payload=payload,
    )
    replay = await _append(
        codex_runtime_pool,
        thread_id=thread_id,
        tenant_id=tenant_id,
        user_id=user_id,
        session_id=session_id,
        event_id=event_id,
        event_key="event-1",
        payload=payload,
    )
    assert first == replay == 1

    with pytest.raises(asyncpg.UniqueViolationError):
        await _append(
            codex_runtime_pool,
            thread_id=thread_id,
            tenant_id=tenant_id,
            user_id=user_id,
            session_id=session_id,
            event_id=uuid.uuid4(),
            event_key="event-1",
            payload={"text": "different"},
        )

    with pytest.raises(asyncpg.InsufficientPrivilegeError):
        await _append(
            codex_runtime_pool,
            thread_id=thread_id,
            tenant_id="tenant-b",
            user_id=user_id,
            session_id=session_id,
            event_id=uuid.uuid4(),
            event_key="cross-tenant",
            payload=payload,
        )

    concurrent = await asyncio.gather(
        *(
            _append(
                codex_runtime_pool,
                thread_id=thread_id,
                tenant_id=tenant_id,
                user_id=user_id,
                session_id=session_id,
                event_id=uuid.uuid4(),
                event_key=f"parallel-{index}",
                payload={"index": index},
            )
            for index in range(20)
        )
    )
    assert sorted(concurrent) == list(range(2, 22))

    async with codex_runtime_pool.acquire() as conn:
        sequences = await conn.fetch(
            """
            SELECT sequence FROM assistant_runtime_items
            WHERE runtime_thread_id = $1 ORDER BY sequence
            """,
            thread_id,
        )
        assert [row["sequence"] for row in sequences] == list(range(1, 22))
        assert (
            await conn.fetchval(
                "SELECT last_sequence FROM assistant_runtime_threads WHERE runtime_thread_id = $1",
                thread_id,
            )
            == 21
        )


@pytest.mark.asyncio
async def test_snapshots_items_and_run_identity_fail_closed(
    codex_runtime_pool: asyncpg.Pool,
) -> None:
    thread_id, tenant_id, user_id, session_id = await _seed_thread(codex_runtime_pool)
    run_id = uuid.uuid4()
    snapshot_id = uuid.uuid4()
    snapshot = {"schema_version": "agent-runtime-snapshot/v1", "permissions": {}}
    snapshot_text = json.dumps(snapshot, separators=(",", ":"), sort_keys=True)
    snapshot_hash = hashlib.sha256(snapshot_text.encode()).hexdigest()

    async with codex_runtime_pool.acquire() as conn:
        async with conn.transaction():
            await conn.execute(
                """
                INSERT INTO assistant_runtime_snapshots (
                    snapshot_id, runtime_thread_id, run_id, tenant_id, user_id,
                    session_id, schema_version, snapshot, snapshot_sha256
                ) VALUES ($1, $2, $3, $4, $5, $6, $7, $8::jsonb, $9)
                """,
                snapshot_id,
                thread_id,
                run_id,
                tenant_id,
                user_id,
                session_id,
                "agent-runtime-snapshot/v1",
                snapshot_text,
                snapshot_hash,
            )
            await conn.execute(
                """
                INSERT INTO assistant_runs (
                    run_id, tenant_id, user_id, session_id, engine,
                    harness_thread_id, harness_turn_id, runtime_snapshot_id,
                    kernel_revision, capability_revision
                ) VALUES (
                    $1, $2, $3, $4, 'codex_harness', $5, 'turn-1', $6, 'fork-sha', 1
                )
                """,
                run_id,
                tenant_id,
                user_id,
                session_id,
                thread_id,
                snapshot_id,
            )

        await conn.execute(
            """
            INSERT INTO assistant_runtime_snapshot_revocations (
                snapshot_id, tenant_id, user_id, session_id, reason_code, revoked_by
            ) VALUES ($1, $2, $3, $4, 'lease_revoked', 'test-operator')
            """,
            snapshot_id,
            tenant_id,
            user_id,
            session_id,
        )
        with pytest.raises(asyncpg.ObjectNotInPrerequisiteStateError):
            await conn.execute(
                """
                UPDATE assistant_runtime_snapshot_revocations
                SET reason_code = 'changed' WHERE snapshot_id = $1
                """,
                snapshot_id,
            )
        with pytest.raises(asyncpg.ObjectNotInPrerequisiteStateError):
            await conn.execute(
                "DELETE FROM assistant_runtime_snapshots WHERE snapshot_id = $1",
                snapshot_id,
            )

        with pytest.raises(asyncpg.CheckViolationError):
            await conn.execute(
                """
                INSERT INTO assistant_runs (
                    run_id, tenant_id, user_id, session_id, engine
                ) VALUES ($1, $2, $3, $4, 'codex_harness')
                """,
                uuid.uuid4(),
                tenant_id,
                user_id,
                session_id,
            )

    event_id = uuid.uuid4()
    await _append(
        codex_runtime_pool,
        thread_id=thread_id,
        tenant_id=tenant_id,
        user_id=user_id,
        session_id=session_id,
        event_id=event_id,
        event_key="immutable-item",
        payload={"text": "immutable"},
    )
    async with codex_runtime_pool.acquire() as conn:
        with pytest.raises(asyncpg.ObjectNotInPrerequisiteStateError):
            await conn.execute(
                "UPDATE assistant_runtime_items SET status = 'completed' WHERE event_id = $1",
                event_id,
            )
