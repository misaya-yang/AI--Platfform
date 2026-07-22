"""
Run database migrations.
Usage: python database/run_migration.py <migration_file>

NOTE: This is a legacy script. Prefer using: python database/cli.py migrate
"""

import asyncio
import os
import sys
from pathlib import Path

import asyncpg


def get_dsn() -> str:
    """Get database DSN from environment or settings."""
    dsn = os.environ.get("GATEWAY_DATABASE__DSN")
    if dsn:
        return dsn

    try:
        project_root = str(Path(__file__).parent.parent)
        if project_root not in sys.path:
            sys.path.insert(0, project_root)
        from src.config.settings import Settings
        settings = Settings()
        if getattr(settings, "database", None) and settings.database.dsn:
            return settings.database.dsn
    except Exception as exc:
        print(f"Failed to load Settings for migration DSN: {exc}", file=sys.stderr)

    print(
        "GATEWAY_DATABASE__DSN is not set and Settings could not be loaded. "
        "Cannot determine database connection string.",
        file=sys.stderr,
    )
    sys.exit(2)


async def run_migration(file_path: str, dsn: str):
    """Run a single migration file."""
    print(f"Running migration: {file_path}")

    conn = await asyncpg.connect(dsn)

    try:
        with open(file_path, "r", encoding="utf-8") as f:
            sql = f.read()

        async with conn.transaction():
            await conn.execute(sql)
        print(f"Migration completed successfully: {file_path}")

    except asyncpg.PostgresSyntaxError as e:
        print(f"SQL Syntax Error: {e}")
        raise
    except Exception as e:
        print(f"Error running migration: {e}")
        raise
    finally:
        await conn.close()


async def main():
    dsn = get_dsn()

    if len(sys.argv) > 1:
        migration_file = sys.argv[1]
    else:
        print("Usage: python database/run_migration.py <migration_file>")
        print("Example: python database/run_migration.py database/migrations/028_segments_fulltext_search.sql")
        sys.exit(1)

    file_path = Path(migration_file)
    if not file_path.exists():
        file_path = Path(__file__).parent / migration_file

    if not file_path.exists():
        print(f"Migration file not found: {file_path}")
        sys.exit(1)

    await run_migration(str(file_path), dsn)


if __name__ == "__main__":
    asyncio.run(main())
