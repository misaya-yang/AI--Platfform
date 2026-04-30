#!/usr/bin/env python3
"""Backfill missing Hadith localizations from source metadata.

This script only inserts a missing Arabic/English localization when a source
row can be matched deterministically. It does not generate translations or use
display fallbacks.
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
AHMEDBASET_BASE_URL = "https://raw.githubusercontent.com/AhmedBaset/hadith-json/main/db/by_book/the_9_books"

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
class MissingLocalization:
    id: int
    collection_name: str
    book_number: str
    hadith_number: str
    missing_language: str
    chapter_title_en: str
    chapter_title_ar: str
    arabic_text: str
    english_text: str


def _clean(value: Any) -> str:
    text = str(value or "")
    for char in BIDI_NOISE_CHARS:
        text = text.replace(char, "")
    return re.sub(r"\s+", " ", text).strip()


def _normalize_match(value: Any) -> str:
    return _clean(value).casefold()


def _english_text(value: Any) -> str:
    if isinstance(value, dict):
        return _clean(" ".join(str(part or "") for part in (value.get("narrator"), value.get("text"))))
    return _clean(value)


def _fetch_json(url: str) -> dict[str, Any]:
    with urllib.request.urlopen(url, timeout=180) as response:
        payload = json.load(response)
    if not isinstance(payload, dict):
        raise TypeError(f"Expected JSON object from {url}")
    return payload


def _load_fawaz_edition(collection: str, language: str) -> dict[str, str]:
    prefix = "ara" if language == "ar" else "eng"
    url = f"{FAWAZ_BASE_URL}/{prefix}-{urllib.parse.quote(collection)}.min.json"
    payload = _fetch_json(url)
    rows: dict[str, str] = {}
    for hadith in payload.get("hadiths", []):
        number = _clean(hadith.get("hadithnumber"))
        text = _clean(hadith.get("text"))
        if number and text:
            rows[number] = text
    return rows


def _load_ahmedbaset_rows(collection: str) -> list[dict[str, str]]:
    url = f"{AHMEDBASET_BASE_URL}/{urllib.parse.quote(collection)}.json"
    payload = _fetch_json(url)
    rows = []
    for hadith in payload.get("hadiths", []):
        arabic = _clean(hadith.get("arabic"))
        english = _english_text(hadith.get("english"))
        if arabic or english:
            rows.append({"arabic": arabic, "english": english})
    return rows


def _find_by_opposite_text(
    rows: list[dict[str, str]],
    *,
    opposite_language: str,
    opposite_text: str,
    target_language: str,
) -> str:
    normalized = _normalize_match(opposite_text)
    if not normalized:
        return ""

    matches = []
    for row in rows:
        candidate = _normalize_match(row[opposite_language])
        if candidate and (candidate == normalized or candidate in normalized or normalized in candidate):
            target = _clean(row[target_language])
            if target:
                matches.append(target)

    unique = sorted(set(matches))
    return unique[0] if len(unique) == 1 else ""


async def _missing_localizations(conn: asyncpg.Connection) -> list[MissingLocalization]:
    rows = await conn.fetch(
        f"""
        SELECT
            hi.id,
            hi.collection_name,
            hi.book_number,
            hi.hadith_number,
            lang.language AS missing_language,
            coalesce(hc.title_en, '') AS chapter_title_en,
            coalesce(hc.title_ar, '') AS chapter_title_ar,
            coalesce(ar.body_text, '') AS arabic_text,
            coalesce(en.body_text, '') AS english_text
        FROM {SCHEMA}.hadith_items hi
        JOIN {SCHEMA}.hadith_chapters hc ON hc.id = hi.chapter_ref_id
        LEFT JOIN {SCHEMA}.hadith_localizations ar
            ON ar.hadith_item_id = hi.id AND ar.language = 'ar'
        LEFT JOIN {SCHEMA}.hadith_localizations en
            ON en.hadith_item_id = hi.id AND en.language = 'en'
        CROSS JOIN (VALUES ('ar'), ('en')) AS lang(language)
        WHERE NOT EXISTS (
            SELECT 1
            FROM {SCHEMA}.hadith_localizations hl
            WHERE hl.hadith_item_id = hi.id
              AND hl.language = lang.language
              AND hl.body_text IS NOT NULL
              AND length(trim(hl.body_text)) > 0
        )
        ORDER BY hi.collection_name, hi.book_number, hi.hadith_number, lang.language
        """
    )
    return [MissingLocalization(**dict(row)) for row in rows]


async def repair(args: argparse.Namespace) -> dict[str, int]:
    conn = await asyncpg.connect(args.dsn)
    stats = {
        "missing_before": 0,
        "filled_from_fawaz": 0,
        "filled_from_ahmedbaset": 0,
        "source_gaps": 0,
    }
    fawaz_cache: dict[tuple[str, str], dict[str, str]] = {}
    ahmedbaset_cache: dict[str, list[dict[str, str]]] = {}

    try:
        missing_rows = await _missing_localizations(conn)
        stats["missing_before"] = len(missing_rows)
        async with conn.transaction():
            for row in missing_rows:
                text = ""
                key = (row.collection_name, row.missing_language)
                try:
                    fawaz_rows = fawaz_cache.setdefault(
                        key,
                        _load_fawaz_edition(row.collection_name, row.missing_language),
                    )
                    text = fawaz_rows.get(row.hadith_number, "")
                except Exception:
                    text = ""

                source = ""
                if text:
                    source = "fawaz"
                    stats["filled_from_fawaz"] += 1
                else:
                    ahmedbaset_rows = ahmedbaset_cache.setdefault(
                        row.collection_name,
                        _load_ahmedbaset_rows(row.collection_name),
                    )
                    if row.missing_language == "ar":
                        text = _find_by_opposite_text(
                            ahmedbaset_rows,
                            opposite_language="english",
                            opposite_text=row.english_text,
                            target_language="arabic",
                        )
                    else:
                        text = _find_by_opposite_text(
                            ahmedbaset_rows,
                            opposite_language="arabic",
                            opposite_text=row.arabic_text,
                            target_language="english",
                        )
                    if text:
                        source = "ahmedbaset"
                        stats["filled_from_ahmedbaset"] += 1

                if not text:
                    stats["source_gaps"] += 1
                    print(
                        "SOURCE_GAP",
                        row.collection_name,
                        row.book_number,
                        row.hadith_number,
                        row.missing_language,
                    )
                    continue

                if args.dry_run:
                    print(
                        "WOULD_FILL",
                        source,
                        row.collection_name,
                        row.book_number,
                        row.hadith_number,
                        row.missing_language,
                    )
                    continue

                chapter_title = row.chapter_title_ar if row.missing_language == "ar" else row.chapter_title_en
                await conn.execute(
                    f"""
                    INSERT INTO {SCHEMA}.hadith_localizations (
                        hadith_item_id,
                        language,
                        chapter_title,
                        body_text,
                        updated_at
                    )
                    VALUES ($1, $2, $3, $4, NOW())
                    ON CONFLICT (hadith_item_id, language)
                    DO UPDATE SET
                        chapter_title = EXCLUDED.chapter_title,
                        body_text = EXCLUDED.body_text,
                        updated_at = NOW()
                    WHERE {SCHEMA}.hadith_localizations.body_text IS NULL
                       OR length(trim({SCHEMA}.hadith_localizations.body_text)) = 0
                    """,
                    row.id,
                    row.missing_language,
                    chapter_title or None,
                    text,
                )
    finally:
        await conn.close()

    return stats


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--dsn", required=True)
    parser.add_argument("--dry-run", action="store_true")
    args = parser.parse_args()
    stats = asyncio.run(repair(args))
    for key, value in stats.items():
        print(f"{key}: {value}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
