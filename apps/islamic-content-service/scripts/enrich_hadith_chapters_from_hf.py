#!/usr/bin/env python3
"""Repair placeholder Hadith chapter titles using external source metadata.

The sunnah.com scraper gives us detailed book -> HTML-anchor chapter linkage,
but some source anchors are literal placeholders such as "Chapter:" / "باب".
meeAtif's HuggingFace CSVs carry per-hadith Reference plus chapter title fields;
AhmedBaset/hadith-json carries clean collection-level chapter metadata. This
script uses both to repair only missing/placeholder chapter fields while
preserving the existing hadith text, grades, and chapter_ref_id model.
"""

from __future__ import annotations

import argparse
import asyncio
import csv
import io
import json
import re
import urllib.parse
import urllib.request
from dataclasses import dataclass
from typing import Any

import asyncpg

SCHEMA = "islamic_content"
HF_BASE_URL = "https://huggingface.co/datasets/meeAtif/hadith_datasets/resolve/main"
AHMEDBASET_BASE_URL = "https://raw.githubusercontent.com/AhmedBaset/hadith-json/main/db/by_book/the_9_books"
HF_FILES = {
    "bukhari": "Sahih al-Bukhari.csv",
    "muslim": "Sahih Muslim.csv",
    "abudawud": "Sunan Abi Dawud.csv",
    "tirmidhi": "Jami` at-Tirmidhi.csv",
    "nasai": "Sunan an-Nasa'i.csv",
    "ibnmajah": "Sunan Ibn Majah.csv",
}
PLACEHOLDER_EN = {
    "",
    "chapter",
    "chapter:",
    "additional hadiths (not grouped by sunnah.com)",
    "introduction (unmapped preamble hadiths)",
}
PLACEHOLDER_AR = {"", "باب", "باب:", "باب :", "،", ".", ":"}
BIDI_NOISE_CHARS = (
    "\u200e",
    "\u200f",
    "\u202a",
    "\u202b",
    "\u202c",
    "\u202d",
    "\u202e",
    "\u2066",
    "\u2067",
    "\u2068",
    "\u2069",
    "\ufffd",
)


@dataclass(frozen=True)
class HfChapter:
    chapter_number: str
    title_en: str
    title_ar: str


def _clean(value: Any) -> str:
    text = str(value or "")
    for char in BIDI_NOISE_CHARS:
        text = text.replace(char, "")
    return re.sub(r"\s+", " ", text).strip()


def _is_placeholder_title(title_en: str | None, title_ar: str | None) -> bool:
    en = _clean(title_en).casefold()
    ar = _clean(title_ar)
    return en in PLACEHOLDER_EN or ar in PLACEHOLDER_AR


def _is_placeholder_en(value: str | None) -> bool:
    return _clean(value).casefold() in PLACEHOLDER_EN


def _is_placeholder_ar(value: str | None) -> bool:
    return _clean(value) in PLACEHOLDER_AR


def _reference_number(reference: str) -> str | None:
    match = re.search(r":([^/:?#]+)(?:[?#].*)?$", reference or "")
    return match.group(1) if match else None


def load_hf_metadata(collections: set[str]) -> dict[tuple[str, str], HfChapter]:
    metadata: dict[tuple[str, str], HfChapter] = {}
    for collection in sorted(collections):
        filename = HF_FILES[collection]
        url = f"{HF_BASE_URL}/{urllib.parse.quote(filename)}"
        with urllib.request.urlopen(url, timeout=120) as response:
            wrapper = io.TextIOWrapper(
                response,
                encoding="utf-8-sig",
                errors="ignore",
                newline="",
            )
            for row in csv.DictReader(wrapper):
                hadith_number = _reference_number(row.get("Reference", ""))
                if not hadith_number:
                    continue
                title_en = _clean(row.get("Chapter_Title_English"))
                title_ar = _clean(row.get("Chapter_Title_Arabic"))
                if _is_placeholder_title(title_en, title_ar):
                    continue
                metadata[(collection, hadith_number)] = HfChapter(
                    chapter_number=_clean(row.get("Chapter_Number")),
                    title_en=title_en,
                    title_ar=title_ar,
                )
    return metadata


