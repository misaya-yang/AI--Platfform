#!/usr/bin/env python
"""
AI Gateway Database CLI

Unified database management tool for initialization, migrations, and maintenance.

Usage:
    python database/cli.py init          # Initialize database (create + schema + migrations)
    python database/cli.py migrate       # Run pending migrations
    python database/cli.py status        # Show migration status
    python database/cli.py reset         # Drop and recreate all tables (DANGER!)
    python database/cli.py check         # Check database connection and tables
"""

import asyncio
import hashlib
import os
import re
import sys
import time
from pathlib import Path
from typing import Literal

# Add project root to path
PROJECT_ROOT = Path(__file__).parent.parent
sys.path.insert(0, str(PROJECT_ROOT))

try:
    import asyncpg
except ImportError:
    print("Error: asyncpg not installed. Run: pip install asyncpg")
    sys.exit(1)

# Paths
DATABASE_DIR = Path(__file__).parent
SCHEMA_FILE = DATABASE_DIR / "schema.sql"
MIGRATIONS_DIR = DATABASE_DIR / "migrations"
MIGRATION_PATTERN = re.compile(r"^(\d{3})_(.+)\.sql$")
ROLLBACK_SUFFIX = "_rollback.sql"
TrackingMode = Literal["filename", "version"]

# Shared with scripts/new/migrate.sh. Session-level advisory locks serialize
# the complete migration plan across Python and shell processes. PostgreSQL
# releases the lock automatically if the owning connection exits or crashes.
MIGRATION_ADVISORY_LOCK_NAMESPACE = 1_095_781_959
MIGRATION_ADVISORY_LOCK_ID = 1

# These duplicate numeric prefixes pre-date both migration ledgers.  A
# filename ledger can distinguish them; a legacy numeric ledger cannot.  Do
# not extend this allow-list: new duplicate prefixes are always an error.
HISTORICAL_FILENAME_DUPLICATES = {
    "016": frozenset({"016_confluence_multi_root_pages.sql", "016_usage_hourly_aggregates.sql"}),
    "031": frozenset({"031_align_model_prices_20260211.sql", "031_hierarchical_segments.sql"}),
}

LEGACY_FILENAME_ALIASES = {
    "089_agent_runtime_thread_store.sql": "089_codex_runtime_thread_store.sql",
    "090_agent_runtime_model_leases.sql": "090_codex_runtime_model_leases.sql",
    "092_agent_runtime_legacy_import.sql": "092_codex_runtime_legacy_import.sql",
    "093_agent_runtime_assistant_session_fks.sql": "093_codex_runtime_assistant_session_fks.sql",
    "094_agent_runtime_legacy_import_normalization.sql": (
        "094_codex_runtime_legacy_import_normalization.sql"
    ),
}


class MigrationChainError(RuntimeError):
    """Raised when the on-disk forward migration chain is ambiguous."""


def get_dsn() -> str:
    """Get database DSN from environment or settings. Fail closed — no default password."""
    dsn = os.environ.get("DATABASE_URL") or os.environ.get("GATEWAY_DATABASE__DSN")
    if dsn:
        return dsn

    try:
        from src.config.settings import Settings

        settings = Settings()
        if getattr(settings, "database", None) and settings.database.dsn:
            return settings.database.dsn
    except Exception:
        print("Failed to load Settings for database DSN.", file=sys.stderr)

    print(
        "DATABASE_URL and GATEWAY_DATABASE__DSN are not set, and Settings "
        "could not provide a DSN. "
        "Cannot determine database connection string.",
        file=sys.stderr,
    )
    sys.exit(2)


def mask_dsn(dsn: str) -> str:
    """Mask the userinfo password in a DSN without leaking ':' or '@' inside it."""
    scheme_sep = dsn.find("://")
    if scheme_sep == -1:
        return dsn
    userinfo_start = scheme_sep + 3
    at = dsn.rfind("@")
    if at <= userinfo_start:
        return dsn
    colon = dsn.find(":", userinfo_start)
    if colon == -1 or colon > at:
        return dsn
    return f"{dsn[: colon + 1]}******{dsn[at:]}"


def compute_checksum(content: str) -> str:
    """Compute SHA256 checksum of content."""
    return hashlib.sha256(content.encode("utf-8")).hexdigest()[:16]


