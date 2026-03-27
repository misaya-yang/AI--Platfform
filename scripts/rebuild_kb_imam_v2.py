#!/usr/bin/env python3
"""Rebuild Islamic KB v2 — Complete re-embedding into new Qdrant collection.

Creates `kb_imam_v2_1024` with ALL Islamic data:
  Phase 1: Quran 6,236 + Hadith ~34,574 + Dua 72  (from PostgreSQL)
  Phase 2: Tafsir/Fiqh/Aqeedah/Seerah PDFs         (from filesystem)

Embedding: gemini-embedding-2-preview @ 1024 dim
Dependencies: asyncpg, qdrant-client, httpx

Usage:
    python scripts/rebuild_kb_imam_v2.py --google-api-key AIza...
    python scripts/rebuild_kb_imam_v2.py --source quran --dry-run
    python scripts/rebuild_kb_imam_v2.py --source pdf --pdf-dir /path/to/pdfs
"""
from __future__ import annotations

import argparse
import asyncio
import hashlib
import json
import logging
import os
import re
import sys
import time
import uuid
from pathlib import Path
from typing import Any

import asyncpg
import httpx
from qdrant_client import AsyncQdrantClient
from qdrant_client.http import models as qmodels

log = logging.getLogger("kb-rebuild")

# ── Constants ────────────────────────────────────────────────────────────────
COLLECTION = "kb_imam_v2_1024"
EMBEDDING_MODEL = "gemini-embedding-2-preview"
EMBEDDING_DIM = 1024
SCHEMA = "islamic_content"
GEMINI_API_URL = "https://generativelanguage.googleapis.com/v1beta/models"

# Batch sizes
EMBED_BATCH = 100       # Gemini max per request
QDRANT_BATCH = 100      # Qdrant upsert batch
EMBED_CONCURRENCY = 25  # Concurrent embedding requests (paid tier)

# UUID namespace for deterministic IDs
_NS = uuid.UUID("b1c2d3e4-f5a6-7890-abcd-ef1234567890")

HADITH_TITLES = {
    "bukhari": "Sahih al-Bukhari",
    "muslim": "Sahih Muslim",
    "abudawud": "Sunan Abu Dawud",
    "tirmidhi": "Jami at-Tirmidhi",
    "nasai": "Sunan an-Nasa'i",
    "ibnmajah": "Sunan Ibn Majah",
    "nawawi": "Forty Hadith Nawawi",
}

# PDF category → source_type mapping
PDF_CATEGORIES = {
    "Tafsir": "tafseer",
    "Fiqh": "fiqh",
    "Aqeedah": "aqeedah",
    "Seerah": "seerah",
}

# Fiqh books → madhab
FIQH_MADHAB = {
    "The book of Prayer (Hanafi)": "hanafi",
    "The book of Inheritance (Hanafi)": "hanafi",
    "The book of purification and purity (Hanafi)": "hanafi",
    "The book of fasting (Hanafi)": "hanafi",
    "Fiqh of Marriage": "general",
}


def _uuid5(key: str) -> str:
    return str(uuid.uuid5(_NS, key))


def _sha256(text: str) -> str:
    return hashlib.sha256(text.encode()).hexdigest()[:16]


# ── Gemini Embedding ─────────────────────────────────────────────────────────

class GeminiEmbedder:
    def __init__(self, api_key: str, model: str = EMBEDDING_MODEL, dim: int = EMBEDDING_DIM, concurrency: int = EMBED_CONCURRENCY):
        self.api_key = api_key
        self.model = model
        self.dim = dim
        self.concurrency = concurrency
        self._client = httpx.AsyncClient(
            timeout=httpx.Timeout(60.0, connect=10.0),
            limits=httpx.Limits(max_connections=concurrency * 2, max_keepalive_connections=concurrency),
        )
        self._sem = asyncio.Semaphore(concurrency)

    async def close(self):
        await self._client.aclose()

    async def embed_batch(self, texts: list[str]) -> list[list[float]]:
        """Embed up to 100 texts in one API call with retry."""
        async with self._sem:
            return await self._call_api(texts)

    async def _call_api(self, texts: list[str], retries: int = 4) -> list[list[float]]:
        url = f"{GEMINI_API_URL}/{self.model}:batchEmbedContents"
        requests = []
        for text in texts:
            # Truncate very long texts
            if len(text) > 8000:
                text = text[:8000]
            requests.append({
                "model": f"models/{self.model}",
                "content": {"parts": [{"text": text}]},
                "taskType": "RETRIEVAL_DOCUMENT",
                "outputDimensionality": self.dim,
            })

        for attempt in range(retries):
            try:
                resp = await self._client.post(
                    url, json={"requests": requests},
                    params={"key": self.api_key},
                    headers={"Content-Type": "application/json"},
                )
                if resp.status_code == 429:
                    # Extract retry delay if available
                    delay = 2 ** (attempt + 1)
                    m = re.search(r'"retryDelay"\s*:\s*"(\d+)s"', resp.text)
                    if m:
                        delay = max(delay, int(m.group(1)))
                    log.warning("Rate limited, waiting %ds (attempt %d/%d)", delay, attempt + 1, retries)
                    await asyncio.sleep(delay)
                    continue
                if resp.status_code >= 400:
                    raise RuntimeError(f"Gemini API {resp.status_code}: {resp.text[:300]}")
                data = resp.json()
                embeddings = data.get("embeddings", [])
                if len(embeddings) != len(texts):
                    raise RuntimeError(f"Expected {len(texts)} embeddings, got {len(embeddings)}")
                return [e["values"] for e in embeddings]
            except (httpx.TimeoutException, httpx.ConnectError) as exc:
                if attempt < retries - 1:
                    delay = 2 ** attempt
                    log.warning("Network error, retrying in %ds: %s", delay, exc)
                    await asyncio.sleep(delay)
                else:
                    raise
        raise RuntimeError("Gemini embed failed after all retries")


