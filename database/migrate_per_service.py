#!/usr/bin/env python
"""Phase-6 per-service migration runner.

Replaces the legacy ``DatabaseStorage(auto_init=True)`` startup hook with a
standalone job. Run by the ``migrate`` compose service (or by hand from the
deploy host) after a deploy that needs schema changes.

Discovery rules:
  * ``database/migrations/per_service/_global/*.sql`` — runs first, in name
    order. Used for cross-cutting setup (schemas, search_path, role grants,
    schema-split moves).
  * ``database/migrations/per_service/<service>/*.sql`` — runs in service
    order: gateway, assistant, knowledge. Each service block runs in name
    order. Use this for service-owned table changes going forward.

Idempotency:
  Each ``.sql`` file is recorded in ``public.schema_migrations_meta`` keyed by
  ``"<service>:<filename>"`` so a re-run is a fast no-op. ``meta.applied_at``
  reflects the first successful application.

Failure mode:
  Hard fail at the first migration that errors. Does NOT skip. Operator must
  inspect, fix, and re-run; subsequent successes still record.

Connection:
  Uses ``DATABASE_URL`` env var. ``GATEWAY_DATABASE__DSN`` is honored as a
  fallback so you can re-use the gateway's existing prod env.
"""

from __future__ import annotations

import asyncio
import logging
import os
import sys
from collections.abc import Iterable
from pathlib import Path

try:
    import asyncpg
except ImportError:
    print("asyncpg required: pip install asyncpg", file=sys.stderr)
    sys.exit(1)

logger = logging.getLogger("migrate")
logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(message)s")

ROOT = Path(__file__).resolve().parent
PER_SVC = ROOT / "migrations" / "per_service"
SERVICE_ORDER = ("_global", "gateway", "assistant", "knowledge")
MIGRATION_ADVISORY_LOCK_NAMESPACE = 1_095_781_959
MIGRATION_ADVISORY_LOCK_ID = 1
LEDGER_DDL = """
CREATE TABLE IF NOT EXISTS public.schema_migrations_meta (
    name        TEXT PRIMARY KEY,
    applied_at  TIMESTAMPTZ NOT NULL DEFAULT now(),
    notes       TEXT
);
"""


def _dsn() -> str:
    for var in ("DATABASE_URL", "GATEWAY_DATABASE__DSN"):
        v = os.getenv(var)
        if v:
            return v
    print("Set DATABASE_URL (or GATEWAY_DATABASE__DSN).", file=sys.stderr)
    sys.exit(2)


def _files_for(service: str) -> list[Path]:
    d = PER_SVC / service
    if not d.is_dir():
        return []
    return sorted(p for p in d.glob("*.sql") if p.is_file())


async def _already_applied(conn: asyncpg.Connection, ledger_key: str) -> bool:
    row = await conn.fetchrow(
        "SELECT 1 FROM public.schema_migrations_meta WHERE name = $1",
        ledger_key,
    )
    return row is not None


async def _record(conn: asyncpg.Connection, ledger_key: str, sql_path: Path) -> None:
    await conn.execute(
        "INSERT INTO public.schema_migrations_meta(name, notes) VALUES ($1, $2) "
        "ON CONFLICT (name) DO NOTHING",
        ledger_key,
        f"file={sql_path.relative_to(ROOT)}",
    )


async def _run_one(conn: asyncpg.Connection, service: str, sql_path: Path) -> None:
    ledger_key = f"{service}:{sql_path.name}"
    if await _already_applied(conn, ledger_key):
        logger.info("skip %-10s %-50s (already applied)", service, sql_path.name)
        return

    sql = sql_path.read_text(encoding="utf-8")
    logger.info("apply %-10s %s", service, sql_path.name)
    # Run each migration file in its own transaction. Some files (e.g.
    # 002_move_tables.sql) wrap their own DO blocks; that's fine — pg
    # handles nested savepoints implicitly.
    async with conn.transaction():
        await conn.execute(sql)
        await _record(conn, ledger_key, sql_path)


async def _run_service_chain(conn: asyncpg.Connection, service: str) -> int:
    files = _files_for(service)
    if not files:
        return 0
    for f in files:
        await _run_one(conn, service, f)
    return len(files)


async def main(argv: Iterable[str] | None = None) -> int:
    argv = list(argv or sys.argv[1:])

    only = None
    if argv and argv[0] in {"-s", "--service"}:
        if len(argv) < 2:
            print("--service requires a name", file=sys.stderr)
            return 2
        only = argv[1]
        if only not in SERVICE_ORDER:
            print(f"unknown service {only!r}; choose from {SERVICE_ORDER}", file=sys.stderr)
            return 2

    dsn = _dsn()
    logger.info("connecting to %s", dsn.split("@", 1)[-1])
    conn = await asyncpg.connect(dsn)
    try:
        # Serialize the legal per-service track with both central runners.
        # Closing this session releases the lock even after a process crash.
        await conn.execute(
            "SELECT pg_advisory_lock($1::integer, $2::integer)",
            MIGRATION_ADVISORY_LOCK_NAMESPACE,
            MIGRATION_ADVISORY_LOCK_ID,
        )
        await conn.execute(LEDGER_DDL)
        total = 0
        for svc in SERVICE_ORDER:
            if only and svc != only:
                continue
            total += await _run_service_chain(conn, svc)
        logger.info("migration complete — %d file(s) processed", total)
    finally:
        await conn.close()
    return 0


if __name__ == "__main__":
    raise SystemExit(asyncio.run(main()))