def load_ahmedbaset_metadata(collections: set[str]) -> dict[tuple[str, str], HfChapter]:
    metadata: dict[tuple[str, str], HfChapter] = {}
    for collection in sorted(collections):
        url = f"{AHMEDBASET_BASE_URL}/{collection}.json"
        with urllib.request.urlopen(url, timeout=120) as response:
            payload = json.load(response)
        chapters_by_id = {
            chapter.get("id"): chapter
            for chapter in payload.get("chapters", [])
        }
        for hadith in payload.get("hadiths", []):
            hadith_number = _clean(hadith.get("id"))
            chapter = chapters_by_id.get(hadith.get("chapterId"))
            if not hadith_number or not chapter:
                continue
            title_en = _clean(chapter.get("english"))
            title_ar = _clean(chapter.get("arabic"))
            if _is_placeholder_title(title_en, title_ar):
                continue
            metadata[(collection, hadith_number)] = HfChapter(
                chapter_number=_clean(chapter.get("id")),
                title_en=title_en,
                title_ar=title_ar,
            )
    return metadata


def load_ahmedbaset_book_titles(collections: set[str]) -> dict[tuple[str, str], tuple[str, str]]:
    titles: dict[tuple[str, str], tuple[str, str]] = {}
    for collection in sorted(collections):
        url = f"{AHMEDBASET_BASE_URL}/{collection}.json"
        with urllib.request.urlopen(url, timeout=120) as response:
            payload = json.load(response)
        for chapter in payload.get("chapters", []):
            chapter_id = _clean(chapter.get("id"))
            title_en = _clean(chapter.get("english"))
            title_ar = _clean(chapter.get("arabic"))
            if chapter_id and not _is_placeholder_title(title_en, title_ar):
                titles[(collection, chapter_id)] = (title_en, title_ar)
    return titles


async def _placeholder_chapters(
    conn: asyncpg.Connection,
    collections: set[str],
) -> list[asyncpg.Record]:
    return await conn.fetch(
        f"""
        SELECT
            hc.id,
            hc.collection_name,
            hc.book_number,
            hb.title AS book_title,
            hc.chapter_order,
            hc.title_en,
            hc.title_ar,
            hc.source_api,
            array_agg(hi.hadith_number ORDER BY hi.id) AS hadith_numbers
        FROM {SCHEMA}.hadith_chapters hc
        LEFT JOIN {SCHEMA}.hadith_books hb
            ON hb.collection_name = hc.collection_name
           AND hb.book_number = hc.book_number
        JOIN {SCHEMA}.hadith_items hi ON hi.chapter_ref_id = hc.id
        WHERE hc.collection_name = ANY($1::text[])
          AND (
              lower(trim(coalesce(hc.title_en, ''))) IN (
                  '',
                  'chapter',
                  'chapter:',
                  'additional hadiths (not grouped by sunnah.com)',
                  'introduction (unmapped preamble hadiths)'
              )
              OR trim(coalesce(hc.title_ar, '')) IN ('', 'باب', 'باب:', 'باب :', '،', '.', ':')
          )
        GROUP BY hc.id, hb.title
        ORDER BY hc.collection_name, hc.book_number, hc.chapter_order
        """,
        sorted(collections),
    )


async def _matching_chapter_id(
    conn: asyncpg.Connection,
    *,
    collection: str,
    book_number: str,
    current_id: int,
    title_en: str,
    title_ar: str,
) -> int | None:
    return await conn.fetchval(
        f"""
        SELECT id
        FROM {SCHEMA}.hadith_chapters
        WHERE collection_name = $1
          AND book_number = $2
          AND id <> $3
          AND (
              trim(coalesce(title_en, '')) = $4
              OR trim(coalesce(title_ar, '')) = $5
          )
        ORDER BY chapter_order
        LIMIT 1
        """,
        collection,
        book_number,
        current_id,
        title_en,
        title_ar,
    )


async def _next_chapter_order(
    conn: asyncpg.Connection,
    *,
    collection: str,
    book_number: str,
) -> int:
    value = await conn.fetchval(
        f"""
        SELECT coalesce(max(chapter_order), 0) + 1
        FROM {SCHEMA}.hadith_chapters
        WHERE collection_name = $1
          AND book_number = $2
        """,
        collection,
        book_number,
    )
    return int(value or 1)


