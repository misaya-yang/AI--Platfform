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
from datetime import datetime
from pathlib import Path
from typing import List, Optional, Tuple

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


def get_dsn() -> str:
    """Get database DSN from environment or settings."""
    # 1) Explicit environment variable
    dsn = os.environ.get("GATEWAY_DATABASE__DSN")
    if dsn:
        return dsn

    # 2) Try loading from project Settings
    try:
        from src.config.settings import Settings
        settings = Settings()
        if getattr(settings, "database", None) and settings.database.dsn:
            return settings.database.dsn
    except Exception:
        pass

    # 3) Fallback default
    return "postgresql://postgres:postgres@localhost:5432/gateway"


def mask_dsn(dsn: str) -> str:
    """Mask password in DSN for display."""
    import re
    return re.sub(r':([^:@]+)@', ':******@', dsn)


def compute_checksum(content: str) -> str:
    """Compute SHA256 checksum of content."""
    return hashlib.sha256(content.encode("utf-8")).hexdigest()[:16]


def discover_migrations() -> List[Tuple[str, str, Path]]:
    """
    Discover all migration files.

    Returns:
        List of (version, description, path)
    """
    if not MIGRATIONS_DIR.exists():
        return []

    migrations = []
    pattern = re.compile(r"^(\d{3})_(.+)\.sql$")

    for file_path in sorted(MIGRATIONS_DIR.glob("*.sql")):
        match = pattern.match(file_path.name)
        if match:
            version = match.group(1)
            description = match.group(2).replace("_", " ").title()
            migrations.append((version, description, file_path))

    return migrations


async def create_database(dsn: str) -> bool:
    """Create database if not exists."""
    db_name = dsn.rsplit("/", 1)[-1].split("?")[0]
    postgres_dsn = dsn.rsplit("/", 1)[0] + "/postgres"

    try:
        conn = await asyncpg.connect(postgres_dsn)
        exists = await conn.fetchval(
            "SELECT 1 FROM pg_database WHERE datname = $1", db_name
        )

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


async def ensure_migration_table(conn: asyncpg.Connection) -> None:
    """Ensure migration tracking table exists."""
    await conn.execute("""
        CREATE TABLE IF NOT EXISTS schema_migrations (
            id SERIAL PRIMARY KEY,
            version VARCHAR(10) NOT NULL UNIQUE,
            name VARCHAR(255) NOT NULL,
            applied_at TIMESTAMPTZ NOT NULL DEFAULT NOW(),
            checksum VARCHAR(64),
            execution_time_ms INTEGER
        )
    """)


async def get_applied_migrations(conn: asyncpg.Connection) -> List[str]:
    """Get list of applied migration versions."""
    rows = await conn.fetch(
        "SELECT version FROM schema_migrations ORDER BY version"
    )
    return [row["version"] for row in rows]


async def record_migration(
    conn: asyncpg.Connection,
    version: str,
    name: str,
    checksum: str,
    execution_time_ms: int,
) -> None:
    """Record a migration as applied."""
    await conn.execute("""
        INSERT INTO schema_migrations (version, name, checksum, execution_time_ms)
        VALUES ($1, $2, $3, $4)
        ON CONFLICT (version) DO UPDATE SET
            applied_at = NOW(),
            checksum = $3,
            execution_time_ms = $4
    """, version, name, checksum, execution_time_ms)


async def run_sql_file(conn: asyncpg.Connection, file_path: Path) -> Tuple[bool, str, int]:
    """
    Execute a SQL file.

    Returns:
        (success, message, execution_time_ms)
    """
    import time

    content = file_path.read_text(encoding="utf-8")
    start_time = time.time()

    try:
        async with conn.transaction():
            await conn.execute(content)

        execution_time_ms = int((time.time() - start_time) * 1000)
        return True, "OK", execution_time_ms

    except asyncpg.exceptions.DuplicateTableError as e:
        return True, f"Table already exists (skipped)", 0
    except asyncpg.exceptions.DuplicateColumnError as e:
        return True, f"Column already exists (skipped)", 0
    except asyncpg.exceptions.DuplicateObjectError as e:
        return True, f"Object already exists (skipped)", 0
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
        await ensure_migration_table(conn)
        applied = await get_applied_migrations(conn)
        migrations = discover_migrations()

        pending = [(v, d, p) for v, d, p in migrations if v not in applied]

        if not pending:
            print("  All migrations are up to date.")
        else:
            for version, description, file_path in pending:
                print(f"  Applying {version}: {description}...")
                content = file_path.read_text(encoding="utf-8")
                checksum = compute_checksum(content)

                success, msg, ms = await run_sql_file(conn, file_path)
                if success:
                    await record_migration(conn, version, description, checksum, ms)
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


async def cmd_migrate(target_version: Optional[str] = None):
    """Run pending migrations."""
    dsn = get_dsn()
    print(f"\nDatabase: {mask_dsn(dsn)}")

    try:
        conn = await asyncpg.connect(dsn)
    except Exception as e:
        print(f"Connection failed: {e}")
        sys.exit(1)

    try:
        await ensure_migration_table(conn)
        applied = await get_applied_migrations(conn)
        migrations = discover_migrations()

        pending = [
            (v, d, p) for v, d, p in migrations
            if v not in applied and (target_version is None or v == target_version)
        ]

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
                await record_migration(conn, version, description, checksum, ms)
                print(f"    Done ({ms}ms)")
                if msg != "OK":
                    print(f"    Note: {msg}")
            else:
                print(f"    Error: {msg}")
                print("\nMigration aborted.")
                sys.exit(1)

        print(f"\nAll migrations completed successfully!")

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
        await ensure_migration_table(conn)
        applied = await get_applied_migrations(conn)
        migrations = discover_migrations()

        print("\n" + "=" * 60)
        print("Migration Status")
        print("=" * 60)

        for version, description, path in migrations:
            status = "Applied" if version in applied else "Pending"
            icon = "[x]" if version in applied else "[ ]"
            print(f"  {icon} {version}  {description}")

        print("=" * 60)
        applied_count = len(applied)
        pending_count = len(migrations) - applied_count
        print(f"Total: {len(migrations)} migrations, {applied_count} applied, {pending_count} pending")
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
    """Reset database - drop and recreate all tables."""
    dsn = get_dsn()
    print(f"\nDatabase: {mask_dsn(dsn)}")

    confirm = input("\nWARNING: This will DROP ALL TABLES and recreate them.\nType 'yes' to confirm: ")
    if confirm.lower() != 'yes':
        print("Aborted.")
        return

    try:
        conn = await asyncpg.connect(dsn)
    except Exception as e:
        print(f"Connection failed: {e}")
        sys.exit(1)

    try:
        # Drop all tables
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
        await ensure_migration_table(conn)
        migrations = discover_migrations()

        for version, description, file_path in migrations:
            content = file_path.read_text(encoding="utf-8")
            checksum = compute_checksum(content)

            success, msg, ms = await run_sql_file(conn, file_path)
            if success:
                await record_migration(conn, version, description, checksum, ms)
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
