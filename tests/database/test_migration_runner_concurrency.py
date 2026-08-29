"""Cross-process PostgreSQL proof for canonical migration serialization."""

from __future__ import annotations

import asyncio
import os
import shutil
import uuid
from pathlib import Path
from typing import Any
from urllib.parse import quote

import asyncpg
import pytest
from dotenv import dotenv_values

from database import cli

ROOT = Path(__file__).resolve().parents[2]


def _postgres_config() -> dict[str, Any]:
    file_values = dotenv_values(os.environ.get("ENV_FILE") or ROOT / ".env")
    required = ("POSTGRES_USER", "POSTGRES_PASSWORD", "POSTGRES_DB", "POSTGRES_PORT")
    values = {key: os.environ.get(key) or file_values.get(key) for key in required}
    missing = [key for key, value in values.items() if not value]
    if missing:
        pytest.fail(f"local PostgreSQL test configuration missing keys: {', '.join(missing)}")
    return {
        "host": os.environ.get("POSTGRES_HOST") or file_values.get("POSTGRES_HOST") or "127.0.0.1",
        "port": int(str(values["POSTGRES_PORT"])),
        "user": str(values["POSTGRES_USER"]),
        "password": str(values["POSTGRES_PASSWORD"]),
        "database": str(values["POSTGRES_DB"]),
    }


def _dsn(config: dict[str, Any], database: str) -> str:
    user = quote(str(config["user"]), safe="")
    password = quote(str(config["password"]), safe="")
    return f"postgresql://{user}:{password}@{config['host']}:{config['port']}/{database}"


def _write_shell_fixture(
    project: Path,
    config: dict[str, Any],
    database: str,
) -> tuple[Path, Path, Path]:
    script_dir = project / "scripts" / "new"
    migrations_dir = project / "database" / "migrations"
    script_dir.mkdir(parents=True)
    migrations_dir.mkdir(parents=True)
    shutil.copy2(ROOT / "scripts/new/common.sh", script_dir / "common.sh")
    shutil.copy2(ROOT / "scripts/new/migrate.sh", script_dir / "migrate.sh")

    (project / "database/schema.sql").write_text(
        """
        CREATE TABLE public.services (service_id TEXT PRIMARY KEY);
        CREATE TABLE public.datasets (dataset_id TEXT PRIMARY KEY);
        CREATE TABLE public.documents (document_id TEXT PRIMARY KEY);
        CREATE TABLE public.segments (segment_id TEXT PRIMARY KEY);
        """,
        encoding="utf-8",
    )
    migration = migrations_dir / "100_lock_probe.sql"
    migration.write_text(
        """
        BEGIN;
        CREATE SCHEMA IF NOT EXISTS knowledge;
        SET LOCAL search_path = knowledge, gateway, assistant, public;
        CREATE TABLE IF NOT EXISTS migration_lock_probe (
            singleton INTEGER PRIMARY KEY,
            execution_count INTEGER NOT NULL
        );
        INSERT INTO migration_lock_probe (singleton, execution_count) VALUES (1, 1);
        SELECT pg_sleep(0.5);
        COMMIT;
        """,
        encoding="utf-8",
    )
    env_file = project / ".env"
    env_file.write_text(
        "\n".join(
            (
                f"POSTGRES_HOST={config['host']}",
                f"POSTGRES_PORT={config['port']}",
                f"POSTGRES_USER={config['user']}",
                f"POSTGRES_PASSWORD={config['password']}",
                f"POSTGRES_DB={database}",
            )
        )
        + "\n",
        encoding="utf-8",
    )
    env_file.chmod(0o600)
    return script_dir / "migrate.sh", env_file, migration


def _shell_environment(
    config: dict[str, Any],
    database: str,
) -> dict[str, str]:
    client_container = os.environ.get("POSTGRES_CLIENT_CONTAINER")
    if client_container is None and shutil.which("psql") is None:
        pytest.fail("psql is required for the shell migration lock gate")
    return {
        **os.environ,
        "POSTGRES_HOST": str(config["host"]),
        "POSTGRES_PORT": str(config["port"]),
        "POSTGRES_USER": str(config["user"]),
        "POSTGRES_PASSWORD": str(config["password"]),
        "POSTGRES_DB": database,
        "POSTGRES_CONTAINER": client_container or f"no-shared-postgres-{uuid.uuid4().hex}",
    }