def discover_migrations(
    migrations_dir: Path = MIGRATIONS_DIR,
) -> list[tuple[str, str, Path]]:
    """
    Discover all migration files.

    Returns:
        List of (version, description, path)
    """
    if not migrations_dir.exists():
        return []

    migrations: list[tuple[str, str, Path]] = []

    for file_path in sorted(migrations_dir.glob("*.sql")):
        if file_path.name.endswith(ROLLBACK_SUFFIX):
            continue
        match = MIGRATION_PATTERN.fullmatch(file_path.name)
        if match:
            version = match.group(1)
            description = match.group(2).replace("_", " ").title()
            migrations.append((version, description, file_path))

    return migrations


def validate_migration_chain(
    migrations: list[tuple[str, str, Path]],
    *,
    allow_historical_filename_duplicates: bool,
) -> None:
    """Reject ambiguous numeric revisions before executing migration SQL."""
    files_by_version: dict[str, set[str]] = {}
    for version, _description, file_path in migrations:
        files_by_version.setdefault(version, set()).add(file_path.name)

    for version, filenames in sorted(files_by_version.items()):
        if len(filenames) == 1:
            continue
        if allow_historical_filename_duplicates and frozenset(
            filenames
        ) == HISTORICAL_FILENAME_DUPLICATES.get(version):
            continue
        joined = ", ".join(sorted(filenames))
        raise MigrationChainError(
            f"duplicate migration version {version}: {joined}; "
            "each new forward migration needs a unique numeric prefix"
        )


async def create_database(dsn: str) -> bool:
    """Create database if not exists."""
    db_name = dsn.rsplit("/", 1)[-1].split("?")[0]
    postgres_dsn = dsn.rsplit("/", 1)[0] + "/postgres"

    try:
        conn = await asyncpg.connect(postgres_dsn)
        exists = await conn.fetchval("SELECT 1 FROM pg_database WHERE datname = $1", db_name)

        if not exists:
            await conn.execute(f'CREATE DATABASE "{db_name}"')
            print(f"  Created database '{db_name}'")
        else:
            print(f"  Database '{db_name}' already exists")

        await conn.close()
        return True
    except Exception as e:
        print(f"  Error creating database: {e}")
        return False


async def migration_tracking_columns(conn: asyncpg.Connection) -> set[str]:
    """Return the canonical public ledger columns."""
    rows = await conn.fetch("""
        SELECT column_name
        FROM information_schema.columns
        WHERE table_schema = 'public' AND table_name = 'schema_migrations'
    """)
    return {str(row["column_name"]) for row in rows}


async def ensure_migration_table(conn: asyncpg.Connection) -> TrackingMode:
    """Ensure the filename ledger exists, while accepting legacy version ledgers."""
    await conn.execute("""
        CREATE TABLE IF NOT EXISTS public.schema_migrations (
            filename   TEXT PRIMARY KEY,
            applied_at TIMESTAMPTZ NOT NULL DEFAULT NOW()
        )
    """)
    columns = await migration_tracking_columns(conn)
    if "filename" in columns:
        return "filename"
    if "version" in columns:
        return "version"
    raise MigrationChainError("public.schema_migrations has neither filename nor version column")


async def get_applied_migrations(conn: asyncpg.Connection, mode: TrackingMode) -> set[str]:
    """Get applied filenames or normalized three-digit legacy versions."""
    if mode == "filename":
        rows = await conn.fetch("SELECT filename FROM public.schema_migrations ORDER BY filename")
        return {str(row["filename"]) for row in rows}

    columns = await migration_tracking_columns(conn)
    dirty_filter = " WHERE dirty = FALSE" if "dirty" in columns else ""
    rows = await conn.fetch(
        f"SELECT version FROM public.schema_migrations{dirty_filter} ORDER BY version"
    )
    return {f"{int(row['version']):03d}" for row in rows}


def migration_is_applied(
    *,
    mode: TrackingMode,
    applied: set[str],
    version: str,
    file_path: Path,
) -> bool:
    if mode == "version":
        return version in applied
    if file_path.name in applied:
        return True
    alias = LEGACY_FILENAME_ALIASES.get(file_path.name)
    return alias in applied if alias else False


