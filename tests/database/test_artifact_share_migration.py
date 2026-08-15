"""Artifact-share schema-split migration contracts."""

from __future__ import annotations

import json
import os
import uuid
from pathlib import Path
from urllib.parse import urlsplit, urlunsplit

import asyncpg
import pytest

ROOT = Path(__file__).resolve().parents[2]
ROOT_MIGRATION = ROOT / "database" / "migrations" / "083_artifact_shares.sql"
SERVICE_MIGRATION = (
    ROOT
    / "database"
    / "migrations"
    / "per_service"
    / "assistant"
    / "003_artifact_shares.sql"
)


def test_artifact_share_migrations_preserve_legacy_width_and_schema_ownership() -> None:
    root_sql = ROOT_MIGRATION.read_text(encoding="utf-8")
    service_sql = SERVICE_MIGRATION.read_text(encoding="utf-8")

    assert "VARCHAR(12)" not in root_sql
    assert "VARCHAR(20)" in root_sql
    assert "assistant.artifact_shares" in service_sql
    assert "FROM assistant.quiz_shares" in service_sql
    assert "FROM assistant.quiz_attempts" in service_sql
    assert "artifact_share_submitters" in service_sql
    assert "VARCHAR(20)" in service_sql
    assert "DROP TABLE" not in service_sql.upper()
    assert "TRUNCATE" not in service_sql.upper()


def _dsn_for_database(dsn: str, database_name: str) -> str:
    parsed = urlsplit(dsn)
    return urlunsplit(parsed._replace(path=f"/{database_name}"))


