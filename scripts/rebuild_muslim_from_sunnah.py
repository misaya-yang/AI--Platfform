#!/usr/bin/env python3
"""Rebuild Sahih Muslim's hadith data from sunnah.com (Plan B).

Fixes: Muslim had only 14% of hadith_items linked to their chapter because
``fawazahmed0`` uses the canonical Abdul Baqi numbering (1..7563) while
sunnah.com — and therefore our new ``hadith_chapters`` table — uses the
modern numbering ("8", "11a", "14b"…). The two never line up by integer
hadith_number, so the chapter-linking JOIN fell through.

This script replaces Muslim's hadith data with sunnah.com's version.

Fast path: bulk inserts via asyncpg ``copy_records_to_table`` (COPY
protocol). On a localhost DB the whole rebuild takes seconds; over an
SSH tunnel it takes 1-2 minutes instead of 20+ minutes for single-row
INSERTs.

Usage:
    python scripts/rebuild_muslim_from_sunnah.py \\
        --dsn "postgresql://postgres:PW@HOST:5432/gateway" \\
        --scraper http://localhost:3333
"""
from __future__ import annotations

import argparse
import asyncio
import logging
from typing import Any

import asyncpg
import httpx

logger = logging.getLogger("rebuild_muslim")

SCHEMA = "islamic_content"
COLL = "muslim"


def _clean(value: Any) -> str | None:
    if value is None:
        return None
    text = str(value).strip()
    return text or None


def _short(value: Any, limit: int = 250) -> str | None:
    """Truncate to fit legacy VARCHAR(255) columns — canonical full title
    lives in hadith_chapters.title_en (TEXT), so losing trailing bytes is
    safe for these legacy mirror fields."""
    text = _clean(value)
    if text is None:
        return None
    return text if len(text) <= limit else text[:limit - 1] + "…"


async def _fetch_book(client: httpx.AsyncClient, scraper: str, book: str,
                      retries: int = 3) -> dict | None:
    """Fetch one book with up to N retries — sunnah.com occasionally 500s or
    times out on the heavier books (Kitab al-Hajj etc.); a single retry
    with short backoff is enough to recover in >95% of cases."""
    url = f"{scraper}/v1/site/collections/{COLL}/books/{book}"
    last_exc: Exception | None = None
    for attempt in range(1, retries + 1):
        try:
            resp = await client.get(url, timeout=120.0)
            resp.raise_for_status()
            return resp.json()
        except Exception as exc:
            last_exc = exc
            if attempt < retries:
                await asyncio.sleep(2 * attempt)
    logger.warning("fetch %s/%s failed after %d attempts: %s",
                   COLL, book, retries, last_exc)
    return None


async def _collect_all(scraper: str, books: list[str], concurrency: int) -> dict[str, dict]:
    payloads: dict[str, dict] = {}
    sem = asyncio.Semaphore(concurrency)

    async def worker(client: httpx.AsyncClient, book: str) -> None:
        async with sem:
            payload = await _fetch_book(client, scraper, book)
        if payload and payload.get("status") == "success":
            payloads[book] = payload
            logger.info("  fetched %s/%s — %s chapters, %s hadiths",
                        COLL, book,
                        payload["metadata"].get("numberOfChapters"),
                        payload["metadata"].get("numberOfHadith"))
        else:
            logger.error("  missing payload for %s/%s", COLL, book)

    async with httpx.AsyncClient(limits=httpx.Limits(max_connections=concurrency * 2)) as client:
        await asyncio.gather(*(worker(client, b) for b in books))
    return payloads


