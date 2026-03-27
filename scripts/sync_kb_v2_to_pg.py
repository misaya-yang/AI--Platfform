#!/usr/bin/env python3
"""Sync KB V2 Qdrant collection → PostgreSQL segments table.

The rebuild_kb_imam_v2.py script only wrote to Qdrant. This script reads all
points from the V2 Qdrant collection and populates the PG segments table so
the frontend KB management page shows correct data.

Steps:
  1. Find the imam dataset in PG (by collection_name)
  2. Create one document per source_type (quran, hadith, tafseer, etc.)
  3. Delete old V1 segments for this dataset
  4. Read all points from V2 Qdrant collection
  5. Insert them into PG segments table

Usage:
    python scripts/sync_kb_v2_to_pg.py
    python scripts/sync_kb_v2_to_pg.py --dry-run
    python scripts/sync_kb_v2_to_pg.py --delete-v1  # Also delete V1 Qdrant collection
"""
from __future__ import annotations

import argparse
import asyncio
import hashlib
import logging
import os
import sys
import time
from typing import Any

import asyncpg
from qdrant_client import AsyncQdrantClient

log = logging.getLogger("sync-v2-pg")

V2_COLLECTION = "kb_imam_v2_1024"
V1_COLLECTION = "kb_islamic-knowledge_1024"
BATCH_SIZE = 500  # Qdrant scroll batch
PG_BATCH = 200    # PG insert batch

# Source type → Document title mapping
SOURCE_DOCS = {
    "quran": "Quran - Sahih International",
    "hadith": "Hadith Collections",
    "dua": "Du'a & Supplications",
    "tafseer": "Tafsir Sources",
    "fiqh": "Fiqh & Jurisprudence",
    "aqeedah": "Aqeedah & Creed",
    "seerah": "Seerah & Prophetic Biography",
    "general_islamic": "General Islamic Knowledge",
    "unknown": "Other Islamic Sources",
}


async def find_dataset(pool: asyncpg.Pool) -> dict | None:
    """Find the imam dataset by collection_name."""
    row = await pool.fetchrow(
        "SELECT dataset_id, name, collection_name FROM datasets "
        "WHERE collection_name = $1 AND is_deleted = FALSE",
        V2_COLLECTION,
    )
    if row:
        return dict(row)

    # Fallback: find by name pattern
    row = await pool.fetchrow(
        "SELECT dataset_id, name, collection_name FROM datasets "
        "WHERE (name ILIKE '%imam%' OR name ILIKE '%islamic%') AND is_deleted = FALSE "
        "ORDER BY updated_at DESC LIMIT 1"
    )
    return dict(row) if row else None


async def ensure_documents(pool: asyncpg.Pool, dataset_id: str) -> dict[str, str]:
    """Create one document per source_type, return {source_type: document_id}."""
    doc_map = {}
    for source_type, title in SOURCE_DOCS.items():
        doc_id = f"doc-{dataset_id}-{source_type}"
        await pool.execute(
            """
            INSERT INTO documents (document_id, dataset_id, title, source_type, status, enabled)
            VALUES ($1, $2, $3, 'text', 'completed', TRUE)
            ON CONFLICT (document_id) DO UPDATE SET
                title = EXCLUDED.title,
                status = 'completed',
                updated_at = NOW()
            """,
            doc_id, dataset_id, title,
        )
        doc_map[source_type] = doc_id
    return doc_map


async def delete_old_segments(pool: asyncpg.Pool, dataset_id: str) -> int:
    """Delete all existing segments for the dataset."""
    result = await pool.execute(
        "DELETE FROM segments WHERE dataset_id = $1", dataset_id
    )
    count = int(result.split()[-1])
    log.info("Deleted %d old segments for dataset %s", count, dataset_id)
    return count


async def scroll_all_points(client: AsyncQdrantClient, collection: str) -> list[dict]:
    """Read all points from Qdrant collection."""
    all_points = []
    offset = None
    while True:
        result = await client.scroll(
            collection_name=collection,
            limit=BATCH_SIZE,
            offset=offset,
            with_payload=True,
            with_vectors=False,
        )
        points, next_offset = result
        for p in points:
            all_points.append({
                "id": str(p.id),
                "payload": p.payload or {},
            })
        if next_offset is None or len(points) == 0:
            break
        offset = next_offset
        log.info("  Scrolled %d points so far...", len(all_points))

    log.info("Total points from Qdrant: %d", len(all_points))
    return all_points


