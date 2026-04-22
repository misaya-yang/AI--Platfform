#!/usr/bin/env python3
"""Sync real book → chapter → hadith hierarchy from sunnah.com.

The canonical fawazahmed0 CDN we originally sync'd has only 2 levels
(collection → section), so every book in the UI showed "chapter 1"
and nothing else. This script closes that gap by pulling the real
chapter breakdown (≈20-40 chapters per book, EN + AR titles and
intros) from a locally-run AhmedElTabarani/sunnah-hadith-api scraper
and storing it in the ``hadith_chapters`` table.

Prerequisite:

    git clone https://github.com/AhmedElTabarani/sunnah-hadith-api /tmp/sunnah-scraper
    # patch rateLimitMax in config/config.js to 10_000_000
    cd /tmp/sunnah-scraper && npm install && PORT=3333 node server.js &

Then run this script against the server DB (nawawi is skipped because
sunnah.com doesn't expose it under the book/chapter API — it stays at
the existing flat 42-hadith shape):

    python scripts/sync_hadith_chapters_sunnah.py \\
        --dsn "postgresql://postgres:PASSWORD@HOST:5432/gateway" \\
        --scraper http://localhost:3333

The script is idempotent — each (collection, book_number) upsert is
safe to re-run, and ``hadith_items.chapter_ref_id`` is re-assigned
every time.
"""
from __future__ import annotations

import argparse
import asyncio
import logging
import sys

import asyncpg
import httpx

logger = logging.getLogger("sync_chapters")

SCHEMA = "islamic_content"
# Collections our DB knows about. nawawi's 40 Hadith has no book/chapter
# hierarchy on sunnah.com, so the scraper returns empty — skip it.
COLLECTIONS_WITH_CHAPTERS = ["bukhari", "muslim", "abudawud", "tirmidhi", "nasai", "ibnmajah"]


def _clean(value: object) -> str | None:
    if value is None:
        return None
    text = str(value).strip()
    return text or None


async def _fetch_book(client: httpx.AsyncClient, scraper_base: str, coll: str, book: str) -> dict | None:
    url = f"{scraper_base}/v1/site/collections/{coll}/books/{book}"
    try:
        resp = await client.get(url, timeout=90.0)
    except Exception as exc:
        logger.warning("fetch %s/%s failed: %s", coll, book, exc)
        return None
    if resp.status_code != 200:
        logger.warning("fetch %s/%s HTTP %s: %s", coll, book, resp.status_code, resp.text[:200])
        return None
    return resp.json()


async def _sync_book(
    conn: asyncpg.Connection,
    payload: dict,
    coll: str,
    book: str,
) -> tuple[int, int]:
    """Upsert chapter rows and re-link hadith_items. Returns (chapters, linked)."""
    data = payload.get("data") or []
    chapter_rows = []
    for order, ch in enumerate(data, start=1):
        chapter = ch.get("chapter") or {}
        english = chapter.get("english") or {}
        arabic = chapter.get("arabic") or {}
        chapter_rows.append(
            {
                "order": order,
                "raw_id": _clean(chapter.get("id")),
                "title_en": _clean(english.get("name")),
                "title_ar": _clean(arabic.get("name")),
                "intro_en": _clean(english.get("intro")),
                "intro_ar": _clean(arabic.get("intro")),
                "hadith_count": ch.get("numberOfHadith") or 0,
                "hadith_numbers": [
                    str(h.get("reference", {}).get("hadithNumberInCollection"))
                    for h in (ch.get("ahadith") or [])
                    if h.get("reference", {}).get("hadithNumberInCollection") is not None
                ],
            }
        )

    if not chapter_rows:
        return 0, 0

    # Wipe any previous chapters for this book so re-runs don't leak
    # orphaned rows when sunnah.com restructures.
    await conn.execute(
        f"DELETE FROM {SCHEMA}.hadith_chapters WHERE collection_name = $1 AND book_number = $2",
        coll,
        book,
    )

    inserted_ids: list[int] = []
    for row in chapter_rows:
        new_id = await conn.fetchval(
            f"""
            INSERT INTO {SCHEMA}.hadith_chapters
                (collection_name, book_number, chapter_order, chapter_id_raw,
                 title_en, title_ar, intro_en, intro_ar, hadith_count)
            VALUES ($1, $2, $3, $4, $5, $6, $7, $8, $9)
            RETURNING id
            """,
            coll,
            book,
            row["order"],
            row["raw_id"],
            row["title_en"],
            row["title_ar"],
            row["intro_en"],
            row["intro_ar"],
            row["hadith_count"],
        )
        inserted_ids.append(new_id)

    # Link each hadith back to its chapter row via hadithNumberInCollection.
    linked = 0
    for row, chapter_id in zip(chapter_rows, inserted_ids):
        if not row["hadith_numbers"]:
            continue
        result = await conn.execute(
            f"""
            UPDATE {SCHEMA}.hadith_items
            SET chapter_ref_id = $1, updated_at = NOW()
            WHERE collection_name = $2 AND hadith_number = ANY($3)
            """,
            chapter_id,
            coll,
            row["hadith_numbers"],
        )
        try:
            linked += int(result.rsplit(" ", 1)[-1])
        except ValueError:
            pass

    return len(chapter_rows), linked


