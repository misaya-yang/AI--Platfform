"""
Run database migrations.
Usage: python database/run_migration.py [migration_file]
"""

import asyncio
import sys
from pathlib import Path

import asyncpg


async def run_migration(file_path: str, dsn: str):
    """Run a single migration file."""
    print(f"Running migration: {file_path}")

    conn = await asyncpg.connect(dsn)

    try:
        with open(file_path, "r", encoding="utf-8") as f:
            sql = f.read()

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
    dsn = "postgresql://postgres:111111@127.0.0.1:5433/gateway"

    if len(sys.argv) > 1:
        migration_file = sys.argv[1]
    else:
        migration_file = "database/migrations/004_confluence_integration.sql"

    file_path = Path(migration_file)
    if not file_path.exists():
        # Try relative to script location
        file_path = Path(__file__).parent / "migrations" / "004_confluence_integration.sql"

    if not file_path.exists():
        print(f"Migration file not found: {file_path}")
        sys.exit(1)

    await run_migration(str(file_path), dsn)


if __name__ == "__main__":
    asyncio.run(main())