async def _create_chapter(
    conn: asyncpg.Connection,
    *,
    collection: str,
    book_number: str,
    chapter_order: int,
    title_en: str,
    title_ar: str,
    source_api: str,
) -> int:
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
            VALUES ($1, $2, $3, $4, $5, $6, 0, $7, NOW(), NOW())
            RETURNING id
            """,
            collection,
            book_number,
            chapter_order,
            f"EXT-{chapter_order}",
            title_en or None,
            title_ar or None,
            source_api,
        )
    )


async def _split_synthetic_chapter(
    conn: asyncpg.Connection,
    *,
    row: asyncpg.Record,
    source_rows: list[tuple[str, HfChapter | None]],
    stats: dict[str, int],
    dry_run: bool,
) -> bool:
    groups: dict[tuple[str, str], list[str]] = {}
    missing: list[str] = []
    for hadith_number, source_row in source_rows:
        if source_row is None or (not source_row.title_en and not source_row.title_ar):
            missing.append(hadith_number)
            continue
        groups.setdefault((source_row.title_en, source_row.title_ar), []).append(hadith_number)

    if len(groups) <= 1:
        return False

    stats["split_synthetic"] += 1
    stats["moved_hadiths"] += sum(len(numbers) for numbers in groups.values())
    if missing:
        stats["split_leftovers"] += 1

    if dry_run:
        stats["created_chapters"] += len(groups)
        return True

    next_order = await _next_chapter_order(
        conn,
        collection=row["collection_name"],
        book_number=row["book_number"],
    )
    for (title_en, title_ar), hadith_numbers in groups.items():
        target_id = await _matching_chapter_id(
            conn,
            collection=row["collection_name"],
            book_number=row["book_number"],
            current_id=row["id"],
            title_en=title_en,
            title_ar=title_ar,
        )
        if target_id is None:
            target_id = await _create_chapter(
                conn,
                collection=row["collection_name"],
                book_number=row["book_number"],
                chapter_order=next_order,
                title_en=title_en,
                title_ar=title_ar,
                source_api="huggingface-meeAtif",
            )
            next_order += 1
            stats["created_chapters"] += 1

        await conn.execute(
            f"""
            UPDATE {SCHEMA}.hadith_items
            SET chapter_ref_id = $1, updated_at = NOW()
            WHERE collection_name = $2
              AND book_number = $3
              AND chapter_ref_id = $4
              AND hadith_number = ANY($5::text[])
            """,
            target_id,
            row["collection_name"],
            row["book_number"],
            row["id"],
            hadith_numbers,
        )

    if not missing:
        await conn.execute(f"DELETE FROM {SCHEMA}.hadith_chapters WHERE id = $1", row["id"])
        stats["deleted_synthetic"] += 1

    return True


async def repair(args: argparse.Namespace) -> dict[str, int]:
    collections = set(HF_FILES)
    if args.only_collection:
        collections = {args.only_collection}

    hf_metadata = load_hf_metadata(collections)
    ahmedbaset_metadata = load_ahmedbaset_metadata(collections)
    ahmedbaset_book_titles = load_ahmedbaset_book_titles(collections)
    stats = {
        "placeholder_chapters": 0,
        "merged": 0,
        "retitled": 0,
        "filled_ar": 0,
        "filled_from_book_title": 0,
        "skipped_missing_hf": 0,
        "skipped_mixed_hf": 0,
        "split_synthetic": 0,
        "created_chapters": 0,
        "moved_hadiths": 0,
        "split_leftovers": 0,
        "deleted_synthetic": 0,
    }

    conn = await asyncpg.connect(args.dsn)
    try:
        rows = await _placeholder_chapters(conn, collections)
        stats["placeholder_chapters"] = len(rows)
        async with conn.transaction():
            for row in rows:
                collection = row["collection_name"]
                hadith_numbers = [str(value) for value in row["hadith_numbers"] or []]
                source_rows: list[tuple[str, HfChapter | None]] = []
                for hadith_number in hadith_numbers:
                    hf_row = hf_metadata.get((collection, hadith_number))
                    ab_row = ahmedbaset_metadata.get((collection, hadith_number))
                    title_en = (
                        (hf_row.title_en if hf_row and not _is_placeholder_en(hf_row.title_en) else "")
                        or (ab_row.title_en if ab_row and not _is_placeholder_en(ab_row.title_en) else "")
                    )
                    title_ar = (
                        (hf_row.title_ar if hf_row and not _is_placeholder_ar(hf_row.title_ar) else "")
                        or (ab_row.title_ar if ab_row and not _is_placeholder_ar(ab_row.title_ar) else "")
                    )
                    if not title_en and not title_ar:
                        source_rows.append((hadith_number, None))
                    else:
                        source_rows.append((hadith_number, HfChapter("", title_en, title_ar)))

                is_synthetic = row["source_api"] == "synthetic-catchall"
                if is_synthetic and await _split_synthetic_chapter(
                    conn,
                    row=row,
                    source_rows=source_rows,
                    stats=stats,
                    dry_run=args.dry_run,
                ):
                    continue

                title_rows = [item for _, item in source_rows]
                used_book_title = False
                if any(item is None for item in title_rows):
                    book_title_en = _clean(row["book_title"])
                    ab_book_title = ahmedbaset_book_titles.get(
                        (collection, str(row["book_number"]))
                    )
                    book_title_ar = ab_book_title[1] if ab_book_title else ""
                    if (
                        (_is_placeholder_en(row["title_en"]) and book_title_en)
                        or (_is_placeholder_ar(row["title_ar"]) and book_title_ar)
                    ):
                        source_rows = [
                            (hadith_number, HfChapter("", book_title_en, book_title_ar))
                            for hadith_number in hadith_numbers
                        ]
                        title_rows = [item for _, item in source_rows]
                        used_book_title = True
                    else:
                        stats["skipped_missing_hf"] += 1
                        continue

                unique_en = {item.title_en for item in title_rows if item and item.title_en}
                unique_ar = {item.title_ar for item in title_rows if item and item.title_ar}
                ab_book_title = ahmedbaset_book_titles.get(
                    (collection, str(row["book_number"]))
                )
                if _is_placeholder_en(row["title_en"]) and not unique_en:
                    book_title_en = _clean(row["book_title"])
                    if book_title_en:
                        unique_en = {book_title_en}
                        used_book_title = True
                if _is_placeholder_ar(row["title_ar"]) and not unique_ar and ab_book_title:
                    unique_ar = {ab_book_title[1]}
                    used_book_title = True
                if len(unique_en) > 1 or len(unique_ar) > 1:
                    stats["skipped_mixed_hf"] += 1
                    continue

                source_title_en = next(iter(unique_en), "")
                source_title_ar = next(iter(unique_ar), "")
                desired_title_en = source_title_en if _is_placeholder_en(row["title_en"]) else _clean(row["title_en"])
                desired_title_ar = source_title_ar if _is_placeholder_ar(row["title_ar"]) else _clean(row["title_ar"])
                if not desired_title_en and not desired_title_ar:
                    stats["skipped_missing_hf"] += 1
                    continue

                target_id = None
                if _is_placeholder_en(row["title_en"]):
                    target_id = await _matching_chapter_id(
                        conn,
                        collection=collection,
                        book_number=row["book_number"],
                        current_id=row["id"],
                        title_en=desired_title_en,
                        title_ar=desired_title_ar,
                    )

                if target_id is not None:
                    stats["merged"] += 1
                    if not args.dry_run:
                        await conn.execute(
                            f"""
                            UPDATE {SCHEMA}.hadith_items
                            SET chapter_ref_id = $1, updated_at = NOW()
                            WHERE chapter_ref_id = $2
                            """,
                            target_id,
                            row["id"],
                        )
                        await conn.execute(
                            f"DELETE FROM {SCHEMA}.hadith_chapters WHERE id = $1",
                            row["id"],
                        )
                else:
                    if _is_placeholder_en(row["title_en"]):
                        stats["retitled"] += 1
                    if used_book_title:
                        stats["filled_from_book_title"] += 1
                    elif _is_placeholder_ar(row["title_ar"]):
                        stats["filled_ar"] += 1
                    if not args.dry_run:
                        await conn.execute(
                            f"""
                            UPDATE {SCHEMA}.hadith_chapters
                            SET title_en = $1,
                                title_ar = $2,
                                source_api = 'huggingface-meeAtif',
                                updated_at = NOW()
                            WHERE id = $3
                            """,
                            desired_title_en or None,
                            desired_title_ar or None,
                            row["id"],
                        )

            if not args.dry_run:
                await conn.execute(
                    f"""
                    UPDATE {SCHEMA}.hadith_chapters hc
                    SET hadith_count = counts.count
                    FROM (
                        SELECT chapter_ref_id, COUNT(*)::int AS count
                        FROM {SCHEMA}.hadith_items
                        WHERE chapter_ref_id IS NOT NULL
                        GROUP BY chapter_ref_id
                    ) counts
                    WHERE counts.chapter_ref_id = hc.id
                    """
                )
                await conn.execute(
                    f"""
                    UPDATE {SCHEMA}.hadith_chapters hc
                    SET hadith_count = 0
                    WHERE NOT EXISTS (
                        SELECT 1
                        FROM {SCHEMA}.hadith_items hi
                        WHERE hi.chapter_ref_id = hc.id
                    )
                    """
                )
                await conn.execute(
                    f"""
                    DELETE FROM {SCHEMA}.hadith_chapters hc
                    WHERE hc.source_api = 'synthetic-catchall'
                      AND NOT EXISTS (
                        SELECT 1
                        FROM {SCHEMA}.hadith_items hi
                        WHERE hi.chapter_ref_id = hc.id
                      )
                    """
                )
    finally:
        await conn.close()

    return stats


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--dsn", required=True)
    parser.add_argument("--only-collection", choices=sorted(HF_FILES))
    parser.add_argument("--dry-run", action="store_true")
    args = parser.parse_args()
    stats = asyncio.run(repair(args))
    for key, value in stats.items():
        print(f"{key}: {value}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