async def record_migration(
    conn: asyncpg.Connection,
    mode: TrackingMode,
    version: str,
    filename: str,
    name: str,
    checksum: str,
    execution_time_ms: int,
) -> None:
    """Record a migration in the canonical or a supported legacy ledger."""
    if mode == "filename":
        await conn.execute(
            "INSERT INTO public.schema_migrations (filename) VALUES ($1) "
            "ON CONFLICT (filename) DO NOTHING",
            filename,
        )
        return

    columns = await migration_tracking_columns(conn)
    numeric_version = int(version)
    if "dirty" in columns:
        await conn.execute(
            "INSERT INTO public.schema_migrations (version, dirty) VALUES ($1, FALSE) "
            "ON CONFLICT (version) DO UPDATE SET dirty = FALSE",
            numeric_version,
        )
        return
    if {"name", "checksum", "execution_time_ms"} <= columns:
        await conn.execute(
            """
            INSERT INTO public.schema_migrations (
                version, name, checksum, execution_time_ms
            ) VALUES ($1, $2, $3, $4)
            ON CONFLICT (version) DO UPDATE SET
                applied_at = NOW(), checksum = $3, execution_time_ms = $4
            """,
            version,
            name,
            checksum,
            execution_time_ms,
        )
        return
    await conn.execute(
        "INSERT INTO public.schema_migrations (version) VALUES ($1) "
        "ON CONFLICT (version) DO NOTHING",
        numeric_version,
    )


async def configure_migration_search_path(conn: asyncpg.Connection) -> None:
    """Create the owner schema and resolve every migration knowledge-first."""
    await conn.execute("CREATE SCHEMA IF NOT EXISTS knowledge")
    await conn.execute("SET search_path TO knowledge, gateway, assistant, public")


async def acquire_migration_advisory_lock(conn: asyncpg.Connection) -> None:
    """Wait for exclusive ownership of the canonical migration chain."""
    await conn.execute(
        "SELECT pg_advisory_lock($1::integer, $2::integer)",
        MIGRATION_ADVISORY_LOCK_NAMESPACE,
        MIGRATION_ADVISORY_LOCK_ID,
    )


async def load_migration_state(
    conn: asyncpg.Connection,
) -> tuple[TrackingMode, set[str], list[tuple[str, str, Path]]]:
    """Load one validated migration plan against the canonical public ledger."""
    mode = await ensure_migration_table(conn)
    migrations = discover_migrations()
    validate_migration_chain(
        migrations,
        allow_historical_filename_duplicates=mode == "filename",
    )
    applied = await get_applied_migrations(conn, mode)
    await configure_migration_search_path(conn)
    return mode, applied, migrations


def pending_migrations(
    mode: TrackingMode,
    applied: set[str],
    migrations: list[tuple[str, str, Path]],
) -> list[tuple[str, str, Path]]:
    return [
        migration
        for migration in migrations
        if not migration_is_applied(
            mode=mode,
            applied=applied,
            version=migration[0],
            file_path=migration[2],
        )
    ]


async def run_sql_file(conn: asyncpg.Connection, file_path: Path) -> tuple[bool, str, int]:
    """
    Execute a SQL file.

    Returns:
        (success, message, execution_time_ms)
    """
    content = file_path.read_text(encoding="utf-8")
    start_time = time.time()

    try:
        has_explicit_transaction = bool(
            re.search(r"(?im)^\s*BEGIN\s*;", content) and re.search(r"(?im)^\s*COMMIT\s*;", content)
        )
        if has_explicit_transaction:
            await conn.execute(content)
        else:
            async with conn.transaction():
                await conn.execute(content)

        execution_time_ms = int((time.time() - start_time) * 1000)
        return True, "OK", execution_time_ms

    except Exception as e:
        return False, str(e), 0