# ── Qdrant helpers ───────────────────────────────────────────────────────────

async def create_collection(client: AsyncQdrantClient):
    """Create the new collection (idempotent)."""
    exists = await client.collection_exists(COLLECTION)
    if exists:
        info = await client.get_collection(COLLECTION)
        log.info("Collection %s already exists (%d points)", COLLECTION, info.points_count)
        return

    await client.create_collection(
        collection_name=COLLECTION,
        vectors_config=qmodels.VectorParams(
            size=EMBEDDING_DIM,
            distance=qmodels.Distance.COSINE,
        ),
        optimizers_config=qmodels.OptimizersConfigDiff(
            indexing_threshold=20000,
        ),
    )
    # Create payload indexes for filtering
    for field, ftype in [
        ("source_type", qmodels.PayloadSchemaType.KEYWORD),
        ("collection_name", qmodels.PayloadSchemaType.KEYWORD),
        ("chapter_id", qmodels.PayloadSchemaType.INTEGER),
        ("verse_key", qmodels.PayloadSchemaType.KEYWORD),
        ("hadith_number", qmodels.PayloadSchemaType.KEYWORD),
        ("authority_rank", qmodels.PayloadSchemaType.INTEGER),
        ("level", qmodels.PayloadSchemaType.INTEGER),
        ("language", qmodels.PayloadSchemaType.KEYWORD),
        ("madhab", qmodels.PayloadSchemaType.KEYWORD),
    ]:
        await client.create_payload_index(COLLECTION, field, ftype)

    log.info("Created collection %s with indexes", COLLECTION)


async def _filter_missing(client: AsyncQdrantClient, segments: list[dict], label: str) -> list[dict]:
    """Check which segment IDs are missing from Qdrant and return only those."""
    all_ids = [s["id"] for s in segments]
    existing = set()

    # Check in batches of 100
    for i in range(0, len(all_ids), 100):
        batch_ids = all_ids[i:i + 100]
        try:
            points = await client.retrieve(COLLECTION, ids=batch_ids, with_payload=False, with_vectors=False)
            existing.update(p.id for p in points)
        except Exception:
            pass

    missing = [s for s in segments if s["id"] not in existing]
    log.info("  %s patch: %d existing, %d missing → will embed %d", label, len(existing), len(missing), len(missing))
    return missing


async def upsert_points(client: AsyncQdrantClient, points: list[qmodels.PointStruct]):
    """Upsert points in batches."""
    for i in range(0, len(points), QDRANT_BATCH):
        batch = points[i:i + QDRANT_BATCH]
        await client.upsert(COLLECTION, batch)


# ── Embed + Store pipeline ──────────────────────────────────────────────────

async def _process_one_batch(
    embedder: GeminiEmbedder,
    qdrant: AsyncQdrantClient,
    batch: list[dict],
    offset: int,
) -> int:
    """Embed + upsert a single batch. Returns count stored."""
    texts = [s["text"] for s in batch]
    try:
        vectors = await embedder.embed_batch(texts)
    except Exception as exc:
        log.error("Embed failed at offset %d: %s", offset, exc)
        return 0

    points = []
    for seg, vec in zip(batch, vectors):
        payload = {k: v for k, v in seg["payload"].items() if v is not None}
        payload["text"] = seg["text"][:1500]
        points.append(qmodels.PointStruct(id=seg["id"], vector=vec, payload=payload))

    try:
        await upsert_points(qdrant, points)
    except Exception as exc:
        log.error("Qdrant upsert failed at offset %d: %s", offset, exc)
        return 0

    return len(batch)


