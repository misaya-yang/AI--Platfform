#!/usr/bin/env python
"""Run database migrations for KBMS enhancements."""

import asyncio
import asyncpg
import os


async def run_migration():
    dsn = os.environ.get("DATABASE_DSN", "postgresql://postgres:111111@localhost:5433/gateway")
    
    print(f"Connecting to database...")
    conn = await asyncpg.connect(dsn)
    
    try:
        # First check if tables exist
        tables = await conn.fetch("""
            SELECT table_name FROM information_schema.tables 
            WHERE table_schema = 'public' AND table_type = 'BASE TABLE'
        """)
        existing_tables = {row['table_name'] for row in tables}
        print(f"Existing tables: {existing_tables}")
        
        if 'datasets' not in existing_tables:
            print("Base table 'datasets' not found. Running base schema first...")
            # Read and execute base schema
            with open('database/schema.sql', 'r', encoding='utf-8') as f:
                base_sql = f.read()
            await conn.execute(base_sql)
            print("Base schema created successfully!")
        
        # Now run the KBMS enhancement migration
        print("\nRunning KBMS enhancement migration...")
        with open('database/migrations/002_kbms_enhancements.sql', 'r', encoding='utf-8') as f:
            migration_sql = f.read()
        
        # Split by statement and execute
        await conn.execute(migration_sql)
        print("KBMS enhancement migration completed successfully!")
        
        # Verify the new tables
        tables = await conn.fetch("""
            SELECT table_name FROM information_schema.tables 
            WHERE table_schema = 'public' AND table_type = 'BASE TABLE'
            ORDER BY table_name
        """)
        print(f"\nAll tables after migration:")
        for row in tables:
            print(f"  - {row['table_name']}")
        
    except Exception as e:
        print(f"Migration error: {e}")
        raise
    finally:
        await conn.close()
        print("\nDatabase connection closed.")


if __name__ == "__main__":
    asyncio.run(run_migration())

