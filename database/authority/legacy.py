"""Native executor for the immutable pre-baseline (legacy) migration chain.

The authority itself executes the historical ``002…112`` files and the
per-service track; it no longer delegates to ``database/cli.py`` or
``database/migrate_per_service.py``.  Those entrypoints now delegate here,
so every writer funnels through one code path under one advisory lock.

Semantics preserved from the historical runners (verified against
``database/cli.py`` and ``scripts/new/migrate.sh`` before the delegation
switch):

* discovery recognizes BOTH the flat layout (``database/migrations/*.sql``)
  and the future layout (``database/migrations/legacy/*.sql``) — step one of
  the two-step directory move;
* historical files own their own ``BEGIN;``/``COMMIT;`` and are executed
  verbatim; files without explicit transactions run inside a runner-owned
  transaction;
* the filename ledger (``public.schema_migrations.filename``) is canonical;
  numeric version ledgers and their dirty/name/checksum variants stay
  readable and writable exactly the way the old runners wrote them, because
  an existing installation must never be forced onto a new ledger mid-chain;
* the session ``search_path`` is ``knowledge, gateway, assistant, public``
  (knowledge first) exactly like the historical runners, because migrations
  100–112 create unqualified objects that must land in ``knowledge``;
* the per-service track only tops up databases that ALREADY carry
  ``public.schema_migrations_meta`` — a default database never gets the
  split-layout move from the authority.

All legacy ledgers are one-shot adoption inputs: after the baseline marker
is written they are frozen evidence and this module never writes them again.
"""

from __future__ import annotations

import hashlib
import re
from pathlib import Path
from typing import Any

from .constants import DEFAULT_ROLE_PREFIX  # noqa: F401  (kept for callers)
from .discovery import (
    LEGACY_FILENAME_ALIASES,
    LegacyMigration,
    discover_legacy_migrations,
    validate_legacy_chain,
)
from .runner import AuthorityError, AuthorityPaths

# Same four required objects migrate.sh/base_schema_exists checks.
_BASE_SCHEMA_PROBE = """
SELECT CASE WHEN
    COALESCE(to_regclass('gateway.services'), to_regclass('public.services')) IS NOT NULL
    AND COALESCE(to_regclass('knowledge.datasets'), to_regclass('public.datasets')) IS NOT NULL
    AND COALESCE(to_regclass('knowledge.documents'), to_regclass('public.documents')) IS NOT NULL
    AND COALESCE(to_regclass('knowledge.segments'), to_regclass('public.segments')) IS NOT NULL
THEN TRUE ELSE FALSE END
"""

# Historical migrations own their transaction boundaries.
_EXPLICIT_TRANSACTION_RE = re.compile(
    r"(?im)^\s*BEGIN\s*;"
)
_EXPLICIT_COMMIT_RE = re.compile(
    r"(?im)^\s*COMMIT\s*;"
)

PER_SERVICE_ORDER = ("_global", "gateway", "assistant", "knowledge")

_TRACKING_FILENAME = "filename"
_TRACKING_VERSION = "version"


def legacy_checksum(content: str) -> str:
    """Truncated checksum exactly as the historical ledgers recorded it."""
    return hashlib.sha256(content.encode("utf-8")).hexdigest()[:16]


async def base_schema_present(conn: Any) -> bool:
    return bool(await conn.fetchval(_BASE_SCHEMA_PROBE))


async def ensure_base_schema(conn: Any, paths: AuthorityPaths, log: Any = print) -> None:
    """Apply database/schema.sql when the required base objects are missing.

    This mirrors ``ensure_base_schema`` in scripts/new/migrate.sh: the legacy
    chain starts at 002 and assumes the schema.sql bootstrap ran first.
    """
    if await base_schema_present(conn):
        return
    schema_path = paths.database_dir / "schema.sql"
    if not schema_path.exists():
        raise AuthorityError(f"base schema file missing: {schema_path}")
    log("authority: base schema missing; applying database/schema.sql")
    async with conn.transaction():
        await conn.execute(schema_path.read_text(encoding="utf-8"))