async def embed_and_store(
    embedder: GeminiEmbedder,
    qdrant: AsyncQdrantClient,
    segments: list[dict],
    label: str,
    dry_run: bool = False,
) -> int:
    """Embed segments and store in Qdrant using parallel pipeline."""
    if dry_run:
        log.info("[DRY-RUN] %s: %d segments", label, len(segments))
        return len(segments)

    total = len(segments)
    t0 = time.time()

    # Split into batches
    batches = []
    for i in range(0, total, EMBED_BATCH):
        batches.append((i, segments[i:i + EMBED_BATCH]))

    # Process all batches concurrently (semaphore controls actual parallelism)
    stored = 0
    # Process in waves to get progress updates
    wave_size = embedder.concurrency
    for wave_start in range(0, len(batches), wave_size):
        wave = batches[wave_start:wave_start + wave_size]
        tasks = [
            _process_one_batch(embedder, qdrant, batch, offset)
            for offset, batch in wave
        ]
        results = await asyncio.gather(*tasks, return_exceptions=True)
        for r in results:
            if isinstance(r, int):
                stored += r

        elapsed = time.time() - t0
        rate = stored / elapsed if elapsed > 0 else 0
        log.info("  %s: %d/%d (%.0f/s)", label, stored, total, rate)

    elapsed = time.time() - t0
    log.info("  %s: DONE — %d stored in %.1fs", label, stored, elapsed)
    return stored


# ══════════════════════════════════════════════════════════════════════════════
# QURAN: L1 (surah summary) → L3 (individual ayah)
# ══════════════════════════════════════════════════════════════════════════════

async def build_quran_segments(pool: asyncpg.Pool) -> list[dict]:
    """Build segments for all Quran data."""
    log.info("=== QURAN: Loading from PostgreSQL ===")

    async with pool.acquire() as conn:
        await conn.execute(f'SET search_path TO "{SCHEMA}", public')

        chapters = {r["chapter_id"]: dict(r) for r in await conn.fetch(
            "SELECT chapter_id, name_simple, name_arabic, translated_name, "
            "revelation_place, verses_count FROM quran_chapters ORDER BY chapter_id"
        )}
        # JOIN with quran_ayah_translations for Sahih International (translation_id=20)
        ayahs = [dict(r) for r in await conn.fetch("""
            SELECT a.verse_key, a.chapter_id, a.ayah_number, a.juz_number,
                   a.arabic_text, a.transliteration_text,
                   COALESCE(t.translation_text, a.translation_text, '') AS translation_text
            FROM quran_ayahs a
            LEFT JOIN quran_ayah_translations t
              ON t.verse_key = a.verse_key AND t.translation_id = 20
            ORDER BY a.chapter_id, a.ayah_number
        """)]

    log.info("  Loaded %d chapters, %d ayahs", len(chapters), len(ayahs))
    if not ayahs:
        log.error("  No Quran ayahs found! Check islamic_content schema.")
        return []

    segments = []

    # Group ayahs by chapter
    ch_ayahs: dict[int, list[dict]] = {}
    for a in ayahs:
        ch_ayahs.setdefault(a["chapter_id"], []).append(a)

    for cid, ch in chapters.items():
        surah_name = ch["name_simple"]
        surah_ar = ch.get("name_arabic", "")
        surah_ayahs = ch_ayahs.get(cid, [])

        # ── L1: Surah summary ──
        l1_parts = [
            f"Surah {surah_name} ({surah_ar})",
            f"Chapter {cid}, {ch['verses_count']} verses, Revelation: {ch.get('revelation_place', 'unknown')}",
            "",
        ]
        # Include first few ayahs for context
        for a in surah_ayahs[:5]:
            l1_parts.append(f"[{a['verse_key']}] {a['arabic_text']}")
            if a.get("translation_text"):
                l1_parts.append(f"  {a['translation_text']}")
        if len(surah_ayahs) > 5:
            l1_parts.append(f"... ({len(surah_ayahs) - 5} more verses)")

        segments.append({
            "id": _uuid5(f"quran-l1-surah-{cid}"),
            "text": "\n".join(l1_parts),
            "payload": {
                "source_type": "quran",
                "citation_text": f"Surah {surah_name} (Chapter {cid})",
                "chapter_id": cid,
                "chapter_name_en": surah_name,
                "chapter_name_ar": surah_ar,
                "revelation_place": ch.get("revelation_place"),
                "level": 1,
                "authority_rank": 1,
                "language": "ar_en",
            },
        })

        # ── L3: Individual ayahs ──
        for a in surah_ayahs:
            vk = a["verse_key"]
            arabic = a.get("arabic_text", "")
            translation = a.get("translation_text", "")
            transliteration = a.get("transliteration_text", "")

            # Build embedding text per handoff spec
            parts = [f"Quran {vk} ({surah_name})"]
            if arabic:
                parts.append(f"Arabic: {arabic}")
            if transliteration:
                parts.append(f"Transliteration: {transliteration}")
            if translation:
                parts.append(f"Translation (Sahih International): {translation}")

            embed_text = "\n".join(parts)

            segments.append({
                "id": _uuid5(f"quran-ayah-{vk}"),
                "text": embed_text,
                "payload": {
                    "source_type": "quran",
                    "citation_text": f"Quran {vk} - Sahih International",
                    "chapter_id": cid,
                    "ayah_number": a["ayah_number"],
                    "verse_key": vk,
                    "chapter_name_en": surah_name,
                    "chapter_name_ar": surah_ar,
                    "juz_number": a.get("juz_number"),
                    "revelation_place": ch.get("revelation_place"),
                    "translation": "Sahih International",
                    "level": 3,
                    "authority_rank": 1,
                    "language": "ar_en",
                },
            })

    l1 = sum(1 for s in segments if s["payload"]["level"] == 1)
    l3 = sum(1 for s in segments if s["payload"]["level"] == 3)
    log.info("  Quran segments: L1=%d L3=%d total=%d", l1, l3, len(segments))
    return segments