async def cmd_init():
    """Initialize database: create database, run schema, run migrations."""
    print("\n" + "=" * 60)
    print("AI Gateway Database Initialization")
    print("=" * 60)

    dsn = get_dsn()
    print(f"\nDatabase: {mask_dsn(dsn)}")

    # Step 1: Create database
    print("\n[1/4] Creating database...")
    if not await create_database(dsn):
        print("\nFailed to create database.")
        sys.exit(1)

    # Step 2: Connect and verify
    print("\n[2/4] Connecting to database...")
    try:
        conn = await asyncpg.connect(dsn)
        version = await conn.fetchval("SELECT version()")
        print(f"  Connected: {version.split(',')[0]}")
    except Exception as e:
        print(f"  Connection failed: {e}")
        sys.exit(1)

    try:
        await acquire_migration_advisory_lock(conn)
        # Step 3: Run base schema
        print("\n[3/4] Running base schema...")
        if not SCHEMA_FILE.exists():
            print(f"  Error: Schema file not found: {SCHEMA_FILE}")
            sys.exit(1)

        success, msg, ms = await run_sql_file(conn, SCHEMA_FILE)
        if success:
            print(f"  Base schema applied ({ms}ms)")
        else:
            print(f"  Error: {msg}")
            sys.exit(1)

        # Step 4: Run migrations
        print("\n[4/4] Running migrations...")
        mode, applied, migrations = await load_migration_state(conn)
        pending = pending_migrations(mode, applied, migrations)

        if not pending:
            print("  All migrations are up to date.")
        else:
            for version, description, file_path in pending:
                print(f"  Applying {version}: {description}...")
                content = file_path.read_text(encoding="utf-8")
                checksum = compute_checksum(content)

                success, msg, ms = await run_sql_file(conn, file_path)
                if success:
                    await record_migration(
                        conn,
                        mode,
                        version,
                        file_path.name,
                        description,
                        checksum,
                        ms,
                    )
                    print(f"    Done ({ms}ms)")
                else:
                    print(f"    Error: {msg}")
                    sys.exit(1)

        # Summary
        tables = await conn.fetch("""
            SELECT table_name FROM information_schema.tables
            WHERE table_schema = 'public' AND table_type = 'BASE TABLE'
            ORDER BY table_name
        """)

        print("\n" + "=" * 60)
        print(f"Initialization complete! {len(tables)} tables created.")
        print("=" * 60 + "\n")

    finally:
        await conn.close()


async def cmd_migrate(target_version: str | None = None):
    """Run pending migrations."""
    dsn = get_dsn()
    print(f"\nDatabase: {mask_dsn(dsn)}")

    try:
        conn = await asyncpg.connect(dsn)
    except Exception as e:
        print(f"Connection failed: {e}")
        sys.exit(1)

    try:
        await acquire_migration_advisory_lock(conn)
        mode, applied, migrations = await load_migration_state(conn)
        pending = pending_migrations(mode, applied, migrations)
        if target_version is not None:
            if not re.fullmatch(r"\d{1,3}", target_version):
                print(f"Invalid migration version: {target_version}")
                sys.exit(2)
            normalized_target = f"{int(target_version):03d}"
            pending = [item for item in pending if item[0] == normalized_target]

        if not pending:
            if target_version:
                print(f"Migration {target_version} already applied or not found.")
            else:
                print("All migrations are up to date.")
            return

        print(f"\nRunning {len(pending)} migration(s)...\n")

        for version, description, file_path in pending:
            print(f"  {version}: {description}")
            content = file_path.read_text(encoding="utf-8")
            checksum = compute_checksum(content)

            success, msg, ms = await run_sql_file(conn, file_path)
            if success:
                await record_migration(
                    conn,
                    mode,
                    version,
                    file_path.name,
                    description,
                    checksum,
                    ms,
                )
                print(f"    Done ({ms}ms)")
                if msg != "OK":
                    print(f"    Note: {msg}")
            else:
                print(f"    Error: {msg}")
                print("\nMigration aborted.")
                sys.exit(1)

        print("\nAll migrations completed successfully!")

    finally:
        await conn.close()