async def configure_legacy_search_path(conn: Any) -> None:
    """Match the historical runners: knowledge-first migration resolution."""
    await conn.execute("CREATE SCHEMA IF NOT EXISTS knowledge")
    await conn.execute("SET search_path TO knowledge, gateway, assistant, public")


async def tracking_mode(conn: Any) -> str | None:
    """Detect the legacy ledger shape; None when no ledger exists yet."""
    columns = await conn.fetch(
        """
        SELECT column_name
        FROM information_schema.columns
        WHERE table_schema = 'public' AND table_name = 'schema_migrations'
        """
    )
    names = {str(row["column_name"]) for row in columns}
    if "filename" in names:
        return _TRACKING_FILENAME
    if "version" in names:
        return _TRACKING_VERSION
    if names:
        raise AuthorityError(
            "public.schema_migrations exists but has neither filename nor "
            "version column; refusing to guess a tracking mode"
        )
    return None


async def ensure_filename_ledger(conn: Any) -> None:
    """Create the canonical filename ledger (only when nothing exists yet)."""
    await conn.execute(
        """
        CREATE TABLE IF NOT EXISTS public.schema_migrations (
            filename   TEXT PRIMARY KEY,
            applied_at TIMESTAMPTZ NOT NULL DEFAULT NOW()
        )
        """
    )


async def applied_legacy_names(conn: Any, mode: str) -> set[str]:
    """Applied filenames (filename mode) or 3-digit versions (version mode)."""
    if mode == _TRACKING_FILENAME:
        rows = await conn.fetch("SELECT filename FROM public.schema_migrations")
        return {str(row["filename"]) for row in rows}

    columns = await conn.fetch(
        """
        SELECT column_name
        FROM information_schema.columns
        WHERE table_schema = 'public' AND table_name = 'schema_migrations'
        """
    )
    names = {str(row["column_name"]) for row in columns}
    dirty_filter = " WHERE dirty = FALSE" if "dirty" in names else ""
    rows = await conn.fetch(
        f"SELECT version FROM public.schema_migrations{dirty_filter}"
    )
    return {f"{int(row['version']):03d}" for row in rows}


def legacy_is_applied(mode: str, applied: set[str], migration: LegacyMigration) -> bool:
    if mode == _TRACKING_VERSION:
        return migration.version in applied
    if migration.path.name in applied:
        return True
    alias = LEGACY_FILENAME_ALIASES.get(migration.path.name)
    return alias in applied if alias else False


async def pending_legacy_migrations(
    conn: Any, paths: AuthorityPaths, *, mode: str | None
) -> tuple[str, list[LegacyMigration]]:
    """Return (effective mode, pending migrations) for the legacy chain."""
    migrations = discover_legacy_migrations(paths.migrations_root)
    effective_mode = mode
    if effective_mode is None:
        effective_mode = _TRACKING_FILENAME
        await ensure_filename_ledger(conn)
    # A numeric ledger cannot distinguish the historical 016/031 duplicates.
    validate_legacy_chain(
        migrations,
        allow_historical_filename_duplicates=effective_mode == _TRACKING_FILENAME,
    )
    applied = await applied_legacy_names(conn, effective_mode)
    pending = [
        migration
        for migration in migrations
        if not legacy_is_applied(effective_mode, applied, migration)
    ]
    return effective_mode, pending


async def record_legacy_migration(
    conn: Any, mode: str, migration: LegacyMigration, duration_ms: int
) -> None:
    """Write the success fact in exactly the ledger shape the DB already has."""
    if mode == _TRACKING_FILENAME:
        await conn.execute(
            "INSERT INTO public.schema_migrations (filename) VALUES ($1) "
            "ON CONFLICT (filename) DO NOTHING",
            migration.path.name,
        )
        return

    columns = await conn.fetch(
        """
        SELECT column_name
        FROM information_schema.columns
        WHERE table_schema = 'public' AND table_name = 'schema_migrations'
        """
    )
    names = {str(row["column_name"]) for row in columns}
    numeric_version = int(migration.version)
    if "dirty" in names:
        await conn.execute(
            "INSERT INTO public.schema_migrations (version, dirty) VALUES ($1, FALSE) "
            "ON CONFLICT (version) DO UPDATE SET dirty = FALSE",
            numeric_version,
        )
        return
    if {"name", "checksum", "execution_time_ms"} <= names:
        content = migration.path.read_text(encoding="utf-8")
        await conn.execute(
            """
            INSERT INTO public.schema_migrations (
                version, name, checksum, execution_time_ms
            ) VALUES ($1, $2, $3, $4)
            ON CONFLICT (version) DO UPDATE SET
                applied_at = NOW(), checksum = $3, execution_time_ms = $4
            """,
            migration.version,
            migration.path.name,
            legacy_checksum(content),
            duration_ms,
        )
        return
    await conn.execute(
        "INSERT INTO public.schema_migrations (version) VALUES ($1) "
        "ON CONFLICT (version) DO NOTHING",
        numeric_version,
    )