# ══════════════════════════════════════════════════════════════════════════════
# HADITH: L1 (collection) → L3 (individual hadith)
# ══════════════════════════════════════════════════════════════════════════════

async def build_hadith_segments(pool: asyncpg.Pool) -> list[dict]:
    """Build segments for all Hadith data from PostgreSQL."""
    log.info("=== HADITH: Loading from PostgreSQL ===")

    async with pool.acquire() as conn:
        await conn.execute(f'SET search_path TO "{SCHEMA}", public')

        collections = {r["name"]: dict(r) for r in await conn.fetch(
            "SELECT name, title, total_hadith FROM hadith_collections ORDER BY name"
        )}

        # Fetch all hadiths with localizations in one query
        rows = await conn.fetch("""
            SELECT
                h.id, h.collection_name, h.book_number, h.hadith_number,
                h.chapter_title,
                l_en.body_text AS text_en,
                l_ar.body_text AS text_ar
            FROM hadith_items h
            LEFT JOIN hadith_localizations l_en
                ON l_en.hadith_item_id = h.id AND l_en.language = 'en'
            LEFT JOIN hadith_localizations l_ar
                ON l_ar.hadith_item_id = h.id AND l_ar.language = 'ar'
            ORDER BY h.collection_name, h.book_number, h.hadith_number
        """)

    log.info("  Loaded %d collections, %d hadith rows", len(collections), len(rows))
    if not rows:
        log.error("  No hadith found! Check islamic_content schema.")
        return []

    segments = []

    # Group by collection
    coll_hadiths: dict[str, list[dict]] = {}
    for r in rows:
        d = dict(r)
        coll_hadiths.setdefault(d["collection_name"], []).append(d)

    for coll_name, hadiths in coll_hadiths.items():
        coll = collections.get(coll_name, {})
        title = coll.get("title") or HADITH_TITLES.get(coll_name, coll_name.title())

        # ── L1: Collection summary ──
        segments.append({
            "id": _uuid5(f"hadith-l1-{coll_name}"),
            "text": f"{title}\n\nA collection of {len(hadiths)} authenticated Hadith narrations.",
            "payload": {
                "source_type": "hadith",
                "citation_text": title,
                "collection_name": coll_name,
                "collection_title": title,
                "level": 1,
                "authority_rank": 2,
                "language": "ar_en",
            },
        })

        # ── L3: Individual hadith ──
        for h in hadiths:
            hnum = str(h["hadith_number"])
            book_num = str(h.get("book_number", "0"))
            text_en = (h.get("text_en") or "").strip()
            text_ar = (h.get("text_ar") or "").strip()

            if not text_en and not text_ar:
                continue

            # Build embedding text per handoff spec
            citation = f"{title}, Book {book_num}, Hadith {hnum}"
            parts = [citation]
            if text_ar:
                parts.append(f"Arabic: {text_ar}")
            if text_en:
                parts.append(f"English: {text_en}")

            embed_text = "\n".join(parts)

            segments.append({
                "id": _uuid5(f"hadith-{coll_name}-{hnum}"),
                "text": embed_text,
                "payload": {
                    "source_type": "hadith",
                    "citation_text": citation,
                    "collection_name": coll_name,
                    "collection_title": title,
                    "book_number": book_num,
                    "hadith_number": hnum,
                    "chapter_title": h.get("chapter_title", ""),
                    "level": 3,
                    "authority_rank": 2,
                    "language": "ar_en" if text_ar else "en",
                },
            })

    l1 = sum(1 for s in segments if s["payload"]["level"] == 1)
    l3 = sum(1 for s in segments if s["payload"]["level"] == 3)
    log.info("  Hadith segments: L1=%d L3=%d total=%d", l1, l3, len(segments))
    return segments


# ══════════════════════════════════════════════════════════════════════════════
# DUA: L1 (overview) → L2 (category) → L3 (individual dua)
# ══════════════════════════════════════════════════════════════════════════════