async def cmd_status():
    """Show migration status."""
    dsn = get_dsn()
    print(f"\nDatabase: {mask_dsn(dsn)}")

    try:
        conn = await asyncpg.connect(dsn)
    except Exception as e:
        print(f"Connection failed: {e}")
        sys.exit(1)

    try:
        await acquire_migration_advisory_lock(conn)
        mode, applied, migrations = await load_migration_state(conn)
        pending = pending_migrations(mode, applied, migrations)
        pending_filenames = {path.name for _version, _description, path in pending}

        print("\n" + "=" * 60)
        print("Migration Status")
        print("=" * 60)

        for version, description, path in migrations:
            icon = "[ ]" if path.name in pending_filenames else "[x]"
            print(f"  {icon} {version}  {description}")

        print("=" * 60)
        pending_count = len(pending)
        applied_count = len(migrations) - pending_count
        print(
            f"Total: {len(migrations)} migrations, {applied_count} applied, {pending_count} pending"
        )
        print()

    finally:
        await conn.close()


async def cmd_check():
    """Check database connection and tables."""
    dsn = get_dsn()
    print(f"\nDatabase: {mask_dsn(dsn)}")

    try:
        conn = await asyncpg.connect(dsn)
        version = await conn.fetchval("SELECT version()")
        print(f"Connected: {version.split(',')[0]}")
    except Exception as e:
        print(f"Connection failed: {e}")
        sys.exit(1)

    try:
        tables = await conn.fetch("""
            SELECT table_name FROM information_schema.tables
            WHERE table_schema = 'public' AND table_type = 'BASE TABLE'
            ORDER BY table_name
        """)

        print(f"\nTables ({len(tables)}):")
        for t in tables:
            print(f"  - {t['table_name']}")

        if not tables:
            print("  (no tables found)")
            print("\nRun 'python database/cli.py init' to initialize the database.")

    finally:
        await conn.close()


async def cmd_reset():
    """Reset public-schema tables; this is not a full split-schema reset."""
    dsn = get_dsn()
    print(f"\nDatabase: {mask_dsn(dsn)}")

    confirm = input(
        "\nWARNING: This will DROP ALL TABLES and recreate them.\nType 'yes' to confirm: "
    )
    if confirm.lower() != "yes":
        print("Aborted.")
        return

    try:
        conn = await asyncpg.connect(dsn)
    except Exception as e:
        print(f"Connection failed: {e}")
        sys.exit(1)

    try:
        await acquire_migration_advisory_lock(conn)
        # Drop public-schema tables only.
        print("\nDropping all tables...")
        await conn.execute("""
            DO $$
            DECLARE r RECORD;
            BEGIN
                FOR r IN (SELECT tablename FROM pg_tables WHERE schemaname = 'public') LOOP
                    EXECUTE 'DROP TABLE IF EXISTS ' || quote_ident(r.tablename) || ' CASCADE';
                END LOOP;
            END $$;
        """)
        print("  All tables dropped.")

        # Recreate
        print("\nRecreating schema...")
        success, msg, ms = await run_sql_file(conn, SCHEMA_FILE)
        if success:
            print(f"  Base schema applied ({ms}ms)")
        else:
            print(f"  Error: {msg}")
            sys.exit(1)

        # Run migrations
        print("\nRunning migrations...")
        mode, applied, migrations = await load_migration_state(conn)

        for version, description, file_path in pending_migrations(mode, applied, migrations):
            content = file_path.read_text(encoding="utf-8")
            checksum = compute_checksum(content)

            success, msg, ms = await run_sql_file(conn, file_path)
            if success:
                await record_migration(
                    conn,
                    mode,
                    version,
                    file_path.name,
                    description,
                    checksum,
                    ms,
                )
                print(f"  {version}: {description} ({ms}ms)")
            else:
                print(f"  {version}: Error - {msg}")

        print("\nDatabase reset complete!")

    finally:
        await conn.close()


def main():
    """Main entry point."""
    if len(sys.argv) < 2 or sys.argv[1] in ("-h", "--help", "help"):
        print(__doc__)
        sys.exit(0 if len(sys.argv) >= 2 else 1)

    command = sys.argv[1].lower()

    if command == "init":
        asyncio.run(cmd_init())
    elif command == "migrate":
        target = sys.argv[2] if len(sys.argv) > 2 else None
        asyncio.run(cmd_migrate(target))
    elif command == "status":
        asyncio.run(cmd_status())
    elif command == "check":
        asyncio.run(cmd_check())
    elif command == "reset":
        asyncio.run(cmd_reset())
    else:
        print(f"Unknown command: {command}")
        print(__doc__)
        sys.exit(1)


if __name__ == "__main__":
    main()
