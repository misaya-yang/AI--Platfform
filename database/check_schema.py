#!/usr/bin/env python
"""Check database schema after migration."""

import asyncio
import asyncpg
import os


async def check():
    dsn = os.environ.get("GATEWAY_DATABASE__DSN")
    if not dsn:
        raise RuntimeError(
            "Database DSN not configured. Set GATEWAY_DATABASE__DSN environment variable."
        )
    conn = await asyncpg.connect(dsn)
    
    # Get all tables
    tables = await conn.fetch("""
        SELECT table_name FROM information_schema.tables 
        WHERE table_schema = 'public' AND table_type = 'BASE TABLE'
        ORDER BY table_name
    """)
    print('Current tables:')
    for t in tables:
        print(f"  - {t['table_name']}")
    
    # Check new columns on documents
    cols = await conn.fetch("""
        SELECT column_name FROM information_schema.columns 
        WHERE table_name = 'documents' ORDER BY ordinal_position
    """)
    print('\nDocuments table columns:')
    for c in cols:
        print(f"  - {c['column_name']}")
    
    # Check new columns on segments
    cols = await conn.fetch("""
        SELECT column_name FROM information_schema.columns 
        WHERE table_name = 'segments' ORDER BY ordinal_position
    """)
    print('\nSegments table columns:')
    for c in cols:
        print(f"  - {c['column_name']}")
    
    await conn.close()
    print('\n✅ Schema check completed!')


if __name__ == "__main__":
    asyncio.run(check())



