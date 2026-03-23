#!/usr/bin/env python3
"""
Sync Arabic Hadith text from fawazahmed0 CDN into hadith_localizations.

Matches Arabic hadiths to existing English entries by hadithnumber,
then upserts into hadith_localizations with language='ar'.

Usage:
    python scripts/sync_hadith_arabic.py
    python scripts/sync_hadith_arabic.py --dsn postgresql://postgres:111111@127.0.0.1:5432/gateway
"""

import argparse
import asyncio
import html
import json
import logging
import os
import re
import time

import asyncpg
import httpx

logging.basicConfig(level=logging.INFO, format="%(asctime)s [%(levelname)s] %(message)s", datefmt="%H:%M:%S")
log = logging.getLogger("sync-ar")

SCHEMA = "islamic_content"
HADITH_CDN = "https://cdn.jsdelivr.net/gh/fawazahmed0/hadith-api@1"

# Arabic edition keys (with diacritics for display)
ARABIC_EDITIONS = {
    "bukhari": "ara-bukhari",
    "muslim": "ara-muslim",
    "abudawud": "ara-abudawud",
    "tirmidhi": "ara-tirmidhi",
    "nasai": "ara-nasai",
    "ibnmajah": "ara-ibnmajah",
    "nawawi": "ara-nawawi",
    "qudsi": "ara-qudsi",
    "malik": "ara-malik",
    "dehlawi": "ara-dehlawi",
}


def _clean_html(text):
    if not text:
        return ""
    text = re.sub(r"<sup[^>]*>.*?</sup>", "", text, flags=re.DOTALL)
    text = re.sub(r"<[^>]+>", "", text)
    text = html.unescape(text)
    return text.strip()


async def _get_json(client, url, retries=3):
    for attempt in range(retries):
        try:
            r = await client.get(url, timeout=30)
            if r.status_code == 429:
                await asyncio.sleep(int(r.headers.get("retry-after", 5)))
                continue
            r.raise_for_status()
            return r.json()
        except Exception as e:
            if attempt < retries - 1:
                await asyncio.sleep(2 ** attempt)
            else:
                log.error("Failed %s: %s", url, e)
                return None


async def main():
    parser = argparse.ArgumentParser(description="Sync Arabic Hadith from CDN")
    parser.add_argument("--dsn", default=os.environ.get(
        "DATABASE_DSN",
        os.environ.get("GATEWAY_DATABASE__DSN", "postgresql://postgres:111111@127.0.0.1:5432/gateway")
    ))
    args = parser.parse_args()

    pool = await asyncpg.create_pool(args.dsn, min_size=2, max_size=5)
    total_synced = 0

    async with httpx.AsyncClient(follow_redirects=True, limits=httpx.Limits(max_connections=10)) as client:
        for coll_name, ara_key in ARABIC_EDITIONS.items():
            log.info("Fetching Arabic: %s (%s)...", coll_name, ara_key)

            url = f"{HADITH_CDN}/editions/{ara_key}.json"
            data = await _get_json(client, url)
            if not data:
                log.warning("  Skipped %s (fetch failed)", coll_name)
                continue

            hadiths = data.get("hadiths", [])
            log.info("  %s: %d Arabic hadiths", coll_name, len(hadiths))

            synced = 0
            async with pool.acquire() as conn:
                for h in hadiths:
                    hadith_num = str(h.get("hadithnumber", ""))
                    if not hadith_num:
                        continue

                    ar_text = _clean_html(h.get("text", h.get("body", "")))
                    if not ar_text:
                        continue

                    # Find existing hadith_item_id by collection + hadith_number
                    row = await conn.fetchrow(f"""
                        SELECT id FROM {SCHEMA}.hadith_items
                        WHERE collection_name = $1 AND hadith_number = $2
                    """, coll_name, hadith_num)

                    if not row:
                        continue  # No matching English entry

                    item_id = row["id"]

                    # Upsert Arabic localization
                    await conn.execute(f"""
                        INSERT INTO {SCHEMA}.hadith_localizations
                            (hadith_item_id, language, chapter_title, body_text, updated_at)
                        VALUES ($1, 'ar', '', $2, NOW())
                        ON CONFLICT (hadith_item_id, language) DO UPDATE SET
                            body_text = EXCLUDED.body_text,
                            updated_at = NOW()
                    """, item_id, ar_text)
                    synced += 1

            total_synced += synced
            log.info("  %s: %d Arabic entries synced", coll_name, synced)

    # Summary
    async with pool.acquire() as conn:
        counts = await conn.fetch(f"""
            SELECT language, COUNT(*) as cnt
            FROM {SCHEMA}.hadith_localizations
            GROUP BY language ORDER BY language
        """)
        log.info("Final localization counts:")
        for r in counts:
            log.info("  %s: %d", r["language"], r["cnt"])

    await pool.close()
    log.info("Done. Total Arabic synced: %d", total_synced)


if __name__ == "__main__":
    asyncio.run(main())
