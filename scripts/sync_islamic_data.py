#!/usr/bin/env python3
"""
Sync Islamic Content — Populate Quran & Hadith tables from free public APIs.

Data sources (no API key required):
  - Quran: api.quran.com v4 (public, free)
  - Hadith: cdn.jsdelivr.net/gh/fawazahmed0/hadith-api (public, free CDN)

Usage:
  python scripts/sync_islamic_data.py                           # use DATABASE_DSN or default
  python scripts/sync_islamic_data.py --dsn postgresql://...    # explicit DSN
  python scripts/sync_islamic_data.py --quran-only              # sync Quran only
  python scripts/sync_islamic_data.py --hadith-only             # sync Hadith only
"""

import argparse
import asyncio
import html
import json
import logging
import os
import re
import sys
import time
from dataclasses import dataclass, field

import asyncpg
import httpx

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s [%(levelname)s] %(message)s",
    datefmt="%H:%M:%S",
)
log = logging.getLogger("sync")

SCHEMA = "islamic_content"
QURAN_API = "https://api.quran.com/api/v4"
HADITH_CDN = "https://cdn.jsdelivr.net/gh/fawazahmed0/hadith-api@1"
DEFAULT_TRANSLATION_ID = 20  # Sahih International
DEFAULT_RECITATION_ID = 7    # Mishary Alafasy
SOURCE_API_QURAN = "quran.com"
SOURCE_API_HADITH = "cdn-hadith"

# Hadith collections to sync (CDN edition keys → display names)
HADITH_COLLECTIONS = {
    "bukhari": ("eng-bukhari", "Sahih al-Bukhari", True, True),
    "muslim": ("eng-muslim", "Sahih Muslim", True, True),
    "abudawud": ("eng-abudawud", "Sunan Abu Dawud", True, True),
    "tirmidhi": ("eng-tirmidhi", "Jami at-Tirmidhi", True, True),
    "nasai": ("eng-nasai", "Sunan an-Nasa'i", True, True),
    "ibnmajah": ("eng-ibnmajah", "Sunan Ibn Majah", True, True),
    "nawawi": ("eng-nawawi", "Forty Hadith Nawawi", False, False),
}

# Arabic editions for bilingual sync
HADITH_ARABIC_EDITIONS = {
    "bukhari": "ara-bukhari",
    "muslim": "ara-muslim",
    "abudawud": "ara-abudawud",
    "tirmidhi": "ara-tirmidhi",
    "nasai": "ara-nasai",
    "ibnmajah": "ara-ibnmajah",
    "nawawi": "ara-nawawi",
}


def _clean_html(text: str | None) -> str:
    if not text:
        return ""
    text = re.sub(r"<sup[^>]*>.*?</sup>", "", text, flags=re.DOTALL)
    text = re.sub(r"<[^>]+>", "", text)
    text = html.unescape(text)
    return text.strip()


# ─── HTTP helpers ────────────────────────────────────────────────────────────

async def _get_json(client: httpx.AsyncClient, url: str, params: dict | None = None,
                    retries: int = 3) -> dict | list | None:
    for attempt in range(retries):
        try:
            r = await client.get(url, params=params, timeout=30)
            if r.status_code == 429:
                wait = int(r.headers.get("retry-after", 5))
                log.warning("Rate-limited, waiting %ds...", wait)
                await asyncio.sleep(wait)
                continue
            r.raise_for_status()
            return r.json()
        except Exception as e:
            if attempt < retries - 1:
                await asyncio.sleep(2 ** attempt)
            else:
                log.error("Failed %s: %s", url, e)
                return None


# ─── Quran Sync ──────────────────────────────────────────────────────────────

