#!/usr/bin/env python3
"""Reconcile drift between hadith_items, hadith_chapters and hadith_collections.

Run this any time after a new sync / rebuild / orphan backfill to re-align:

- ``hadith_chapters.hadith_count`` → actual number of linked items
- ``hadith_collections.has_chapters`` → reflects whether any chapter rows exist
- blank ``hadith_chapters.title_en`` → fallback to "Chapter N"
- stale ``hadith_items.chapter_title`` → mirror of parent chapter's title

Idempotent and safe to re-run. No data is deleted — this only normalises
existing rows so the API response shape stays consistent after any
upstream changes to sunnah.com.

Usage:
    python scripts/hadith_chapter_maintenance.py \\
        --dsn "postgresql://postgres:PW@HOST:5432/gateway"
"""
from __future__ import annotations

import argparse
import asyncio
import logging

import asyncpg

logger = logging.getLogger("chapter_maintenance")
SCHEMA = "islamic_content"


async def run(args: argparse.Namespace) -> None:
    conn = await asyncpg.connect(args.dsn)
    try:
        # --- hadith_count per chapter ---------------------------------
        result = await conn.execute(f"""
            UPDATE {SCHEMA}.hadith_chapters hc
            SET hadith_count = sub.n, updated_at = NOW()
            FROM (
              SELECT chapter_ref_id AS id, COUNT(*) AS n
              FROM {SCHEMA}.hadith_items
              WHERE chapter_ref_id IS NOT NULL
              GROUP BY chapter_ref_id
            ) sub
            WHERE hc.id = sub.id AND hc.hadith_count <> sub.n
        """)
        logger.info("refresh hadith_count: %s", result)

        result = await conn.execute(f"""
            UPDATE {SCHEMA}.hadith_chapters
            SET hadith_count = 0, updated_at = NOW()
            WHERE id NOT IN (
                SELECT DISTINCT chapter_ref_id
                FROM {SCHEMA}.hadith_items
                WHERE chapter_ref_id IS NOT NULL
            )
            AND hadith_count <> 0
        """)
        logger.info("zero-out unreferenced chapter counts: %s", result)

        # --- has_chapters flag per collection -------------------------
        result = await conn.execute(f"""
            UPDATE {SCHEMA}.hadith_collections hc
            SET has_chapters = EXISTS (
                SELECT 1 FROM {SCHEMA}.hadith_chapters ch
                WHERE ch.collection_name = hc.name
            ), updated_at = NOW()
            WHERE has_chapters IS DISTINCT FROM EXISTS (
                SELECT 1 FROM {SCHEMA}.hadith_chapters ch
                WHERE ch.collection_name = hc.name
            )
        """)
        logger.info("sync has_chapters flag: %s", result)

        # --- fallback title for fully-blank chapters ------------------
        result = await conn.execute(f"""
            UPDATE {SCHEMA}.hadith_chapters
            SET title_en = 'Chapter ' || chapter_order, updated_at = NOW()
            WHERE (title_en IS NULL OR title_en = '')
              AND (title_ar IS NULL OR title_ar = '')
        """)
        logger.info("fill blank chapter titles: %s", result)

        # --- propagate chapter title to hadith_items.chapter_title ----
        result = await conn.execute(f"""
            UPDATE {SCHEMA}.hadith_items hi
            SET chapter_title = LEFT(COALESCE(hc.title_en, hc.title_ar), 250),
                updated_at = NOW()
            FROM {SCHEMA}.hadith_chapters hc
            WHERE hi.chapter_ref_id = hc.id
              AND hi.chapter_title IS DISTINCT FROM LEFT(COALESCE(hc.title_en, hc.title_ar), 250)
        """)
        logger.info("mirror chapter_title onto hadith_items: %s", result)

        # --- final coverage report ------------------------------------
        for row in await conn.fetch(f"""
            SELECT hc.name,
                   hc.has_chapters,
                   (SELECT COUNT(*) FROM {SCHEMA}.hadith_chapters c WHERE c.collection_name = hc.name) AS chapters,
                   (SELECT COUNT(*) FROM {SCHEMA}.hadith_items i WHERE i.collection_name = hc.name) AS items,
                   (SELECT COUNT(*) FROM {SCHEMA}.hadith_items i
                    WHERE i.collection_name = hc.name AND chapter_ref_id IS NULL) AS orphans
            FROM {SCHEMA}.hadith_collections hc
            ORDER BY hc.name
        """):
            logger.info("  %-10s has_chapters=%s  chapters=%d  items=%d  orphans=%d",
                        row["name"], row["has_chapters"], row["chapters"],
                        row["items"], row["orphans"])
    finally:
        await conn.close()


def main() -> None:
    logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(name)s - %(message)s")
    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    parser.add_argument("--dsn", required=True)
    asyncio.run(run(parser.parse_args()))


if __name__ == "__main__":
    main()