@pytest.mark.asyncio
async def test_artifact_share_migration_upgrades_split_postgres_idempotently() -> None:
    admin_dsn = os.getenv("ARTIFACT_SHARE_TEST_DATABASE_URL")
    enabled = os.getenv("ARTIFACT_SHARE_TEST_ENABLED") == "1"
    if not admin_dsn and not enabled:
        pytest.skip(
            "set ARTIFACT_SHARE_TEST_DATABASE_URL or ARTIFACT_SHARE_TEST_ENABLED=1 "
            "for the PostgreSQL migration gate"
        )

    connection_config: dict[str, object] | None = None
    if admin_dsn is None:
        required = ("POSTGRES_USER", "POSTGRES_PASSWORD", "POSTGRES_DB", "POSTGRES_PORT")
        missing = [name for name in required if not os.getenv(name)]
        if missing:
            pytest.fail(f"missing PostgreSQL migration-test settings: {', '.join(missing)}")
        connection_config = {
            "host": os.getenv("ARTIFACT_SHARE_TEST_HOST", "127.0.0.1"),
            "port": int(os.environ["POSTGRES_PORT"]),
            "user": os.environ["POSTGRES_USER"],
            "password": os.environ["POSTGRES_PASSWORD"],
            "database": os.environ["POSTGRES_DB"],
        }

    database_name = f"artifact_share_test_{uuid.uuid4().hex}"
    admin = await (
        asyncpg.connect(admin_dsn)
        if admin_dsn is not None
        else asyncpg.connect(**connection_config)
    )
    connection: asyncpg.Connection | None = None
    try:
        await admin.execute(f'CREATE DATABASE "{database_name}"')
        connection = await (
            asyncpg.connect(_dsn_for_database(admin_dsn, database_name))
            if admin_dsn is not None
            else asyncpg.connect(**{**connection_config, "database": database_name})
        )
        await connection.execute(
            """
            CREATE SCHEMA gateway;
            CREATE SCHEMA assistant;

            CREATE TABLE assistant.quizzes (
                id UUID PRIMARY KEY,
                tenant_id VARCHAR(64) NOT NULL,
                created_by VARCHAR(128) NOT NULL,
                title VARCHAR(500) NOT NULL,
                description TEXT,
                question_count INT NOT NULL,
                difficulty VARCHAR(20) NOT NULL
            );
            CREATE TABLE assistant.quiz_questions (
                id UUID PRIMARY KEY,
                quiz_id UUID NOT NULL REFERENCES assistant.quizzes(id),
                question_num INT NOT NULL,
                question_type VARCHAR(20) NOT NULL,
                question_text TEXT NOT NULL,
                options JSONB NOT NULL,
                correct_answer JSONB NOT NULL,
                explanation TEXT
            );
            CREATE TABLE assistant.quiz_shares (
                id UUID PRIMARY KEY,
                quiz_id UUID NOT NULL REFERENCES assistant.quizzes(id),
                share_code VARCHAR(20) NOT NULL UNIQUE,
                created_by VARCHAR(128) NOT NULL,
                is_active BOOLEAN NOT NULL DEFAULT TRUE,
                max_attempts INT,
                expires_at TIMESTAMPTZ,
                require_name BOOLEAN NOT NULL DEFAULT FALSE,
                time_limit_minutes INT,
                created_at TIMESTAMPTZ NOT NULL DEFAULT NOW()
            );
            CREATE TABLE assistant.quiz_attempts (
                id UUID PRIMARY KEY,
                quiz_id UUID NOT NULL REFERENCES assistant.quizzes(id),
                share_id UUID,
                display_name VARCHAR(200)
            );

            INSERT INTO assistant.quizzes
                (id, tenant_id, created_by, title, description,
                 question_count, difficulty)
            VALUES
                ('11111111-1111-4111-8111-111111111111', 'tenant-a', 'alex',
                 'Legacy quiz', 'kept', 1, 'medium');
            INSERT INTO assistant.quiz_questions
                (id, quiz_id, question_num, question_type, question_text,
                 options, correct_answer, explanation)
            VALUES
                ('22222222-2222-4222-8222-222222222222',
                 '11111111-1111-4111-8111-111111111111', 1, 'mc_single',
                 'Question?', '[{"label":"A","text":"Answer"}]', '["A"]', 'Why');
            INSERT INTO assistant.quiz_shares
                (id, quiz_id, share_code, created_by, require_name)
            VALUES
                ('33333333-3333-4333-8333-333333333333',
                 '11111111-1111-4111-8111-111111111111',
                 'abcdefghijklmnopqrst', 'alex', TRUE);
            INSERT INTO assistant.quiz_attempts
                (id, quiz_id, share_id, display_name)
            VALUES
                ('44444444-4444-4444-8444-444444444444',
                 '11111111-1111-4111-8111-111111111111',
                 '33333333-3333-4333-8333-333333333333', 'Legacy User');
            """
        )

        sql = SERVICE_MIGRATION.read_text(encoding="utf-8")
        await connection.execute(sql)
        await connection.execute(sql)

        row = await connection.fetchrow(
            "SELECT * FROM assistant.artifact_shares WHERE share_code = $1",
            "abcdefghijklmnopqrst",
        )
        assert row is not None
        assert row["tenant_id"] == "tenant-a"
        assert row["attempt_count"] == 1
        payload = json.loads(row["payload"])
        answer_keys = json.loads(row["answer_keys"])
        assert payload["questions"][0]["question_text"] == "Question?"
        assert answer_keys[0]["correct_answer"] == ["A"]

        width = await connection.fetchval(
            """
            SELECT character_maximum_length
            FROM information_schema.columns
            WHERE table_schema = 'assistant'
              AND table_name = 'artifact_shares'
              AND column_name = 'share_code'
            """
        )
        assert width == 20
        assert await connection.fetchval(
            "SELECT count(*) FROM assistant.artifact_share_submitters"
        ) == 1
    finally:
        if connection is not None:
            await connection.close()
        await admin.execute(
            "SELECT pg_terminate_backend(pid) FROM pg_stat_activity "
            "WHERE datname = $1 AND pid <> pg_backend_pid()",
            database_name,
        )
        await admin.execute(f'DROP DATABASE IF EXISTS "{database_name}"')
        await admin.close()