async def run_one_legacy(
    conn: Any, mode: str, migration: LegacyMigration, log: Any = print
) -> None:
    """Execute one historical migration and record it in the legacy ledger."""
    content = migration.path.read_text(encoding="utf-8")
    has_explicit_transaction = bool(
        _EXPLICIT_TRANSACTION_RE.search(content)
        and _EXPLICIT_COMMIT_RE.search(content)
    )
    log(f"authority: legacy: applying {migration.path.name}")
    start = _monotonic_ms()
    if has_explicit_transaction:
        await conn.execute(content)
    else:
        async with conn.transaction():
            await conn.execute(content)
    duration_ms = _monotonic_ms() - start
    await record_legacy_migration(conn, mode, migration, duration_ms)


def _monotonic_ms() -> int:
    import time

    return int(time.monotonic() * 1000)


async def apply_legacy_chain(
    conn: Any, paths: AuthorityPaths, log: Any = print
) -> tuple[str, int]:
    """Bring the pre-baseline chain up to date. Returns (mode, applied count)."""
    await ensure_base_schema(conn, paths, log=log)
    await configure_legacy_search_path(conn)
    mode = await tracking_mode(conn)
    effective_mode, pending = await pending_legacy_migrations(conn, paths, mode=mode)
    for migration in pending:
        await run_one_legacy(conn, effective_mode, migration, log=log)
    if not pending:
        log("authority: legacy chain already complete")
    return effective_mode, len(pending)


# ----------------------------------------------------------------------
# per-service track (only for databases already carrying its ledger)
# ----------------------------------------------------------------------


async def per_service_ledger_present(conn: Any) -> bool:
    return bool(
        await conn.fetchval(
            "SELECT to_regclass('public.schema_migrations_meta') IS NOT NULL"
        )
    )


def _per_service_root(paths: AuthorityPaths) -> Path:
    return paths.migrations_root / "per_service"


async def apply_per_service_chain(
    conn: Any, paths: AuthorityPaths, log: Any = print
) -> int:
    """Top up the per-service track on a database that already uses it.

    Databases without ``public.schema_migrations_meta`` are untouched: the
    split-layout move is never introduced by the authority on its own.
    """
    if not await per_service_ledger_present(conn):
        return 0
    root = _per_service_root(paths)
    if not root.is_dir():
        return 0
    applied_rows = await conn.fetch(
        "SELECT name FROM public.schema_migrations_meta"
    )
    applied = {str(row["name"]) for row in applied_rows}
    applied_count = 0
    for service in PER_SERVICE_ORDER:
        service_dir = root / service
        if not service_dir.is_dir():
            continue
        for file_path in sorted(service_dir.glob("*.sql")):
            ledger_key = f"{service}:{file_path.name}"
            if ledger_key in applied:
                continue
            log(f"authority: per-service: applying {ledger_key}")
            sql = file_path.read_text(encoding="utf-8")
            async with conn.transaction():
                await conn.execute(sql)
                await conn.execute(
                    "INSERT INTO public.schema_migrations_meta(name, notes) "
                    "VALUES ($1, $2) ON CONFLICT (name) DO NOTHING",
                    ledger_key,
                    f"file=migrations/per_service/{service}/{file_path.name}",
                )
            applied_count += 1
    if applied_count == 0:
        log("authority: per-service track already complete")
    return applied_count
