#!/usr/bin/env python3
"""Give every hadith_item a chapter_ref_id, even when the upstream scraper
missed its real chapter.

After the sunnah.com chapter sync we still have ~1200 hadiths across
5 collections that can't be matched to any chapter row, mostly because:

1. fawazahmed0 exposes a "Book 0" (Introduction / preamble hadiths) that
   sunnah.com does not expose as a browsable book. bukhari, nasai and
   ibnmajah all have these.
2. AhmedElTabarani's scraper truncates the chapter list for a handful of
   very large books (Bukhari 65 "Tafseer" loses ~480 hadiths, Tirmidhi 48
   loses 20, etc.) — the chapters are there in metadata but their
   ``ahadith[]`` arrays are short.

This script closes the gap by creating ONE catch-all chapter per affected
(collection, book_number) pair and linking the orphans to it. Titles are
picked to explain what the reader is looking at:

  - book 0              → "Introduction (Unmapped preamble hadiths)"
  - other books         → "Additional hadiths (not grouped by sunnah.com)"

The synthetic chapter gets chapter_order = max(existing) + 1 so it always
appears at the end of the book's chapter list. Running this repeatedly
is idempotent: if the synthetic chapter already exists it is reused.

Usage:
    python scripts/backfill_orphan_hadith_chapters.py \\
        --dsn "postgresql://postgres:PW@HOST:5432/gateway"
"""
from __future__ import annotations

import argparse
import asyncio
import logging

import asyncpg

logger = logging.getLogger("backfill_orphans")

SCHEMA = "islamic_content"
INTRO_TITLE = "Introduction (Unmapped preamble hadiths)"
CATCHALL_TITLE = "Additional hadiths (not grouped by sunnah.com)"
SOURCE_MARK = "synthetic-catchall"


async def _ensure_catchall(conn: asyncpg.Connection, coll: str, book: str) -> int:
    """Return the id of the catch-all chapter for (coll, book), creating it
    if necessary. Idempotent."""
    existing = await conn.fetchval(
        f"""
        SELECT id FROM {SCHEMA}.hadith_chapters
        WHERE collection_name = $1 AND book_number = $2 AND source_api = $3
        """,
        coll, book, SOURCE_MARK,
    )
    if existing is not None:
        return existing

    max_order = await conn.fetchval(
        f"""
        SELECT COALESCE(MAX(chapter_order), 0)
        FROM {SCHEMA}.hadith_chapters
        WHERE collection_name = $1 AND book_number = $2
        """,
        coll, book,
    )
    title = INTRO_TITLE if book == "0" else CATCHALL_TITLE
    return await conn.fetchval(
        f"""
        INSERT INTO {SCHEMA}.hadith_chapters
            (collection_name, book_number, chapter_order,
             title_en, hadith_count, source_api)
        VALUES ($1, $2, $3, $4, 0, $5)
        RETURNING id
        """,
        coll, book, max_order + 1, title, SOURCE_MARK,
    )


async def run(args: argparse.Namespace) -> None:
    conn = await asyncpg.connect(args.dsn)
    try:
        orphans = await conn.fetch(
            f"""
            SELECT collection_name, book_number, COUNT(*) AS n
            FROM {SCHEMA}.hadith_items
            WHERE chapter_ref_id IS NULL
            GROUP BY collection_name, book_number
            ORDER BY collection_name, (book_number::int)
            """
        )
        if not orphans:
            logger.info("no orphan hadith_items — nothing to do")
            return

        total_fixed = 0
        async with conn.transaction():
            for row in orphans:
                coll = row["collection_name"]
                book = row["book_number"]
                chapter_id = await _ensure_catchall(conn, coll, book)
                result = await conn.execute(
                    f"""
                    UPDATE {SCHEMA}.hadith_items
                    SET chapter_ref_id = $1, updated_at = NOW()
                    WHERE collection_name = $2 AND book_number = $3
                      AND chapter_ref_id IS NULL
                    """,
                    chapter_id, coll, book,
                )
                # asyncpg execute returns "UPDATE <n>" — pull the count off
                try:
                    fixed = int(result.rsplit(" ", 1)[-1])
                except ValueError:
                    fixed = 0
                total_fixed += fixed
                logger.info("  %s/%s → catch-all chapter %d, %d hadiths linked",
                            coll, book, chapter_id, fixed)

            # Keep hadith_count column on the synthetic row honest
            await conn.execute(
                f"""
                UPDATE {SCHEMA}.hadith_chapters hc
                SET hadith_count = (
                        SELECT COUNT(*) FROM {SCHEMA}.hadith_items hi
                        WHERE hi.chapter_ref_id = hc.id
                    ),
                    updated_at = NOW()
                WHERE source_api = $1
                """,
                SOURCE_MARK,
            )

        # Final coverage report
        logger.info("=== DONE — total newly linked: %d ===", total_fixed)
        rows = await conn.fetch(
            f"""
            SELECT collection_name,
                   COUNT(*) AS total,
                   COUNT(*) FILTER (WHERE chapter_ref_id IS NULL) AS unlinked
            FROM {SCHEMA}.hadith_items
            GROUP BY collection_name
            ORDER BY collection_name
            """
        )
        for r in rows:
            logger.info("  %-12s total=%d  unlinked=%d",
                        r["collection_name"], r["total"], r["unlinked"])
    finally:
        await conn.close()


def main() -> None:
    logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(name)s - %(message)s")
    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    parser.add_argument("--dsn", required=True)
    asyncio.run(run(parser.parse_args()))


if __name__ == "__main__":
    main()
