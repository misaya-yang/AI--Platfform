#!/usr/bin/env python3
"""Fix Quran audio URLs that were incorrectly prefixed with
`https://verses.quran.foundation/` instead of `https://`.

Root cause
----------
`QuranSyncService._normalize_audio_url` used to treat bare-domain URLs
(e.g. ``mirrors.quranicaudio.com/everyayah/...``) as relative paths and
append them to ``https://verses.quran.foundation/``, producing 404 URLs
like::

    https://verses.quran.foundation/mirrors.quranicaudio.com/everyayah/002004.mp3

The correct form is::

    https://mirrors.quranicaudio.com/everyayah/002004.mp3

This script is idempotent — running twice is safe. It handles every table
that stores an audio URL, including the JSONB column on
``quran_triplet_blocks``.

Usage
-----
::

    python -m scripts.fix_audio_urls --dry-run   # report counts
    python -m scripts.fix_audio_urls             # apply the fix
"""
from __future__ import annotations

import argparse
import asyncio
import json
import re
import sys
from pathlib import Path
from typing import Any

# Make the package importable when running as a script from scripts/
_REPO_SRC = Path(__file__).resolve().parents[1] / "src"
if str(_REPO_SRC) not in sys.path:
    sys.path.insert(0, str(_REPO_SRC))

from islamic_content_service.config import Settings  # noqa: E402
from islamic_content_service.db import Database  # noqa: E402

_BAD_PREFIX = "https://verses.quran.foundation/"
# Any path segment that *looks* like a domain (contains a dot and no slash)
_BAD_URL_RE = re.compile(
    r"^https://verses\.quran\.foundation/"
    r"(?P<domain>[A-Za-z0-9-]+(?:\.[A-Za-z0-9-]+)+)"
    r"(?P<rest>/.*)?$"
)


def fix_url(url: str | None) -> str | None:
    """Return the corrected URL, or the input unchanged if it's already fine."""
    if not url:
        return url
    match = _BAD_URL_RE.match(url)
    if not match:
        return url
    return f"https://{match.group('domain')}{match.group('rest') or ''}"


# (table, primary key columns, url column)
SCALAR_TARGETS: list[tuple[str, tuple[str, ...], str]] = [
    ("quran_chapters", ("chapter_id",), "default_audio_url"),
    ("quran_ayahs", ("verse_key",), "audio_url"),
    ("quran_words", ("id",), "audio_url"),
    ("quran_ayah_audio", ("id",), "audio_url"),
    ("quran_chapter_audio_tracks", ("id",), "audio_url"),
]


async def fix_scalar_table(
    db: Database,
    table: str,
    pk_columns: tuple[str, ...],
    url_column: str,
    dry_run: bool,
) -> int:
    pk_select = ", ".join(pk_columns)
    rows = await db.fetch(
        f"SELECT {pk_select}, {url_column} FROM {table} "
        f"WHERE {url_column} LIKE $1",
        f"{_BAD_PREFIX}%",
    )
    to_update: list[tuple[Any, ...]] = []
    for row in rows:
        original = row[url_column]
        fixed = fix_url(original)
        if fixed is None or fixed == original:
            continue
        to_update.append((fixed, *(row[col] for col in pk_columns)))

    if not to_update:
        return 0
    if dry_run:
        return len(to_update)

    placeholders = " AND ".join(
        f"{col} = ${index + 2}" for index, col in enumerate(pk_columns)
    )
    await db.executemany(
        f"UPDATE {table} SET {url_column} = $1 WHERE {placeholders}",
        to_update,
    )
    return len(to_update)


async def fix_triplet_blocks(db: Database, dry_run: bool) -> int:
    rows = await db.fetch(
        "SELECT block_id, audio_urls_json FROM quran_triplet_blocks "
        "WHERE audio_urls_json::text LIKE $1",
        f"%{_BAD_PREFIX}%",
    )
    to_update: list[tuple[str, str]] = []
    for row in rows:
        raw = row["audio_urls_json"]
        if isinstance(raw, str):
            payload = json.loads(raw)
        else:
            payload = raw
        if not isinstance(payload, list):
            continue
        changed = False
        for item in payload:
            if not isinstance(item, dict):
                continue
            original = item.get("url")
            fixed = fix_url(original) if isinstance(original, str) else original
            if fixed != original:
                item["url"] = fixed
                changed = True
        if changed:
            to_update.append((json.dumps(payload, ensure_ascii=False), row["block_id"]))

    if not to_update:
        return 0
    if dry_run:
        return len(to_update)

    await db.executemany(
        "UPDATE quran_triplet_blocks SET audio_urls_json = $1::jsonb "
        "WHERE block_id = $2",
        to_update,
    )
    return len(to_update)


async def run(dry_run: bool) -> int:
    settings = Settings()
    db = Database(settings.database)
    await db.connect()
    total = 0
    try:
        for table, pk, col in SCALAR_TARGETS:
            count = await fix_scalar_table(db, table, pk, col, dry_run)
            verb = "would fix" if dry_run else "fixed"
            print(f"  {table}.{col}: {verb} {count} row(s)")
            total += count

        triplet_count = await fix_triplet_blocks(db, dry_run)
        verb = "would fix" if dry_run else "fixed"
        print(f"  quran_triplet_blocks.audio_urls_json: {verb} {triplet_count} row(s)")
        total += triplet_count
    finally:
        await db.close()

    print(f"\nTotal rows {'to fix' if dry_run else 'fixed'}: {total}")
    return total


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument(
        "--dry-run",
        action="store_true",
        help="Report how many rows would be fixed without modifying the database.",
    )
    args = parser.parse_args()
    asyncio.run(run(args.dry_run))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