async def sync_quran(pool: asyncpg.Pool, client: httpx.AsyncClient):
    log.info("═══ QURAN SYNC START ═══")
    t0 = time.time()

    # 1. Chapters
    log.info("Fetching chapters...")
    data = await _get_json(client, f"{QURAN_API}/chapters")
    chapters = data.get("chapters", []) if data else []
    log.info("Got %d chapters", len(chapters))
    async with pool.acquire() as conn:
        for ch in chapters:
            await conn.execute(f"""
                INSERT INTO {SCHEMA}.quran_chapters
                    (chapter_id, name_simple, name_complex, name_arabic,
                     translated_name, revelation_place, verses_count,
                     source_api, updated_at)
                VALUES ($1,$2,$3,$4,$5,$6,$7,$8,NOW())
                ON CONFLICT (chapter_id) DO UPDATE SET
                    name_simple=EXCLUDED.name_simple, name_complex=EXCLUDED.name_complex,
                    name_arabic=EXCLUDED.name_arabic, translated_name=EXCLUDED.translated_name,
                    revelation_place=EXCLUDED.revelation_place, verses_count=EXCLUDED.verses_count,
                    source_api=EXCLUDED.source_api, updated_at=NOW()
            """, ch["id"], ch.get("name_simple"), ch.get("name_complex"),
                ch.get("name_arabic"), ch.get("translated_name", {}).get("name", ""),
                ch.get("revelation_place"), ch.get("verses_count", 0),
                SOURCE_API_QURAN)

    # 2. Translations metadata
    log.info("Fetching translation resources...")
    tdata = await _get_json(client, f"{QURAN_API}/resources/translations", {"language": "en"})
    translations = tdata.get("translations", []) if tdata else []
    async with pool.acquire() as conn:
        for t in translations:
            await conn.execute(f"""
                INSERT INTO {SCHEMA}.quran_translations
                    (translation_id, name, author_name, slug, language_name,
                     source_api, raw_payload, updated_at)
                VALUES ($1,$2,$3,$4,$5,$6,$7,NOW())
                ON CONFLICT (translation_id) DO UPDATE SET
                    name=EXCLUDED.name, author_name=EXCLUDED.author_name,
                    slug=EXCLUDED.slug, language_name=EXCLUDED.language_name,
                    raw_payload=EXCLUDED.raw_payload, updated_at=NOW()
            """, t["id"], t.get("name"), t.get("author_name"),
                t.get("slug", ""), t.get("language_name", "english"),
                SOURCE_API_QURAN, json.dumps(t))
    log.info("Saved %d translation resources", len(translations))

    # 3. Verses (chapter by chapter)
    total_ayahs = 0
    total_words = 0
    sem = asyncio.Semaphore(3)  # max 3 concurrent chapter fetches

    async def sync_chapter_verses(ch_id: int, ch_verses: int):
        nonlocal total_ayahs, total_words
        async with sem:
            page = 1
            while True:
                params = {
                    "language": "en",
                    "words": "true",
                    "translations": str(DEFAULT_TRANSLATION_ID),
                    "fields": "text_uthmani,chapter_id,verse_number,juz_number,hizb_number,rub_el_hizb_number,page_number",
                    "word_fields": "text_uthmani,text_uthmani_simple,text_imlaei_simple",
                    "per_page": "50",
                    "page": str(page),
                }
                data = await _get_json(client, f"{QURAN_API}/verses/by_chapter/{ch_id}", params)
                if not data:
                    break
                verses = data.get("verses", [])
                if not verses:
                    break

                async with pool.acquire() as conn:
                    for v in verses:
                        vk = v.get("verse_key", f"{ch_id}:{v.get('verse_number',0)}")
                        arabic = v.get("text_uthmani", "")
                        transliteration = ""
                        # Extract translation text
                        trans_text = ""
                        for tr in v.get("translations", []):
                            if tr.get("resource_id") == DEFAULT_TRANSLATION_ID:
                                trans_text = _clean_html(tr.get("text", ""))
                                break

                        # Upsert ayah
                        await conn.execute(f"""
                            INSERT INTO {SCHEMA}.quran_ayahs
                                (verse_key, chapter_id, ayah_number, juz_number,
                                 hizb_number, rub_number, page_number,
                                 arabic_text, transliteration_text,
                                 translation_id, translation_text,
                                 recitation_id, audio_url,
                                 source_api, updated_at)
                            VALUES ($1,$2,$3,$4,$5,$6,$7,$8,$9,$10,$11,$12,$13,$14,NOW())
                            ON CONFLICT (verse_key) DO UPDATE SET
                                chapter_id=EXCLUDED.chapter_id, ayah_number=EXCLUDED.ayah_number,
                                juz_number=EXCLUDED.juz_number, hizb_number=EXCLUDED.hizb_number,
                                rub_number=EXCLUDED.rub_number, page_number=EXCLUDED.page_number,
                                arabic_text=EXCLUDED.arabic_text, transliteration_text=EXCLUDED.transliteration_text,
                                translation_id=EXCLUDED.translation_id, translation_text=EXCLUDED.translation_text,
                                source_api=EXCLUDED.source_api, updated_at=NOW()
                        """, vk, ch_id, v.get("verse_number", 0),
                            v.get("juz_number"), v.get("hizb_number"),
                            v.get("rub_el_hizb_number"), v.get("page_number"),
                            arabic, transliteration,
                            DEFAULT_TRANSLATION_ID, trans_text,
                            DEFAULT_RECITATION_ID, None,
                            SOURCE_API_QURAN)

                        # Upsert translation separately
                        if trans_text:
                            await conn.execute(f"""
                                INSERT INTO {SCHEMA}.quran_ayah_translations
                                    (verse_key, translation_id, translation_text, source_api, updated_at)
                                VALUES ($1,$2,$3,$4,NOW())
                                ON CONFLICT (verse_key, translation_id) DO UPDATE SET
                                    translation_text=EXCLUDED.translation_text, updated_at=NOW()
                            """, vk, DEFAULT_TRANSLATION_ID, trans_text, SOURCE_API_QURAN)

                        # Upsert words
                        words = v.get("words", [])
                        for w in words:
                            await conn.execute(f"""
                                INSERT INTO {SCHEMA}.quran_words
                                    (verse_key, position, arabic, arabic_simple,
                                     transliteration, translation, audio_url,
                                     char_type, updated_at)
                                VALUES ($1,$2,$3,$4,$5,$6,$7,$8,NOW())
                                ON CONFLICT (verse_key, position) DO UPDATE SET
                                    arabic=EXCLUDED.arabic, arabic_simple=EXCLUDED.arabic_simple,
                                    transliteration=EXCLUDED.transliteration, translation=EXCLUDED.translation,
                                    audio_url=EXCLUDED.audio_url, char_type=EXCLUDED.char_type,
                                    updated_at=NOW()
                            """, vk, w.get("position", 0),
                                w.get("text_uthmani", w.get("text", "")),
                                w.get("text_uthmani_simple", w.get("text_imlaei_simple", "")),
                                w.get("transliteration", {}).get("text", "") if isinstance(w.get("transliteration"), dict) else (w.get("transliteration") or ""),
                                w.get("translation", {}).get("text", "") if isinstance(w.get("translation"), dict) else (w.get("translation") or ""),
                                w.get("audio_url"),
                                w.get("char_type_name", w.get("char_type", "")))
                            total_words += 1

                        total_ayahs += 1

                pagination = data.get("pagination", {})
                total_pages = pagination.get("total_pages", 1)
                if page >= total_pages:
                    break
                page += 1
            log.info("  Chapter %d: done", ch_id)

    log.info("Syncing verses for %d chapters...", len(chapters))
    tasks = [sync_chapter_verses(ch["id"], ch.get("verses_count", 0)) for ch in chapters]
    await asyncio.gather(*tasks)
    log.info("Quran: %d ayahs, %d words synced", total_ayahs, total_words)

    # 4. Triplet ranges (group ayahs into blocks of 3)
    log.info("Generating triplet ranges...")
    async with pool.acquire() as conn:
        for ch in chapters:
            ch_id = ch["id"]
            vc = ch.get("verses_count", 0)
            for start in range(1, vc + 1, 3):
                end = min(start + 2, vc)
                block_id = f"quran:{ch_id}:{start}-{end}"
                verse_keys = [f"{ch_id}:{a}" for a in range(start, end + 1)]
                await conn.execute(f"""
                    INSERT INTO {SCHEMA}.quran_triplet_ranges
                        (block_id, chapter_id, start_ayah_number, end_ayah_number,
                         verse_keys_json, updated_at)
                    VALUES ($1,$2,$3,$4,$5::jsonb,NOW())
                    ON CONFLICT (block_id) DO UPDATE SET
                        verse_keys_json=EXCLUDED.verse_keys_json, updated_at=NOW()
                """, block_id, ch_id, start, end, json.dumps(verse_keys))

    elapsed = time.time() - t0
    log.info("═══ QURAN SYNC DONE (%d ayahs, %d words) in %.1fs ═══", total_ayahs, total_words, elapsed)


