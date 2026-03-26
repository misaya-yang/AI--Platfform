#!/usr/bin/env python3
"""Backfill chapter_title and book title from fawazahmed0 CDN.

Fixes: chapter_title is empty for all 34K+ hadith_items in PostgreSQL.
Root cause: the original sync didn't fetch section/chapter metadata from CDN.

This script:
1. Downloads each collection's bulk JSON from CDN (has sections + section_details)
2. Builds a mapping: hadith_number → chapter_title (using section_details ranges)
3. Updates hadith_items.chapter_title, hadith_books.title, and hadith_localizations.chapter_title
4. Also backfills chapter_id (section number)

Usage:
    python scripts/backfill_hadith_chapters.py --dsn "postgresql://postgres:PASSWORD@HOST:5432/gateway"
    python scripts/backfill_hadith_chapters.py --dry-run
"""
from __future__ import annotations

import argparse
import asyncio
import logging
import sys

import httpx
import asyncpg

logger = logging.getLogger("backfill_chapters")

CDN_BASE = "https://cdn.jsdelivr.net/gh/fawazahmed0/hadith-api@1"
COLLECTIONS = ["bukhari", "muslim", "abudawud", "tirmidhi", "nasai", "ibnmajah", "nawawi"]
SCHEMA = "islamic_content"


def _build_hadith_to_section(sections: dict, section_details: dict) -> dict[int, tuple[str, str]]:
    """Build mapping: hadith_number → (section_number, chapter_title)."""
    mapping: dict[int, tuple[str, str]] = {}
    for sec_num, title in sections.items():
        detail = section_details.get(sec_num, {})
        first = detail.get("hadithnumber_first")
        last = detail.get("hadithnumber_last")
        if first is None or last is None:
            continue
        for h in range(int(first), int(last) + 1):
            mapping[h] = (str(sec_num), str(title))
    return mapping


async def backfill_collection(conn: asyncpg.Connection, client: httpx.Client, coll_name: str, dry_run: bool) -> dict:
    """Backfill one collection."""
    url = f"{CDN_BASE}/editions/eng-{coll_name}.json"
    logger.info("Downloading %s ...", url)
    try:
        resp = client.get(url)
        resp.raise_for_status()
        data = resp.json()
    except Exception as exc:
        logger.error("Failed to download %s: %s", coll_name, exc)
        return {"collection": coll_name, "error": str(exc)}

    meta = data.get("metadata", {})
    sections = meta.get("sections", {})
    section_details = meta.get("section_details", {})
    coll_display_name = meta.get("name", coll_name.title())

    if not sections:
        logger.warning("No sections found for %s", coll_name)
        return {"collection": coll_name, "sections": 0, "updated": 0}

    mapping = _build_hadith_to_section(sections, section_details)
    logger.info("  %s: %d sections, %d hadith→section mappings", coll_name, len(sections), len(mapping))

    if dry_run:
        # Show sample mappings
        for hnum in sorted(mapping.keys())[:5]:
            sec_num, title = mapping[hnum]
            logger.info("    [DRY-RUN] hadith %d → section %s: %s", hnum, sec_num, title)
        return {"collection": coll_name, "sections": len(sections), "mappings": len(mapping), "dry_run": True}

    # Step 1: Update hadith_books titles
    book_updates = 0
    for sec_num, title in sections.items():
        if not title:
            continue
        result = await conn.execute(
            f"""
            UPDATE {SCHEMA}.hadith_books
            SET title = $1, updated_at = NOW()
            WHERE collection_name = $2 AND book_number = $3 AND (title IS NULL OR title = '')
            """,
            title, coll_name, str(sec_num),
        )
        if "UPDATE 1" in result:
            book_updates += 1

    # Step 2: Update hadith_items chapter_title and chapter_id
    hadith_updates = 0
    # Batch by section for efficiency
    for sec_num, title in sections.items():
        if not title:
            continue
        detail = section_details.get(sec_num, {})
        first = detail.get("hadithnumber_first")
        last = detail.get("hadithnumber_last")
        if first is None or last is None:
            continue

        # Get all hadith numbers in this range
        hadith_numbers = [str(h) for h in range(int(first), int(last) + 1)]
        if not hadith_numbers:
            continue

        result = await conn.execute(
            f"""
            UPDATE {SCHEMA}.hadith_items
            SET chapter_title = $1, chapter_id = $2, updated_at = NOW()
            WHERE collection_name = $3 AND hadith_number = ANY($4)
            """,
            title, str(sec_num), coll_name, hadith_numbers,
        )
        count = int(result.split()[-1]) if result else 0
        hadith_updates += count

    # Step 3: Update hadith_localizations chapter_title
    loc_updates = 0
    for sec_num, title in sections.items():
        if not title:
            continue
        detail = section_details.get(sec_num, {})
        first = detail.get("hadithnumber_first")
        last = detail.get("hadithnumber_last")
        if first is None or last is None:
            continue

        hadith_numbers = [str(h) for h in range(int(first), int(last) + 1)]
        if not hadith_numbers:
            continue

        result = await conn.execute(
            f"""
            UPDATE {SCHEMA}.hadith_localizations hl
            SET chapter_title = $1, updated_at = NOW()
            FROM {SCHEMA}.hadith_items hi
            WHERE hl.hadith_item_id = hi.id
              AND hi.collection_name = $2
              AND hi.hadith_number = ANY($3)
            """,
            title, coll_name, hadith_numbers,
        )
        count = int(result.split()[-1]) if result else 0
        loc_updates += count

    logger.info("  %s: books=%d, hadiths=%d, localizations=%d updated",
                coll_name, book_updates, hadith_updates, loc_updates)

    return {
        "collection": coll_name,
        "display_name": coll_display_name,
        "sections": len(sections),
        "book_updates": book_updates,
        "hadith_updates": hadith_updates,
        "localization_updates": loc_updates,
    }


