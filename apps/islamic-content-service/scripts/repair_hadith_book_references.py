#!/usr/bin/env python3
"""Repair Hadith item book_number using source reference.book metadata.

The fawazahmed0 CDN sometimes includes Hadith rows with an empty metadata
section 0 while each row still has a valid ``reference.book``. This script moves
those rows from DB book 0 into their source-backed book, preserving chapter
titles and merging with an existing chapter when possible.
"""

from __future__ import annotations

import argparse
import asyncio
import json
import re
import urllib.parse
import urllib.request
from dataclasses import dataclass
from typing import Any

import asyncpg

SCHEMA = "islamic_content"
FAWAZ_BASE_URL = "https://raw.githubusercontent.com/fawazahmed0/hadith-api/1/editions"


@dataclass(frozen=True)
class MoveCandidate:
    item_id: int
    old_chapter_id: int
    collection_name: str
    hadith_number: str
    target_book_number: str
    title_en: str
    title_ar: str


def _clean(value: Any) -> str:
    return re.sub(r"\s+", " ", str(value or "")).strip()


def _fetch_json(url: str) -> dict[str, Any]:
    with urllib.request.urlopen(url, timeout=180) as response:
        payload = json.load(response)
    if not isinstance(payload, dict):
        raise TypeError(f"Expected JSON object from {url}")
    return payload


def load_reference_books(collection: str) -> dict[str, str]:
    url = f"{FAWAZ_BASE_URL}/eng-{urllib.parse.quote(collection)}.min.json"
    payload = _fetch_json(url)
    refs: dict[str, str] = {}
    for hadith in payload.get("hadiths", []):
        number = _clean(hadith.get("hadithnumber"))
        reference = hadith.get("reference") or {}
        book = _clean(reference.get("book"))
        if number and book:
            refs[number] = book
    return refs


async def _book0_candidates(conn: asyncpg.Connection, collection: str) -> list[asyncpg.Record]:
    return await conn.fetch(
        f"""
        SELECT
            hi.id AS item_id,
            hi.collection_name,
            hi.hadith_number,
            hi.chapter_ref_id AS old_chapter_id,
            coalesce(hc.title_en, '') AS title_en,
            coalesce(hc.title_ar, '') AS title_ar
        FROM {SCHEMA}.hadith_items hi
        JOIN {SCHEMA}.hadith_chapters hc ON hc.id = hi.chapter_ref_id
        WHERE hi.collection_name = $1
          AND hi.book_number = '0'
        ORDER BY hi.hadith_number
        """,
        collection,
    )


async def _target_chapter_id(
    conn: asyncpg.Connection,
    *,
    collection: str,
    target_book_number: str,
    title_en: str,
    title_ar: str,
) -> int | None:
    return await conn.fetchval(
        f"""
        SELECT id
        FROM {SCHEMA}.hadith_chapters
        WHERE collection_name = $1
          AND book_number = $2
          AND (
              ($3 <> '' AND trim(coalesce(title_en, '')) = $3)
              OR ($4 <> '' AND trim(coalesce(title_ar, '')) = $4)
          )
        ORDER BY chapter_order
        LIMIT 1
        """,
        collection,
        target_book_number,
        title_en,
        title_ar,
    )


async def _create_chapter(
    conn: asyncpg.Connection,
    *,
    collection: str,
    target_book_number: str,
    title_en: str,
    title_ar: str,
) -> int:
    chapter_order = await conn.fetchval(
        f"""
        SELECT coalesce(max(chapter_order), 0) + 1
        FROM {SCHEMA}.hadith_chapters
        WHERE collection_name = $1 AND book_number = $2
        """,
        collection,
        target_book_number,
    )
    return int(
        await conn.fetchval(
            f"""
            INSERT INTO {SCHEMA}.hadith_chapters (
                collection_name,
                book_number,
                chapter_order,
                chapter_id_raw,
                title_en,
                title_ar,
                hadith_count,
                source_api,
                created_at,
                updated_at
            )
            VALUES ($1, $2, $3, $4, $5, $6, 0, 'hadith-cdn-reference', NOW(), NOW())
            RETURNING id
            """,
            collection,
            target_book_number,
            int(chapter_order or 1),
            f"REF-{target_book_number}-{int(chapter_order or 1)}",
            title_en or None,
            title_ar or None,
        )
    )