# ─── Hadith Sync ─────────────────────────────────────────────────────────────

async def sync_hadith(pool: asyncpg.Pool, client: httpx.AsyncClient):
    log.info("═══ HADITH SYNC START ═══")
    t0 = time.time()
    total_items = 0

    for coll_name, (cdn_key, title, has_books, has_chapters) in HADITH_COLLECTIONS.items():
        log.info("Syncing collection: %s (%s)...", coll_name, title)

        # Fetch full collection from CDN
        url = f"{HADITH_CDN}/editions/{cdn_key}.json"
        data = await _get_json(client, url, retries=3)
        if not data:
            log.error("  Failed to fetch %s", url)
            continue

        metadata = data.get("metadata", {})
        hadiths_raw = data.get("hadiths", [])
        log.info("  %s: %d hadiths from CDN", coll_name, len(hadiths_raw))

        # Discover books from the hadiths
        books: dict[str, dict] = {}
        for h in hadiths_raw:
            ref = h.get("reference", {})
            book_num = str(ref.get("book", h.get("bookNumber", "0")))
            if book_num not in books:
                books[book_num] = {
                    "title": "",
                    "start": 999999,
                    "end": 0,
                    "count": 0,
                }
            hadith_num = ref.get("hadith", h.get("hadithnumber", 0))
            try:
                hn = int(hadith_num) if hadith_num else 0
            except (ValueError, TypeError):
                hn = 0
            books[book_num]["start"] = min(books[book_num]["start"], hn) if hn > 0 else books[book_num]["start"]
            books[book_num]["end"] = max(books[book_num]["end"], hn)
            books[book_num]["count"] += 1

        # Upsert collection
        async with pool.acquire() as conn:
            await conn.execute(f"""
                INSERT INTO {SCHEMA}.hadith_collections
                    (name, title, short_intro, has_books, has_chapters,
                     total_books, total_hadith, source_api, updated_at)
                VALUES ($1,$2,$3,$4,$5,$6,$7,$8,NOW())
                ON CONFLICT (name) DO UPDATE SET
                    title=EXCLUDED.title, short_intro=EXCLUDED.short_intro,
                    total_books=EXCLUDED.total_books, total_hadith=EXCLUDED.total_hadith,
                    source_api=EXCLUDED.source_api, updated_at=NOW()
            """, coll_name, title,
                metadata.get("shortIntro", metadata.get("short_intro", "")),
                has_books, has_chapters,
                len(books), len(hadiths_raw), SOURCE_API_HADITH)

            # Upsert books
            for book_num, binfo in books.items():
                await conn.execute(f"""
                    INSERT INTO {SCHEMA}.hadith_books
                        (collection_name, book_number, title,
                         hadith_start_number, hadith_end_number,
                         number_of_hadith, updated_at)
                    VALUES ($1,$2,$3,$4,$5,$6,NOW())
                    ON CONFLICT (collection_name, book_number) DO UPDATE SET
                        title=EXCLUDED.title,
                        hadith_start_number=EXCLUDED.hadith_start_number,
                        hadith_end_number=EXCLUDED.hadith_end_number,
                        number_of_hadith=EXCLUDED.number_of_hadith,
                        updated_at=NOW()
                """, coll_name, book_num, binfo.get("title", ""),
                    binfo["start"] if binfo["start"] < 999999 else None,
                    binfo["end"] if binfo["end"] > 0 else None,
                    binfo["count"])

            # Upsert hadiths
            for h in hadiths_raw:
                ref = h.get("reference", {})
                book_num = str(ref.get("book", h.get("bookNumber", "0")))
                # Use global hadithnumber (unique per collection), NOT reference.hadith (resets per book)
                hadith_num = str(h.get("hadithnumber", ref.get("hadith", "")))
                if not hadith_num:
                    continue
                chapter_id = str(h.get("chapterId", h.get("chapter_id", "")))
                chapter_title = h.get("chapterTitle", h.get("chapter_title", ""))

                # English text
                en_text = _clean_html(h.get("text", h.get("body", "")))
                # Arabic text (if available in the CDN data)
                ar_text = h.get("arabicText", h.get("arabic_text", ""))

                # Upsert hadith_items
                row = await conn.fetchrow(f"""
                    INSERT INTO {SCHEMA}.hadith_items
                        (collection_name, book_number, chapter_id, hadith_number,
                         chapter_title, source_api, updated_at)
                    VALUES ($1,$2,$3,$4,$5,$6,NOW())
                    ON CONFLICT (collection_name, hadith_number) DO UPDATE SET
                        book_number=EXCLUDED.book_number,
                        chapter_id=EXCLUDED.chapter_id,
                        chapter_title=EXCLUDED.chapter_title,
                        source_api=EXCLUDED.source_api,
                        updated_at=NOW()
                    RETURNING id
                """, coll_name, book_num, chapter_id, hadith_num,
                    chapter_title, SOURCE_API_HADITH)

                if row:
                    item_id = row["id"]
                    # Upsert localizations (English)
                    await conn.execute(f"""
                        INSERT INTO {SCHEMA}.hadith_localizations
                            (hadith_item_id, language, chapter_title, body_text, updated_at)
                        VALUES ($1,$2,$3,$4,NOW())
                        ON CONFLICT (hadith_item_id, language) DO UPDATE SET
                            chapter_title=EXCLUDED.chapter_title,
                            body_text=EXCLUDED.body_text,
                            updated_at=NOW()
                    """, item_id, "en", chapter_title, en_text)

                    # Arabic localization if available
                    if ar_text:
                        await conn.execute(f"""
                            INSERT INTO {SCHEMA}.hadith_localizations
                                (hadith_item_id, language, chapter_title, body_text, updated_at)
                            VALUES ($1,$2,$3,$4,NOW())
                            ON CONFLICT (hadith_item_id, language) DO UPDATE SET
                                body_text=EXCLUDED.body_text, updated_at=NOW()
                        """, item_id, "ar", chapter_title, ar_text)

                    # Grades if available (DELETE + INSERT, no unique constraint)
                    grades = h.get("grades", [])
                    if isinstance(grades, list) and grades:
                        await conn.execute(f"""
                            DELETE FROM {SCHEMA}.hadith_grades WHERE hadith_item_id = $1
                        """, item_id)
                        for g in grades:
                            await conn.execute(f"""
                                INSERT INTO {SCHEMA}.hadith_grades
                                    (hadith_item_id, language, grade, graded_by, raw_payload, updated_at)
                                VALUES ($1,$2,$3,$4,$5::jsonb,NOW())
                            """, item_id, "en",
                                g.get("grade", ""), g.get("graded_by", g.get("name", "")),
                                json.dumps(g))

                total_items += 1

        log.info("  %s: %d hadiths synced", coll_name, len(hadiths_raw))

    # ── Phase 2: Sync Arabic text into hadith_localizations ──
    log.info("Syncing Arabic text for all collections...")
    ar_count = 0
    for coll_name, ar_key in HADITH_ARABIC_EDITIONS.items():
        url = f"{HADITH_CDN}/editions/{ar_key}.json"
        data = await _get_json(client, url, retries=3)
        if not data:
            log.warning("  %s: Arabic edition not available", coll_name)
            continue
        ar_hadiths = data.get("hadiths", [])
        log.info("  %s: %d Arabic hadiths from CDN", coll_name, len(ar_hadiths))

        async with pool.acquire() as conn:
            for h in ar_hadiths:
                hadith_num = str(h.get("hadithnumber", ""))
                if not hadith_num:
                    continue
                ar_text = h.get("text", h.get("body", ""))
                if not ar_text:
                    continue
                # Find the item_id for this hadith
                row = await conn.fetchrow(f"""
                    SELECT id FROM {SCHEMA}.hadith_items
                    WHERE collection_name = $1 AND hadith_number = $2
                """, coll_name, hadith_num)
                if row:
                    await conn.execute(f"""
                        INSERT INTO {SCHEMA}.hadith_localizations
                            (hadith_item_id, language, chapter_title, body_text, updated_at)
                        VALUES ($1, 'ar', '', $2, NOW())
                        ON CONFLICT (hadith_item_id, language) DO UPDATE SET
                            body_text = EXCLUDED.body_text, updated_at = NOW()
                    """, row["id"], ar_text)
                    ar_count += 1

        log.info("  %s: %d Arabic texts synced", coll_name, ar_count)

    elapsed = time.time() - t0
    log.info("═══ HADITH SYNC DONE (%d items + %d Arabic) in %.1fs ═══", total_items, ar_count, elapsed)


