# -*- coding: utf-8 -*-
import asyncio
import asyncpg
import sys
import os

# Force UTF-8
sys.stdout.reconfigure(encoding='utf-8')

async def main():
    conn = await asyncpg.connect(
        host="127.0.0.1", port=5433, user="postgres",
        password="111111", database="gateway"
    )

    print("=== Segment Types ===")
    rows = await conn.fetch("SELECT content_type, COUNT(*) as cnt FROM segments GROUP BY content_type")
    for r in rows:
        print(f"  {r['content_type']}: {r['cnt']}")

    print("\n=== Image Segments ===")
    rows = await conn.fetch("SELECT COUNT(*) as cnt FROM segments WHERE content_type = 'image'")
    print(f"  Total image segments: {rows[0]['cnt']}")

    print("\n=== Confluence Bindings ===")
    rows = await conn.fetch("SELECT space_key, sync_images FROM confluence_space_bindings")
    for r in rows:
        print(f"  {r['space_key']}: sync_images={r['sync_images']}")

    print("\n=== Datasets ===")
    rows = await conn.fetch("SELECT dataset_id, name, collection_name FROM datasets")
    for r in rows:
        print(f"  {r['name']}: {r['dataset_id']}, collection={r['collection_name']}")

    await conn.close()

if __name__ == "__main__":
    asyncio.run(main())