async def repair(args: argparse.Namespace) -> dict[str, int]:
    reference_books = load_reference_books(args.collection)
    stats = {
        "book0_items": 0,
        "movable_items": 0,
        "created_chapters": 0,
        "merged_chapters": 0,
        "moved_items": 0,
        "left_in_book0": 0,
    }
    conn = await asyncpg.connect(args.dsn)
    try:
        rows = await _book0_candidates(conn, args.collection)
        stats["book0_items"] = len(rows)
        moves: list[MoveCandidate] = []
        for row in rows:
            target_book = reference_books.get(str(row["hadith_number"]))
            if not target_book or target_book == "0":
                stats["left_in_book0"] += 1
                continue
            moves.append(
                MoveCandidate(
                    item_id=int(row["item_id"]),
                    old_chapter_id=int(row["old_chapter_id"]),
                    collection_name=row["collection_name"],
                    hadith_number=str(row["hadith_number"]),
                    target_book_number=target_book,
                    title_en=_clean(row["title_en"]),
                    title_ar=_clean(row["title_ar"]),
                )
            )
        stats["movable_items"] = len(moves)

        target_cache: dict[tuple[str, str, str, str], int] = {}
        async with conn.transaction():
            for move in moves:
                cache_key = (
                    move.collection_name,
                    move.target_book_number,
                    move.title_en,
                    move.title_ar,
                )
                target_id = target_cache.get(cache_key)
                if target_id is None:
                    target_id = await _target_chapter_id(
                        conn,
                        collection=move.collection_name,
                        target_book_number=move.target_book_number,
                        title_en=move.title_en,
                        title_ar=move.title_ar,
                    )
                    if target_id is None:
                        stats["created_chapters"] += 1
                        if args.dry_run:
                            target_id = -stats["created_chapters"]
                        else:
                            target_id = await _create_chapter(
                                conn,
                                collection=move.collection_name,
                                target_book_number=move.target_book_number,
                                title_en=move.title_en,
                                title_ar=move.title_ar,
                            )
                    else:
                        stats["merged_chapters"] += 1
                    target_cache[cache_key] = int(target_id)

                stats["moved_items"] += 1
                if args.dry_run:
                    continue
                await conn.execute(
                    f"""
                    UPDATE {SCHEMA}.hadith_items
                    SET book_number = $1,
                        chapter_ref_id = $2,
                        updated_at = NOW()
                    WHERE id = $3
                    """,
                    move.target_book_number,
                    target_id,
                    move.item_id,
                )

            if not args.dry_run:
                await conn.execute(
                    f"""
                    DELETE FROM {SCHEMA}.hadith_chapters hc
                    WHERE NOT EXISTS (
                        SELECT 1 FROM {SCHEMA}.hadith_items hi WHERE hi.chapter_ref_id = hc.id
                    )
                    """
                )
                await conn.execute(
                    f"""
                    UPDATE {SCHEMA}.hadith_chapters hc
                    SET hadith_count = counts.count
                    FROM (
                        SELECT chapter_ref_id, COUNT(*)::int AS count
                        FROM {SCHEMA}.hadith_items
                        GROUP BY chapter_ref_id
                    ) counts
                    WHERE counts.chapter_ref_id = hc.id
                    """
                )
                await conn.execute(
                    f"""
                    UPDATE {SCHEMA}.hadith_books hb
                    SET number_of_hadith = counts.count
                    FROM (
                        SELECT collection_name, book_number, COUNT(*)::int AS count
                        FROM {SCHEMA}.hadith_items
                        GROUP BY collection_name, book_number
                    ) counts
                    WHERE counts.collection_name = hb.collection_name
                      AND counts.book_number = hb.book_number
                    """
                )
                await conn.execute(
                    f"""
                    UPDATE {SCHEMA}.hadith_books hb
                    SET number_of_hadith = 0
                    WHERE NOT EXISTS (
                        SELECT 1
                        FROM {SCHEMA}.hadith_items hi
                        WHERE hi.collection_name = hb.collection_name
                          AND hi.book_number = hb.book_number
                    )
                    """
                )
                await conn.execute(
                    f"""
                    UPDATE {SCHEMA}.hadith_collections hc
                    SET total_hadith = counts.count
                    FROM (
                        SELECT collection_name, COUNT(*)::int AS count
                        FROM {SCHEMA}.hadith_items
                        GROUP BY collection_name
                    ) counts
                    WHERE counts.collection_name = hc.name
                    """
                )
    finally:
        await conn.close()
    return stats


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--dsn", required=True)
    parser.add_argument("--collection", default="bukhari")
    parser.add_argument("--dry-run", action="store_true")
    args = parser.parse_args()
    stats = asyncio.run(repair(args))
    for key, value in stats.items():
        print(f"{key}: {value}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