# ─── Record sync run ─────────────────────────────────────────────────────────

async def record_sync_run(pool: asyncpg.Pool, source: str, status: str, items: int):
    try:
        async with pool.acquire() as conn:
            await conn.execute(f"""
                INSERT INTO {SCHEMA}.source_sync_runs
                    (source, status, items_synced, completed_at)
                VALUES ($1, $2, $3, NOW())
            """, source, status, items)
    except Exception:
        pass  # table might not have these columns, non-critical


# ─── Main ────────────────────────────────────────────────────────────────────

async def main():
    parser = argparse.ArgumentParser(description="Sync Islamic Content from free public APIs")
    parser.add_argument("--dsn", default=os.environ.get(
        "DATABASE_DSN",
        os.environ.get("GATEWAY_DATABASE__DSN", "postgresql://postgres:111111@127.0.0.1:5432/gateway")
    ), help="PostgreSQL connection string")
    parser.add_argument("--quran-only", action="store_true", help="Sync Quran only")
    parser.add_argument("--hadith-only", action="store_true", help="Sync Hadith only")
    args = parser.parse_args()

    do_quran = not args.hadith_only
    do_hadith = not args.quran_only

    log.info("Connecting to database...")
    pool = await asyncpg.create_pool(args.dsn, min_size=2, max_size=5)
    log.info("Connected. Schema: %s", SCHEMA)

    async with httpx.AsyncClient(
        follow_redirects=True,
        limits=httpx.Limits(max_connections=10, max_keepalive_connections=5),
    ) as client:
        if do_quran:
            try:
                await sync_quran(pool, client)
                await record_sync_run(pool, "quran", "completed", 6236)
            except Exception as e:
                log.exception("Quran sync failed: %s", e)
                await record_sync_run(pool, "quran", "failed", 0)

        if do_hadith:
            try:
                await sync_hadith(pool, client)
                await record_sync_run(pool, "hadith", "completed", 0)
            except Exception as e:
                log.exception("Hadith sync failed: %s", e)
                await record_sync_run(pool, "hadith", "failed", 0)

    await pool.close()

    # Summary
    log.info("═══ ALL SYNC COMPLETE ═══")
    pool2 = await asyncpg.create_pool(args.dsn, min_size=1, max_size=1)
    async with pool2.acquire() as conn:
        rows = await conn.fetch(f"""
            SELECT relname as table_name, n_live_tup as row_count
            FROM pg_stat_user_tables
            WHERE schemaname = '{SCHEMA}'
            ORDER BY relname
        """)
        log.info("Table row counts:")
        for r in rows:
            log.info("  %-30s %d", r["table_name"], r["row_count"])
    await pool2.close()


if __name__ == "__main__":
    asyncio.run(main())