async def insert_segments(
    pool: asyncpg.Pool,
    dataset_id: str,
    doc_map: dict[str, str],
    points: list[dict],
) -> int:
    """Insert Qdrant points into PG segments table."""
    inserted = 0

    for i in range(0, len(points), PG_BATCH):
        batch = points[i:i + PG_BATCH]
        rows = []
        for pos, point in enumerate(batch, start=i):
            payload = point["payload"]
            source_type = payload.get("source_type", "unknown")
            doc_id = doc_map.get(source_type, doc_map.get("unknown", ""))
            text = payload.get("text", payload.get("content", ""))

            # Build citation_text from payload
            citation = payload.get("citation_text", "")

            # Compute content hash
            content_hash = hashlib.sha256(text.encode()).hexdigest()[:16] if text else ""

            # Word count
            word_count = len(text.split()) if text else 0

            rows.append((
                point["id"],          # segment_id
                dataset_id,           # dataset_id
                doc_id,               # document_id
                pos,                  # position
                text,                 # text
                0,                    # token_count (we don't have this)
                point["id"],          # vector_id (same as Qdrant point ID)
                payload,              # metadata (full payload as JSONB)
                source_type,          # source_type
                payload,              # source_reference
                citation,             # citation_text
                payload.get("language", "en"),  # language
                content_hash,         # content_hash
                word_count,           # word_count
            ))

        await pool.executemany(
            """
            INSERT INTO segments (
                segment_id, dataset_id, document_id, position, text,
                token_count, vector_id, metadata, source_type,
                source_reference, citation_text, language,
                content_hash, word_count, status, enabled
            ) VALUES (
                $1, $2, $3, $4, $5,
                $6, $7, $8::jsonb, $9,
                $10::jsonb, $11, $12,
                $13, $14, 'completed', TRUE
            )
            ON CONFLICT (segment_id) DO NOTHING
            """,
            rows,
        )
        inserted += len(batch)
        log.info("  Inserted %d / %d segments", inserted, len(points))

    return inserted


async def update_document_counts(pool: asyncpg.Pool, dataset_id: str):
    """Update segment_count and word_count on documents."""
    await pool.execute(
        """
        UPDATE documents d SET
            segment_count = sub.cnt,
            word_count = sub.wc,
            updated_at = NOW()
        FROM (
            SELECT document_id, COUNT(*) as cnt, COALESCE(SUM(word_count), 0) as wc
            FROM segments WHERE dataset_id = $1
            GROUP BY document_id
        ) sub
        WHERE d.document_id = sub.document_id
        """,
        dataset_id,
    )
    log.info("Updated document counts for dataset %s", dataset_id)


async def delete_v1_collection(qdrant_url: str):
    """Delete the V1 Qdrant collection."""
    import httpx
    async with httpx.AsyncClient() as client:
        resp = await client.delete(f"{qdrant_url}/collections/{V1_COLLECTION}")
        if resp.status_code in (200, 404):
            log.info("V1 collection '%s' deleted (status=%d)", V1_COLLECTION, resp.status_code)
        else:
            log.warning("Failed to delete V1: %d %s", resp.status_code, resp.text)


async def main():
    parser = argparse.ArgumentParser(description="Sync KB V2 Qdrant → PG segments")
    parser.add_argument("--pg-dsn", default=os.getenv(
        "PG_DSN", "postgresql://postgres:HejazDB2026Secure@127.0.0.1:5432/ai_gateway"
    ))
    parser.add_argument("--qdrant-url", default=os.getenv("QDRANT_URL", "http://127.0.0.1:6333"))
    parser.add_argument("--dry-run", action="store_true", help="Only count, don't write")
    parser.add_argument("--delete-v1", action="store_true", help="Delete V1 Qdrant collection")
    args = parser.parse_args()

    logging.basicConfig(level=logging.INFO, format="%(levelname)s %(message)s")

    # Connect
    pool = await asyncpg.create_pool(args.pg_dsn, min_size=1, max_size=3)
    qdrant = AsyncQdrantClient(url=args.qdrant_url)

    try:
        # 1. Find dataset
        dataset = await find_dataset(pool)
        if not dataset:
            log.error("No imam dataset found in PG. Create one first.")
            sys.exit(1)
        dataset_id = dataset["dataset_id"]
        log.info("Found dataset: %s (id=%s, collection=%s)",
                 dataset["name"], dataset_id, dataset.get("collection_name"))

        # Ensure collection_name is V2
        if dataset.get("collection_name") != V2_COLLECTION:
            log.info("Updating collection_name → %s", V2_COLLECTION)
            if not args.dry_run:
                await pool.execute(
                    "UPDATE datasets SET collection_name = $1, embedding_model = 'gemini-embedding-2-preview', updated_at = NOW() WHERE dataset_id = $2",
                    V2_COLLECTION, dataset_id,
                )

        # 2. Read all V2 points
        log.info("Scrolling V2 collection: %s", V2_COLLECTION)
        points = await scroll_all_points(qdrant, V2_COLLECTION)
        if not points:
            log.error("No points in V2 collection!")
            sys.exit(1)

        # Show distribution
        dist: dict[str, int] = {}
        for p in points:
            st = p["payload"].get("source_type", "unknown")
            dist[st] = dist.get(st, 0) + 1
        for st, count in sorted(dist.items(), key=lambda x: -x[1]):
            log.info("  %s: %d", st, count)

        if args.dry_run:
            log.info("[DRY-RUN] Would sync %d points to PG segments", len(points))
            return

        # 3. Create documents
        doc_map = await ensure_documents(pool, dataset_id)
        log.info("Document map: %s", {k: v for k, v in doc_map.items() if k in dist})

        # 4. Delete old segments
        await delete_old_segments(pool, dataset_id)

        # 5. Insert new segments
        t0 = time.monotonic()
        inserted = await insert_segments(pool, dataset_id, doc_map, points)
        elapsed = time.monotonic() - t0
        log.info("Inserted %d segments in %.1fs", inserted, elapsed)

        # 6. Update document counts
        await update_document_counts(pool, dataset_id)

        # 7. Optionally delete V1 collection
        if args.delete_v1:
            await delete_v1_collection(args.qdrant_url)

        log.info("DONE! Frontend KB page should now show %d segments.", inserted)

    finally:
        await pool.close()
        await qdrant.close()


if __name__ == "__main__":
    asyncio.run(main())