async def build_dua_segments(pool: asyncpg.Pool) -> list[dict]:
    """Build segments for all Dua data."""
    log.info("=== DUA: Loading from PostgreSQL ===")

    async with pool.acquire() as conn:
        await conn.execute(f'SET search_path TO "{SCHEMA}", public')

        categories = [dict(r) for r in await conn.fetch(
            "SELECT category, dua_count FROM dua_categories ORDER BY category"
        )]
        duas = [dict(r) for r in await conn.fetch(
            "SELECT dua_id, category, title, arabic_text, transliteration, "
            "english_meaning, source, reference, authenticity, occasion "
            "FROM dua_items ORDER BY category, dua_id"
        )]

    log.info("  Loaded %d categories, %d duas", len(categories), len(duas))
    if not duas:
        log.error("  No duas found! Check islamic_content schema.")
        return []

    segments = []

    # ── L1: Overview ──
    cat_list = "\n".join(f"- {c['category']} ({c['dua_count']} duas)" for c in categories)
    segments.append({
        "id": _uuid5("dua-l1-overview"),
        "text": f"Islamic Dua and Adhkar Collection\n\n{len(categories)} categories, {len(duas)} duas.\n\nCategories:\n{cat_list}",
        "payload": {
            "source_type": "dua",
            "citation_text": "Islamic Dua and Adhkar Collection",
            "level": 1,
            "authority_rank": 3,
            "language": "ar_en",
        },
    })

    # Group by category
    cat_duas: dict[str, list[dict]] = {}
    for d in duas:
        cat_duas.setdefault(d["category"], []).append(d)

    for cat, items in cat_duas.items():
        # ── L2: Category summary ──
        l2_parts = [f"Dua Category: {cat}\n"]
        for d in items:
            l2_parts.append(f"- {d['title']}: {(d.get('english_meaning') or '')[:100]}...")
        segments.append({
            "id": _uuid5(f"dua-l2-{cat}"),
            "text": "\n".join(l2_parts),
            "payload": {
                "source_type": "dua",
                "citation_text": f"Hisnul Muslim, {cat}",
                "category": cat,
                "level": 2,
                "authority_rank": 3,
                "language": "ar_en",
            },
        })

        # ── L3: Individual dua ──
        for d in items:
            # Build embedding text per handoff spec
            parts = [d["title"], f"Category: {cat}"]
            if d.get("arabic_text"):
                parts.append(f"Arabic: {d['arabic_text']}")
            if d.get("transliteration"):
                parts.append(f"Transliteration: {d['transliteration']}")
            if d.get("english_meaning"):
                parts.append(f"Meaning: {d['english_meaning']}")
            if d.get("source") and d.get("reference"):
                parts.append(f"Source: {d['source']}, {d['reference']}")
            if d.get("occasion"):
                parts.append(f"Occasion: {d['occasion']}")
            if d.get("authenticity"):
                parts.append(f"Authenticity: {d['authenticity']}")

            embed_text = "\n".join(parts)
            citation = f"Hisnul Muslim, {cat} - {d['title']}"

            segments.append({
                "id": _uuid5(f"dua-{d['dua_id']}"),
                "text": embed_text,
                "payload": {
                    "source_type": "dua",
                    "citation_text": citation,
                    "dua_id": d["dua_id"],
                    "category": cat,
                    "title": d["title"],
                    "source": d.get("source", ""),
                    "reference": d.get("reference", ""),
                    "authenticity": d.get("authenticity", ""),
                    "level": 3,
                    "authority_rank": 3,
                    "language": "ar_en",
                },
            })

    l1 = sum(1 for s in segments if s["payload"]["level"] == 1)
    l2 = sum(1 for s in segments if s["payload"]["level"] == 2)
    l3 = sum(1 for s in segments if s["payload"]["level"] == 3)
    log.info("  Dua segments: L1=%d L2=%d L3=%d total=%d", l1, l2, l3, len(segments))
    return segments


# ══════════════════════════════════════════════════════════════════════════════
# PDF: Tafsir / Fiqh / Aqeedah / Seerah
# ══════════════════════════════════════════════════════════════════════════════

def _classify_pdf(filename: str) -> tuple[str, str, str]:
    """Classify PDF → (source_type, book_title, madhab|None)."""
    name = Path(filename).stem

    # Tafsir
    if "tafsir" in name.lower() or "atlas" in name.lower() or "stories" in name.lower():
        return "tafseer", name, ""
    # Aqeedah
    if any(k in name.lower() for k in ["imaan", "tawheed", "creed", "aqeedah", "wasitiyah", "islam", "jesus"]):
        return "aqeedah", name, ""
    # Seerah
    if any(k in name.lower() for k in ["prophet", "sealed", "raheeq", "muhammad", "seerah"]):
        return "seerah", name, ""
    # Fiqh
    if any(k in name.lower() for k in ["prayer", "fiqh", "inheritance", "purification", "fasting", "marriage"]):
        madhab = "hanafi" if "hanafi" in name.lower() else "general"
        return "fiqh", name, madhab
    # Fallback
    return "general_islamic", name, ""


def chunk_text(text: str, chunk_size: int = 800, overlap: int = 150) -> list[str]:
    """Split text into overlapping chunks by word count."""
    words = text.split()
    if len(words) <= chunk_size:
        return [text] if text.strip() else []

    chunks = []
    start = 0
    while start < len(words):
        end = min(start + chunk_size, len(words))
        chunk = " ".join(words[start:end])
        if chunk.strip():
            chunks.append(chunk)
        start += chunk_size - overlap
    return chunks