async def _rebuild_fast(conn: asyncpg.Connection, payloads: dict[str, dict]) -> dict[str, int]:
    """Bulk rebuild using asyncpg COPY protocol for the two big tables."""
    stats = {"chapters": 0, "hadiths": 0, "localizations": 0}

    # ------------------------------------------------------------------
    # Step 1: stage rows in Python first (no DB roundtrips yet)
    # ------------------------------------------------------------------
    # chapter rows keyed by (book, chapter_order) so we can later link
    # hadith items to chapter PKs without a second pass.
    chapter_rows: list[tuple] = []
    # each hadith row carries the (book, chapter_order) key that we'll
    # resolve to chapter_ref_id after chapters are inserted.
    hadith_rows: list[dict[str, Any]] = []

    for book in sorted(payloads, key=lambda b: int(b) if b.isdigit() else 999):
        data = payloads[book].get("data") or []
        for order, ch in enumerate(data, start=1):
            chapter = ch.get("chapter") or {}
            english = chapter.get("english") or {}
            arabic = chapter.get("arabic") or {}
            chapter_rows.append((
                COLL,
                book,
                order,
                _clean(chapter.get("id")),
                _clean(english.get("name")),
                _clean(arabic.get("name")),
                _clean(english.get("intro")),
                _clean(arabic.get("intro")),
                ch.get("numberOfHadith") or 0,
            ))

            for hadith in (ch.get("ahadith") or []):
                ref = hadith.get("reference") or {}
                hadith_num = _clean(ref.get("hadithNumberInCollection"))
                if not hadith_num:
                    continue

                en = hadith.get("english") or {}
                ar = hadith.get("arabic") or {}
                hadith_rows.append({
                    "book": book,
                    "chapter_order": order,
                    "chapter_id_fk": order,  # placeholder — resolved post-insert
                    "hadith_number": hadith_num,
                    "chapter_title_en": _short(english.get("name")) or "",
                    "chapter_title_ar": _short(arabic.get("name")) or "",
                    "en_body": _clean(en.get("fullHadith")) or _clean(en.get("hadith")) or "",
                    "ar_body": _clean(ar.get("fullHadith")) or _clean(ar.get("hadith")) or "",
                })

    logger.info("staged %d chapters + %d hadiths in memory",
                len(chapter_rows), len(hadith_rows))

    # ------------------------------------------------------------------
    # Step 2: destructive + bulk insert inside one transaction
    # ------------------------------------------------------------------
    async with conn.transaction():
        await conn.execute(
            f"DELETE FROM {SCHEMA}.hadith_items WHERE collection_name = $1", COLL,
        )
        await conn.execute(
            f"DELETE FROM {SCHEMA}.hadith_chapters WHERE collection_name = $1", COLL,
        )

        # ---- chapters (bulk via COPY) --------------------------------
        await conn.copy_records_to_table(
            "hadith_chapters",
            schema_name=SCHEMA,
            records=chapter_rows,
            columns=[
                "collection_name", "book_number", "chapter_order", "chapter_id_raw",
                "title_en", "title_ar", "intro_en", "intro_ar", "hadith_count",
            ],
        )
        stats["chapters"] = len(chapter_rows)

        # Build (book, order) → chapter.id map for hadith linkage.
        chapter_pk_rows = await conn.fetch(
            f"SELECT id, book_number, chapter_order FROM {SCHEMA}.hadith_chapters "
            f"WHERE collection_name = $1",
            COLL,
        )
        pk_map = {
            (row["book_number"], row["chapter_order"]): row["id"]
            for row in chapter_pk_rows
        }

        # ---- hadith_items (bulk via COPY) ----------------------------
        hadith_records = [
            (
                COLL,
                h["book"],
                str(h["chapter_order"]),
                h["hadith_number"],
                h["chapter_title_en"] or h["chapter_title_ar"],
                "sunnah.com",
                pk_map.get((h["book"], h["chapter_order"])),
            )
            for h in hadith_rows
        ]
        await conn.copy_records_to_table(
            "hadith_items",
            schema_name=SCHEMA,
            records=hadith_records,
            columns=[
                "collection_name", "book_number", "chapter_id", "hadith_number",
                "chapter_title", "source_api", "chapter_ref_id",
            ],
        )
        stats["hadiths"] = len(hadith_records)

        # ---- localizations (bulk, joined by hadith_number) ----------
        # Need the freshly-minted hadith_items.id to FK localizations.
        item_id_rows = await conn.fetch(
            f"SELECT id, hadith_number FROM {SCHEMA}.hadith_items "
            f"WHERE collection_name = $1",
            COLL,
        )
        item_id_map = {r["hadith_number"]: r["id"] for r in item_id_rows}

        loc_records: list[tuple] = []
        for h in hadith_rows:
            item_id = item_id_map.get(h["hadith_number"])
            if item_id is None:
                continue
            if h["en_body"]:
                loc_records.append((item_id, "en", h["chapter_title_en"], h["en_body"]))
            if h["ar_body"]:
                loc_records.append((item_id, "ar", h["chapter_title_ar"], h["ar_body"]))

        await conn.copy_records_to_table(
            "hadith_localizations",
            schema_name=SCHEMA,
            records=loc_records,
            columns=["hadith_item_id", "language", "chapter_title", "body_text"],
        )
        stats["localizations"] = len(loc_records)

        # ---- collection summary --------------------------------------
        await conn.execute(
            f"""
            UPDATE {SCHEMA}.hadith_collections
            SET total_hadith = (SELECT COUNT(*) FROM {SCHEMA}.hadith_items WHERE collection_name = $1),
                has_chapters = TRUE,
                source_api   = 'sunnah.com',
                updated_at   = NOW()
            WHERE name = $1
            """,
            COLL,
        )

    return stats


async def run(args: argparse.Namespace) -> None:
    conn = await asyncpg.connect(args.dsn)
    try:
        books = [
            str(r["book_number"])
            for r in await conn.fetch(
                f"SELECT book_number FROM {SCHEMA}.hadith_books "
                f"WHERE collection_name = $1 ORDER BY (book_number::int)",
                COLL,
            )
        ]
        logger.info("fetching %d Muslim books (concurrency=%d)",
                    len(books), args.concurrency)

        payloads = await _collect_all(args.scraper, books, args.concurrency)
        missing = set(books) - set(payloads)
        if missing:
            logger.warning("missing %d books — continuing: %s",
                           len(missing), sorted(missing))

        logger.info("rebuilding via COPY ...")
        stats = await _rebuild_fast(conn, payloads)

        total_items = await conn.fetchval(
            f"SELECT count(*) FROM {SCHEMA}.hadith_items WHERE collection_name = $1", COLL,
        )
        linked = await conn.fetchval(
            f"SELECT count(*) FROM {SCHEMA}.hadith_items "
            f"WHERE collection_name = $1 AND chapter_ref_id IS NOT NULL", COLL,
        )
        chapters_total = await conn.fetchval(
            f"SELECT count(*) FROM {SCHEMA}.hadith_chapters WHERE collection_name = $1", COLL,
        )

        logger.info("=== DONE ===")
        logger.info("  chapters: %d  hadith_items: %d  localizations: %d",
                    stats["chapters"], stats["hadiths"], stats["localizations"])
        logger.info("  DB: items=%d chapters=%d linked=%d (%.1f%%)",
                    total_items, chapters_total, linked,
                    (linked / total_items * 100) if total_items else 0)

    finally:
        await conn.close()


def main() -> None:
    logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(name)s - %(message)s")
    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    parser.add_argument("--dsn", required=True)
    parser.add_argument("--scraper", default="http://localhost:3333")
    parser.add_argument("--concurrency", type=int, default=5)
    asyncio.run(run(parser.parse_args()))


if __name__ == "__main__":
    main()