async def run(args: argparse.Namespace) -> None:
    # Separate connections: one dedicated writer that serialises the DELETE +
    # INSERT + UPDATE trio per book (asyncpg connections are not thread-safe
    # for concurrent statements anyway), and a fat client pool that fans out
    # scrape requests in parallel — the scraper parses HTML in ~1-2s per book
    # so that's where the wall clock sits.
    conn = await asyncpg.connect(args.dsn)
    client = httpx.AsyncClient(limits=httpx.Limits(max_connections=args.concurrency * 2))
    totals = {"chapters": 0, "linked": 0, "books": 0, "skipped_books": 0}
    write_lock = asyncio.Lock()
    try:
        books = await conn.fetch(
            f"""
            SELECT collection_name, book_number
            FROM {SCHEMA}.hadith_books
            WHERE collection_name = ANY($1::text[])
            ORDER BY collection_name, book_number::int
            """,
            COLLECTIONS_WITH_CHAPTERS,
        )
        logger.info("Syncing chapters for %d books across %d collections (concurrency=%d)",
                    len(books), len(COLLECTIONS_WITH_CHAPTERS), args.concurrency)

        pending = [
            row for row in books
            if not args.only_collection or row["collection_name"] == args.only_collection
        ]
        if args.skip_existing:
            already_done = {
                (r["collection_name"], r["book_number"])
                for r in await conn.fetch(
                    f"SELECT DISTINCT collection_name, book_number FROM {SCHEMA}.hadith_chapters"
                )
            }
            before = len(pending)
            pending = [r for r in pending if (r["collection_name"], r["book_number"]) not in already_done]
            logger.info("--skip-existing: %d books already synced, %d remaining", before - len(pending), len(pending))

        sem = asyncio.Semaphore(args.concurrency)
        done_counter = {"n": 0}
        total = len(pending)

        async def process(row) -> None:
            coll, book = row["collection_name"], row["book_number"]
            async with sem:
                payload = await _fetch_book(client, args.scraper, coll, book)
            if not payload or payload.get("status") != "success":
                totals["skipped_books"] += 1
                return
            try:
                # Writer must be serialised: both _sync_book and the
                # per-chapter INSERT RETURNING id would otherwise clobber
                # each other on one asyncpg connection.
                async with write_lock:
                    chapters, linked = await _sync_book(conn, payload, coll, book)
            except Exception as exc:
                logger.error("upsert %s/%s failed: %s", coll, book, exc)
                totals["skipped_books"] += 1
                return
            totals["books"] += 1
            totals["chapters"] += chapters
            totals["linked"] += linked
            done_counter["n"] += 1
            if done_counter["n"] % 10 == 0 or args.verbose:
                logger.info("  [%d/%d] %s/%s → %d chapters, %d hadiths linked",
                            done_counter["n"], total, coll, book, chapters, linked)

        await asyncio.gather(*(process(row) for row in pending))

        # Flip has_chapters=true for collections that now have real chapters.
        await conn.execute(
            f"""
            UPDATE {SCHEMA}.hadith_collections
            SET has_chapters = TRUE, updated_at = NOW()
            WHERE name IN (
                SELECT DISTINCT collection_name FROM {SCHEMA}.hadith_chapters
            )
            """
        )

        logger.info("=== DONE ===")
        logger.info("  books synced:    %d", totals["books"])
        logger.info("  books skipped:   %d", totals["skipped_books"])
        logger.info("  chapters:        %d", totals["chapters"])
        logger.info("  hadith linked:   %d", totals["linked"])

    finally:
        await client.aclose()
        await conn.close()


def main() -> None:
    logging.basicConfig(
        level=logging.INFO,
        format="%(asctime)s %(levelname)s %(name)s - %(message)s",
    )
    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    parser.add_argument("--dsn", required=True, help="PostgreSQL DSN")
    parser.add_argument(
        "--scraper",
        default="http://localhost:3333",
        help="Base URL of running AhmedElTabarani scraper",
    )
    parser.add_argument(
        "--concurrency",
        type=int,
        default=4,
        help="Parallel scraper requests (sunnah.com handles ~4-5 concurrent politely)",
    )
    parser.add_argument(
        "--only-collection",
        default=None,
        help="Restrict to one collection (debug)",
    )
    parser.add_argument(
        "--skip-existing",
        action="store_true",
        help="Skip (collection, book_number) pairs that already have chapter rows",
    )
    parser.add_argument("-v", "--verbose", action="store_true")
    args = parser.parse_args()
    try:
        asyncio.run(run(args))
    except KeyboardInterrupt:
        sys.exit(130)


if __name__ == "__main__":
    main()