async def run(args):
    dsn = args.dsn
    logger.info("Connecting to %s ...", dsn.split("@")[-1] if "@" in dsn else dsn)
    conn = await asyncpg.connect(dsn)

    try:
        # Verify the table exists and check current state
        total = await conn.fetchval(f"SELECT COUNT(*) FROM {SCHEMA}.hadith_items")
        empty = await conn.fetchval(
            f"SELECT COUNT(*) FROM {SCHEMA}.hadith_items WHERE chapter_title IS NULL OR chapter_title = ''"
        )
        logger.info("Current state: %d total hadiths, %d with empty chapter_title", total, empty)

        if empty == 0:
            logger.info("All hadiths already have chapter_title. Nothing to do.")
            return

        client = httpx.Client(timeout=120.0, transport=httpx.HTTPTransport(retries=3))
        results = []

        colls = [c.strip() for c in args.collections.split(",") if c.strip()]
        for coll_name in colls:
            result = await backfill_collection(conn, client, coll_name, args.dry_run)
            results.append(result)

        client.close()

        # Final verification
        if not args.dry_run:
            still_empty = await conn.fetchval(
                f"SELECT COUNT(*) FROM {SCHEMA}.hadith_items WHERE chapter_title IS NULL OR chapter_title = ''"
            )
            logger.info("=== DONE: %d/%d hadiths still have empty chapter_title ===", still_empty, total)

        # Summary
        total_updated = sum(r.get("hadith_updates", 0) for r in results)
        logger.info("Total hadith_items updated: %d", total_updated)

    finally:
        await conn.close()


def main():
    logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(name)s - %(message)s")
    p = argparse.ArgumentParser(description="Backfill hadith chapter_title from CDN")
    p.add_argument(
        "--dsn",
        default="postgresql://postgres:HejazDB2026Secure@52.65.136.42:5432/gateway",
        help="PostgreSQL DSN",
    )
    p.add_argument("--collections", default=",".join(COLLECTIONS))
    p.add_argument("--dry-run", action="store_true")
    raise SystemExit(asyncio.run(run(p.parse_args())))


if __name__ == "__main__":
    main()