@pytest.mark.asyncio
async def test_python_and_shell_runners_serialize_then_reread_ledger(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    config = _postgres_config()
    database_name = f"migration_lock_{uuid.uuid4().hex}"
    admin = await asyncpg.connect(**config)
    await admin.execute(f'CREATE DATABASE "{database_name}"')
    shell_script, env_file, migration = _write_shell_fixture(
        tmp_path / "project",
        config,
        database_name,
    )
    dsn = _dsn(config, database_name)
    monkeypatch.setattr(cli, "get_dsn", lambda: dsn)
    monkeypatch.setattr(
        cli,
        "discover_migrations",
        lambda _migrations_dir=cli.MIGRATIONS_DIR: [("100", "Lock Probe", migration)],
    )

    shell_env = _shell_environment(config, database_name)

    async def run_shell() -> None:
        process = await asyncio.create_subprocess_exec(
            "bash",
            str(shell_script),
            "--auto",
            "--env",
            str(env_file),
            env=shell_env,
            stdout=asyncio.subprocess.PIPE,
            stderr=asyncio.subprocess.PIPE,
        )
        _stdout, _stderr = await process.communicate()
        assert process.returncode == 0, (
            f"shell migration runner failed with exit code {process.returncode}; "
            "output is intentionally redacted"
        )

    try:
        await asyncio.gather(cli.cmd_migrate(), run_shell())

        conn = await asyncpg.connect(dsn)
        try:
            assert (
                await conn.fetchval("SELECT execution_count FROM knowledge.migration_lock_probe")
                == 1
            )
            assert (
                await conn.fetchval(
                    """
                SELECT count(*) FROM public.schema_migrations
                WHERE filename = '100_lock_probe.sql'
                """
                )
                == 1
            )
            assert (
                await conn.fetchval(
                    """
                SELECT count(*) FROM pg_stat_activity
                WHERE application_name LIKE 'ai_gateway_migrate_%'
                """
                )
                == 0
            )
        finally:
            await conn.close()
    finally:
        await admin.execute(f'DROP DATABASE "{database_name}"')
        await admin.close()


@pytest.mark.parametrize("signal_name", ("terminate", "kill"))
@pytest.mark.asyncio
async def test_shell_lock_session_releases_on_runner_exit(
    tmp_path: Path,
    signal_name: str,
) -> None:
    config = _postgres_config()
    database_name = f"migration_signal_{uuid.uuid4().hex}"
    admin = await asyncpg.connect(**config)
    await admin.execute(f'CREATE DATABASE "{database_name}"')
    shell_script, env_file, _migration = _write_shell_fixture(
        tmp_path / signal_name,
        config,
        database_name,
    )
    holder = shell_script.with_name("hold-lock.sh")
    holder.write_text(
        """#!/bin/bash
set -euo pipefail
source "$(dirname "$0")/common.sh"
ENV_FILE="$1"
load_env
acquire_migration_advisory_lock
trap release_migration_advisory_lock EXIT
trap 'exit 130' INT
trap 'exit 143' TERM
while true; do sleep 0.2; done
""",
        encoding="utf-8",
    )
    process = await asyncio.create_subprocess_exec(
        "bash",
        str(holder),
        str(env_file),
        env=_shell_environment(config, database_name),
        stdout=asyncio.subprocess.PIPE,
        stderr=asyncio.subprocess.PIPE,
    )
    dsn = _dsn(config, database_name)
    conn: asyncpg.Connection | None = None
    held = False
    try:
        conn = await asyncpg.connect(dsn)
        for _attempt in range(100):
            held = bool(
                await conn.fetchval(
                    """
                    SELECT EXISTS (
                        SELECT 1
                        FROM pg_locks AS l
                        JOIN pg_stat_activity AS a ON a.pid = l.pid
                        WHERE a.application_name LIKE 'ai_gateway_migrate_%'
                          AND l.locktype = 'advisory'
                          AND l.granted
                          AND l.classid::bigint = $1
                          AND l.objid::bigint = $2
                          AND l.objsubid = 2
                    )
                    """,
                    cli.MIGRATION_ADVISORY_LOCK_NAMESPACE,
                    cli.MIGRATION_ADVISORY_LOCK_ID,
                )
            )
            if held:
                break
            await asyncio.sleep(0.05)
        assert held, "shell runner did not acquire the canonical migration lock"

        getattr(process, signal_name)()
        await asyncio.wait_for(process.wait(), timeout=5)
        for _attempt in range(100):
            held = bool(
                await conn.fetchval(
                    """
                    SELECT EXISTS (
                        SELECT 1
                        FROM pg_locks AS l
                        WHERE l.locktype = 'advisory'
                          AND l.granted
                          AND l.classid::bigint = $1
                          AND l.objid::bigint = $2
                          AND l.objsubid = 2
                    )
                    """,
                    cli.MIGRATION_ADVISORY_LOCK_NAMESPACE,
                    cli.MIGRATION_ADVISORY_LOCK_ID,
                )
            )
            if not held:
                break
            await asyncio.sleep(0.05)
        assert not held
    finally:
        if process.returncode is None:
            process.kill()
            await process.wait()
        if conn is not None:
            await conn.close()
        await admin.execute(f'DROP DATABASE "{database_name}"')
        await admin.close()