VLM_OCR_PROMPT = "OCR this page. Extract ALL text preserving Arabic and English exactly as written. Maintain RTL order for Arabic. Preserve paragraph breaks. Output text only."

# SiliconFlow DeepSeek-OCR (free, OpenAI-compatible API)
SILICONFLOW_URL = "https://api.siliconflow.cn/v1/chat/completions"
SILICONFLOW_MODEL = "deepseek-ai/DeepSeek-OCR"


class VLMPageOCR:
    """PDF page OCR via SiliconFlow DeepSeek-OCR with API key pool round-robin."""

    def __init__(self, api_keys: list[str], model: str = SILICONFLOW_MODEL,
                 base_url: str = SILICONFLOW_URL):
        if not api_keys:
            raise ValueError("At least one API key required")
        self.api_keys = api_keys
        self.model = model
        self.base_url = base_url
        self._key_index = 0
        self._lock = asyncio.Lock()
        self._client = httpx.AsyncClient(
            timeout=httpx.Timeout(120.0, connect=10.0),
            limits=httpx.Limits(max_connections=50, max_keepalive_connections=25),
        )
        # 5 concurrent per key, total = 5 × num_keys
        concurrency = 5 * len(api_keys)
        self._sem = asyncio.Semaphore(concurrency)
        log.info("OCR pool: %d keys, concurrency=%d", len(api_keys), concurrency)

    async def _next_key(self) -> str:
        """Round-robin key selection."""
        async with self._lock:
            key = self.api_keys[self._key_index % len(self.api_keys)]
            self._key_index += 1
            return key

    async def close(self):
        await self._client.aclose()

    async def ocr_page(self, image_bytes: bytes) -> str:
        async with self._sem:
            return await self._call_with_retry(image_bytes)

    async def _call_with_retry(self, image_bytes: bytes, retries: int = 3) -> str:
        import base64
        b64 = base64.b64encode(image_bytes).decode()
        mime = "image/png" if image_bytes[:8] == b"\x89PNG\r\n\x1a\n" else "image/jpeg"

        payload = {
            "model": self.model,
            "messages": [{"role": "user", "content": [
                {"type": "image_url", "image_url": {"url": f"data:{mime};base64,{b64}"}},
                {"type": "text", "text": VLM_OCR_PROMPT},
            ]}],
            "max_tokens": 4096,
        }

        for attempt in range(retries):
            key = await self._next_key()
            try:
                resp = await self._client.post(
                    self.base_url, json=payload,
                    headers={"Authorization": f"Bearer {key}", "Content-Type": "application/json"},
                )
                if resp.status_code == 429:
                    # This key is rate limited, try next key immediately
                    delay = 1 if len(self.api_keys) > 1 else 2 ** (attempt + 1)
                    log.warning("OCR 429 on key ..%s, rotate (attempt %d/%d)",
                                key[-6:], attempt + 1, retries)
                    await asyncio.sleep(delay)
                    continue
                if resp.status_code >= 400:
                    log.warning("OCR error %d: %s", resp.status_code, resp.text[:200])
                    if attempt < retries - 1:
                        await asyncio.sleep(2 ** attempt)
                    continue
                data = resp.json()
                text = data.get("choices", [{}])[0].get("message", {}).get("content", "").strip()
                return text
            except Exception as exc:
                if attempt < retries - 1:
                    await asyncio.sleep(2 ** attempt)
                else:
                    log.error("OCR failed after %d attempts: %s", retries, exc)
        return ""


def _render_page_to_image(page, dpi: int = 200, max_bytes: int = 3_000_000) -> bytes:
    """Render a PyMuPDF page to PNG bytes, scaling down if needed."""
    import fitz
    for d in [dpi, 150, 120]:
        mat = fitz.Matrix(d / 72, d / 72)
        pix = page.get_pixmap(matrix=mat)
        img_bytes = pix.tobytes("png")
        if len(img_bytes) <= max_bytes:
            return img_bytes
    # Aggressive scale
    mat = fitz.Matrix(0.8, 0.8)
    pix = page.get_pixmap(matrix=mat)
    return pix.tobytes("jpeg")


async def build_pdf_segments(pdf_dir: str, vlm_ocr: VLMPageOCR | None = None) -> list[dict]:
    """Build segments from PDF files using VLM OCR for text extraction."""
    try:
        import fitz  # PyMuPDF
    except ImportError:
        log.error("PyMuPDF not installed. Run: pip install PyMuPDF")
        return []

    pdf_path = Path(pdf_dir)
    if not pdf_path.exists():
        log.error("PDF directory not found: %s", pdf_dir)
        return []

    pdf_files = sorted(pdf_path.glob("**/*.pdf"))
    log.info("=== PDF: Found %d files in %s ===", len(pdf_files), pdf_dir)
    if vlm_ocr:
        log.info("  Using VLM OCR model: %s (%d keys)", vlm_ocr.model, len(vlm_ocr.api_keys))

    # OCR cache directory — saves results per PDF so we can resume after interruption
    cache_dir = Path(pdf_dir) / ".ocr_cache"
    cache_dir.mkdir(exist_ok=True)

    segments = []

    for pdf_file in pdf_files:
        source_type, book_title, madhab = _classify_pdf(pdf_file.name)
        log.info("  Processing: %s → %s", pdf_file.name, source_type)

        # Check OCR cache first
        cache_file = cache_dir / f"{pdf_file.stem}.json"
        total_pages = 0
        if cache_file.exists() and vlm_ocr:
            try:
                cached = json.loads(cache_file.read_text())
                page_texts = [(p["page"], p["text"]) for p in cached]
                total_pages = max((p["page"] for p in cached), default=0)
                log.info("  %s: loaded %d pages from cache", pdf_file.name, len(page_texts))
            except Exception:
                page_texts = []
                cache_file.unlink(missing_ok=True)
        else:
            page_texts = []

        if not page_texts:
            try:
                doc = fitz.open(str(pdf_file))
            except Exception as exc:
                log.error("  Failed to open %s: %s", pdf_file.name, exc)
                continue

            total_pages = len(doc)
            page_texts = []

            if vlm_ocr:
                # ── VLM OCR: render pages → images → DeepSeek-OCR ──
                batch_size = 5 * len(vlm_ocr.api_keys)
                for batch_start in range(0, total_pages, batch_size):
                    batch_end = min(batch_start + batch_size, total_pages)
                    tasks = []
                    for pn in range(batch_start, batch_end):
                        page = doc[pn]
                        img_bytes = _render_page_to_image(page)
                        tasks.append((pn + 1, vlm_ocr.ocr_page(img_bytes)))

                    results = await asyncio.gather(*[t[1] for t in tasks], return_exceptions=True)
                    for (pn, _), result in zip(tasks, results):
                        if isinstance(result, str) and result.strip():
                            page_texts.append((pn, result))
                        elif isinstance(result, Exception):
                            log.warning("  Page %d OCR failed: %s", pn, result)

                    if batch_end % 50 == 0 or batch_end == total_pages:
                        log.info("    %s: OCR %d/%d pages (%d with text)",
                                 pdf_file.name, batch_end, total_pages, len(page_texts))

                    num_keys = len(vlm_ocr.api_keys) if vlm_ocr else 1
                    sleep_sec = max(1, 4 // num_keys)
                    await asyncio.sleep(sleep_sec)

                # Save OCR results to cache
                cache_data = [{"page": p, "text": t} for p, t in page_texts]
                cache_file.write_text(json.dumps(cache_data, ensure_ascii=False))
                log.info("  %s: cached %d pages to %s", pdf_file.name, len(page_texts), cache_file.name)
            else:
                # ── Fallback: PyMuPDF text extraction ──
                for pn in range(total_pages):
                    page = doc[pn]
                    text = page.get_text("text")
                    if text.strip():
                        page_texts.append((pn + 1, text))

            doc.close()

        if not page_texts:
            log.warning("  Empty PDF: %s", pdf_file.name)
            continue

        # Combine all page texts with page markers
        full_text = ""
        page_breaks = []
        for pn, text in page_texts:
            page_breaks.append((len(full_text), pn))
            full_text += text + "\n\n"

        # Chunk the text
        chunks = chunk_text(full_text, chunk_size=800, overlap=150)
        log.info("  %s: %d/%d pages → %d chunks", pdf_file.name, len(page_texts), total_pages, len(chunks))

        for idx, chunk in enumerate(chunks):
            # Estimate page number
            chunk_offset = full_text.find(chunk[:50])
            page_num = 1
            for offset, pn in page_breaks:
                if offset <= chunk_offset:
                    page_num = pn
                else:
                    break

            citation = book_title
            seg_payload = {
                "source_type": source_type,
                "citation_text": citation,
                "book_title": book_title,
                "file_name": pdf_file.name,
                "page_number": page_num,
                "chunk_index": idx,
                "level": 3,
                "authority_rank": 4,
                "language": "en",
            }
            if madhab:
                seg_payload["madhab"] = madhab

            embed_text = f"{book_title} (p.{page_num})\n\n{chunk}"

            segments.append({
                "id": _uuid5(f"pdf-{_sha256(pdf_file.name)}-{idx}"),
                "text": embed_text,
                "payload": seg_payload,
            })

    log.info("  PDF total: %d segments from %d files", len(segments), len(pdf_files))
    return segments


# ══════════════════════════════════════════════════════════════════════════════
# Main
# ══════════════════════════════════════════════════════════════════════════════

async def run(args):
    t_start = time.time()

    # Connect to PostgreSQL
    log.info("Connecting to PostgreSQL: %s", args.dsn.split("@")[-1])
    pool = await asyncpg.create_pool(args.dsn, min_size=2, max_size=5)

    # Connect to Qdrant
    log.info("Connecting to Qdrant: %s", args.qdrant_url)
    qdrant = AsyncQdrantClient(url=args.qdrant_url, timeout=60)

    # Create collection
    await create_collection(qdrant)

    # Init embedder with configured concurrency
    embedder = GeminiEmbedder(api_key=args.google_api_key, concurrency=args.concurrency)

    sources = [s.strip() for s in args.source.split(",")]
    results = {}

    try:
        # Phase 1: Structured data
        if "quran" in sources or "all" in sources:
            segs = await build_quran_segments(pool)
            if args.patch:
                segs = await _filter_missing(qdrant, segs, "Quran")
            results["quran"] = await embed_and_store(embedder, qdrant, segs, "Quran", args.dry_run)

        if "hadith" in sources or "all" in sources:
            segs = await build_hadith_segments(pool)
            if args.patch:
                segs = await _filter_missing(qdrant, segs, "Hadith")
            results["hadith"] = await embed_and_store(embedder, qdrant, segs, "Hadith", args.dry_run)

        if "dua" in sources or "all" in sources:
            segs = await build_dua_segments(pool)
            if args.patch:
                segs = await _filter_missing(qdrant, segs, "Dua")
            results["dua"] = await embed_and_store(embedder, qdrant, segs, "Dua", args.dry_run)

        # Phase 2: PDFs
        if "pdf" in sources or "all" in sources:
            if args.pdf_dir:
                # Init VLM OCR if requested
                vlm = None
                if args.vlm_ocr:
                    # Build key pool from comma-separated list
                    keys = [k.strip() for k in args.siliconflow_keys.split(",") if k.strip()]
                    if not keys:
                        log.error("--siliconflow-keys required for VLM OCR")
                    else:
                        vlm = VLMPageOCR(api_keys=keys, model=args.vlm_model)
                        log.info("VLM OCR enabled: model=%s, %d API keys", args.vlm_model, len(keys))
                segs = await build_pdf_segments(args.pdf_dir, vlm_ocr=vlm)
                if args.patch:
                    segs = await _filter_missing(qdrant, segs, "PDF")
                results["pdf"] = await embed_and_store(embedder, qdrant, segs, "PDF", args.dry_run)
                if vlm:
                    await vlm.close()
            else:
                log.warning("PDF source requested but --pdf-dir not specified, skipping")

        # Final stats
        info = await qdrant.get_collection(COLLECTION)
        elapsed = time.time() - t_start
        log.info("═══ REBUILD COMPLETE ═══")
        log.info("  Collection: %s", COLLECTION)
        log.info("  Total points: %d", info.points_count)
        log.info("  Results: %s", json.dumps(results))
        log.info("  Elapsed: %.1fs", elapsed)

    finally:
        await embedder.close()
        await qdrant.close()
        await pool.close()


def main():
    logging.basicConfig(
        level=logging.INFO,
        format="%(asctime)s [%(levelname)s] %(name)s - %(message)s",
        datefmt="%H:%M:%S",
    )

    p = argparse.ArgumentParser(description="Rebuild Islamic KB v2")
    p.add_argument("--dsn", default=os.environ.get(
        "DATABASE_DSN",
        os.environ.get("GATEWAY_DATABASE__DSN", "postgresql://postgres:111111@127.0.0.1:5432/gateway"),
    ))
    p.add_argument("--qdrant-url", default=os.environ.get("QDRANT_URL", "http://127.0.0.1:6333"))
    p.add_argument("--google-api-key", default=os.environ.get("GOOGLE_API_KEY", ""))
    p.add_argument("--source", default="all", help="quran,hadith,dua,pdf or all")
    p.add_argument("--pdf-dir", default=os.environ.get("PDF_DIR", ""), help="Path to PDF directory")
    p.add_argument("--concurrency", type=int, default=25, help="Concurrent embedding requests (default 25)")
    p.add_argument("--vlm-ocr", action="store_true", help="Use VLM OCR for PDF text extraction")
    p.add_argument("--vlm-model", default="deepseek-ai/DeepSeek-OCR", help="VLM model (default: SiliconFlow DeepSeek-OCR)")
    p.add_argument("--siliconflow-keys", default=os.environ.get("SILICONFLOW_API_KEYS", os.environ.get("SILICONFLOW_API_KEY", "")),
                   help="SiliconFlow API keys, comma-separated for pool (e.g. sk-aaa,sk-bbb,sk-ccc)")
    p.add_argument("--patch", action="store_true", help="Only embed segments missing from Qdrant")
    p.add_argument("--dry-run", action="store_true", help="Count segments without embedding")
    p.add_argument("--reset", action="store_true", help="Delete existing collection first")
    args = p.parse_args()

    if not args.google_api_key and not args.dry_run:
        log.error("--google-api-key required (or set GOOGLE_API_KEY env var)")
        sys.exit(1)

    asyncio.run(run(args))


if __name__ == "__main__":
    main()
