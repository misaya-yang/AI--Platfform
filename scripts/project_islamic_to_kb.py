#!/usr/bin/env python3
"""Project Islamic content into unified KB with hierarchical indexing.

Uses L1/L2/L3 hierarchy:
  L1 (Document):  Surah summary / Collection summary / Category summary
  L2 (Section):   Large thematic groups (~1500 tokens)
                   Quran: ~10-15 ayah blocks with full Arabic+transliteration+translation
                   Hadith: chapter-level grouping
                   Dua: category-level grouping
  L3 (Paragraph): Precise retrieval units (~400 tokens)
                   Quran: triplet blocks (3 ayah)
                   Hadith: individual hadith full text
                   Dua: individual dua

Usage:
    conda run -n ai_gateway python scripts/project_islamic_to_kb.py --source all
    conda run -n ai_gateway python scripts/project_islamic_to_kb.py --source quran
    conda run -n ai_gateway python scripts/project_islamic_to_kb.py --source hadith --hadith-collections bukhari,muslim
"""
from __future__ import annotations

import argparse
import asyncio
import hashlib
import json
import logging
import os
import sys
import uuid
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

import httpx

ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from src.config.settings import Settings
from src.persistence.database import DatabaseStorage
from src.services.knowledge.embedding import EmbeddingConfig, create_embedding
from src.services.knowledge.vector_store import VectorStore

logger = logging.getLogger("islamic_kb")

# ---------------------------------------------------------------------------
# Constants
# ---------------------------------------------------------------------------
TENANT_ID = "islamic-content"
OWNER_ID = "system-projector"
DATASET_ID = "islamic-knowledge"
EMBEDDING_MODEL = "gemini-embedding-001"
EMBEDDING_DIM = 1024
BATCH_EMBED = 20
BATCH_QDRANT = 32
ISLAMIC_SCHEMA = "islamic_content_runtime"
_NS = uuid.UUID("a1b2c3d4-e5f6-7890-abcd-ef1234567890")

# L2 section: group N ayahs into ~1500-token blocks
QURAN_L2_AYAH_GROUP_SIZE = 12  # ~12 ayahs = ~1500 tokens with 3 languages

DEFAULT_HADITH_COLLECTIONS = [
    "bukhari", "muslim", "abudawud", "tirmidhi", "nasai",
    "ibnmajah", "malik", "nawawi", "qudsi", "dehlawi",
]


def _uuid(key: str) -> str:
    return str(uuid.uuid5(_NS, key))


def _sha256(text: str) -> str:
    return hashlib.sha256(text.encode()).hexdigest()


# ---------------------------------------------------------------------------
# DB helper
# ---------------------------------------------------------------------------
async def _fetch(db: DatabaseStorage, query: str) -> list[dict]:
    if not db._pool:
        raise RuntimeError("DB not connected")
    async with db._pool.acquire() as conn:
        await conn.execute(f'SET search_path TO "{ISLAMIC_SCHEMA}", public')
        return [dict(r) for r in await conn.fetch(query)]


async def _ensure_doc(db, doc_id, title):
    await db.save_document({
        "document_id": doc_id, "dataset_id": DATASET_ID,
        "title": title, "source_type": "islamic_projection",
        "status": "completed", "progress": 100.0,
        "completed_at": datetime.now(timezone.utc),
    })


def _make_segment(seg_id, doc_id, position, text, level, parent_id,
                   source_type, citation, source_ref, section_header, summary=None):
    """Build a segment dict with all required fields."""
    return {
        "segment_id": seg_id,
        "dataset_id": DATASET_ID,
        "document_id": doc_id,
        "position": position,
        "level": level,
        "parent_segment_id": parent_id,
        "summary": summary,
        "text": text,
        "token_count": len(text.split()),
        "word_count": len(text.split()),
        "content_hash": _sha256(text),
        "source_type": source_type,
        "citation_text": citation,
        "source_reference": json.dumps(source_ref) if isinstance(source_ref, dict) else source_ref,
        "language": "ar_en",
        "section_header": section_header,
        "contextual_prefix": "",
        "metadata": json.dumps({"source_type": source_type, "authority_rank": source_ref.get("authority_rank", 4) if isinstance(source_ref, dict) else 1}),
        "enabled": True,
        "created_by": OWNER_ID,
    }


# ---------------------------------------------------------------------------
# Batch embed + store
# ---------------------------------------------------------------------------
async def _embed_store(db, vs, embedder, collection, segments, dry_run=False):
    from qdrant_client.http import models as qmodels

    if dry_run:
        logger.info("[DRY-RUN] %d segments", len(segments))
        return len(segments)

    stored = 0
    for i in range(0, len(segments), BATCH_EMBED):
        batch = segments[i : i + BATCH_EMBED]
        texts = [s["text"] for s in batch]
        try:
            vectors = await embedder.embed_documents(texts)
        except Exception as exc:
            logger.error("Embed failed batch %d: %s", i, exc)
            continue

        points = []
        for seg, vec in zip(batch, vectors):
            payload = {
                "dataset_id": DATASET_ID,
                "document_id": seg["document_id"],
                "segment_id": seg["segment_id"],
                "tenant_id": TENANT_ID,
                "text": seg["text"][:1000],
                "source_type": seg.get("source_type", ""),
                "citation_text": seg.get("citation_text", ""),
                "language": seg.get("language", "ar_en"),
                "section_title": seg.get("section_header", ""),
                "level": seg.get("level", 3),
            }
            points.append(qmodels.PointStruct(id=seg["segment_id"], vector=vec, payload=payload))

        for j in range(0, len(points), BATCH_QDRANT):
            try:
                await vs.upsert(collection, points[j : j + BATCH_QDRANT])
            except Exception as exc:
                logger.error("Qdrant upsert failed: %s", exc)

        for seg in batch:
            seg["vector_id"] = seg["segment_id"]
            seg["status"] = "completed"
        try:
            await db.insert_segments(batch)
        except Exception as exc:
            logger.error("DB insert failed: %s", exc)

        stored += len(batch)
        if stored % 200 == 0 or stored == len(segments):
            logger.info("  progress: %d / %d", stored, len(segments))
        await asyncio.sleep(0.1)
    return stored


# ===========================================================================
# QURAN: L1 (surah summary) → L2 (~12 ayah sections) → L3 (triplet blocks)
# ===========================================================================
async def project_quran(db, vs, embedder, collection, dry_run):
    logger.info("=== Quran: hierarchical L1/L2/L3 ===")

    chapters = {r["chapter_id"]: r for r in await _fetch(db, "SELECT * FROM quran_chapters ORDER BY chapter_id")}
    ayahs = await _fetch(db, "SELECT * FROM quran_ayahs ORDER BY chapter_id, ayah_number")
    triplets = await _fetch(db, "SELECT * FROM quran_triplet_blocks ORDER BY chapter_id, start_ayah_number")

    if not chapters:
        return {"status": "empty"}

    # Group ayahs by chapter
    ch_ayahs: dict[int, list[dict]] = {}
    for a in ayahs:
        ch_ayahs.setdefault(a["chapter_id"], []).append(a)

    # Group triplets by chapter
    ch_triplets: dict[int, list[dict]] = {}
    for t in triplets:
        ch_triplets.setdefault(t["chapter_id"], []).append(t)

    all_segments = []

    for cid, ch in chapters.items():
        doc_id = f"quran-surah-{cid:03d}"
        surah_name = ch["name_simple"]
        surah_arabic = ch.get("name_arabic", "")
        await _ensure_doc(db, doc_id, f"Surah {surah_name} ({surah_arabic})")

        surah_ayahs = ch_ayahs.get(cid, [])
        surah_triplets = ch_triplets.get(cid, [])

        # --- L1: Surah summary (full surah text, truncated for embedding) ---
        l1_id = _uuid(f"quran-l1-{cid}")
        l1_parts = [f"Surah {surah_name} ({surah_arabic}) - {ch.get('translated_name', '')}"]
        l1_parts.append(f"Revelation: {ch.get('revelation_place', 'unknown')}, {ch['verses_count']} verses")
        l1_parts.append("")
        for a in surah_ayahs[:30]:  # first 30 ayahs for summary
            l1_parts.append(f"{a['verse_key']}: {a['arabic_text']}")
            l1_parts.append(f"  {a['translation_text']}")
        if len(surah_ayahs) > 30:
            l1_parts.append(f"... ({len(surah_ayahs) - 30} more verses)")
        l1_text = "\n".join(l1_parts)
        # Cap at ~2000 tokens
        if len(l1_text.split()) > 2000:
            words = l1_text.split()[:2000]
            l1_text = " ".join(words)

        all_segments.append(_make_segment(
            l1_id, doc_id, 0, l1_text, level=1, parent_id=None,
            source_type="quran",
            citation=f"Surah {surah_name} (Chapter {cid})",
            source_ref={"chapter_id": cid, "surah": cid, "authority_rank": 1},
            section_header=f"Surah {surah_name}",
            summary=f"Surah {surah_name} ({surah_arabic}), {ch['verses_count']} verses, {ch.get('revelation_place', '')}",
        ))

        # --- L2: Section blocks (~12 ayahs each, ~1500 tokens) ---
        for group_start in range(0, len(surah_ayahs), QURAN_L2_AYAH_GROUP_SIZE):
            group = surah_ayahs[group_start : group_start + QURAN_L2_AYAH_GROUP_SIZE]
            first_num = group[0]["ayah_number"]
            last_num = group[-1]["ayah_number"]
            l2_id = _uuid(f"quran-l2-{cid}-{first_num}-{last_num}")

            l2_parts = [f"Surah {surah_name} ({surah_arabic}), Ayahs {first_num}-{last_num}\n"]
            for a in group:
                l2_parts.append(f"[{a['verse_key']}]")
                l2_parts.append(f"Arabic: {a['arabic_text']}")
                if a.get("transliteration_text"):
                    l2_parts.append(f"Transliteration: {a['transliteration_text']}")
                l2_parts.append(f"Translation: {a['translation_text']}")
                l2_parts.append("")
            l2_text = "\n".join(l2_parts)

            all_segments.append(_make_segment(
                l2_id, doc_id, 1_000_000 + first_num, l2_text, level=2, parent_id=l1_id,
                source_type="quran",
                citation=f"Quran {cid}:{first_num}-{last_num} - Saheeh International",
                source_ref={"chapter_id": cid, "surah": cid, "verse_start": first_num, "verse_end": last_num, "authority_rank": 1},
                section_header=f"Surah {surah_name}",
            ))

        # --- L3: Triplet blocks (3 ayah, precise retrieval) ---
        for t in surah_triplets:
            l3_id = _uuid(f"quran-l3-{t['block_id']}")
            ref = f"{cid}:{t['start_ayah_number']}-{t['end_ayah_number']}"

            l3_parts = [f"Surah {surah_name} ({surah_arabic}), Ayahs {t['start_ayah_number']}-{t['end_ayah_number']}\n"]
            l3_parts.append(f"Arabic:\n{t['arabic_text']}\n")
            if t.get("transliteration_text"):
                l3_parts.append(f"Transliteration:\n{t['transliteration_text']}\n")
            l3_parts.append(f"Translation:\n{t['translation_text']}")
            l3_text = "\n".join(l3_parts)

            # Find parent L2
            parent_l2 = _uuid(f"quran-l2-{cid}-{(t['start_ayah_number'] - 1) // QURAN_L2_AYAH_GROUP_SIZE * QURAN_L2_AYAH_GROUP_SIZE + 1}-{min((t['start_ayah_number'] - 1) // QURAN_L2_AYAH_GROUP_SIZE * QURAN_L2_AYAH_GROUP_SIZE + QURAN_L2_AYAH_GROUP_SIZE, ch['verses_count'])}")

            all_segments.append(_make_segment(
                l3_id, doc_id, t["start_ayah_number"], l3_text, level=3, parent_id=parent_l2,
                source_type="quran",
                citation=f"Quran {ref} - Saheeh International",
                source_ref={"chapter_id": cid, "surah": cid, "verse_start": t["start_ayah_number"], "verse_end": t["end_ayah_number"], "authority_rank": 1},
                section_header=f"Surah {surah_name}",
            ))

    l1_count = sum(1 for s in all_segments if s["level"] == 1)
    l2_count = sum(1 for s in all_segments if s["level"] == 2)
    l3_count = sum(1 for s in all_segments if s["level"] == 3)
    logger.info("Quran: L1=%d L2=%d L3=%d total=%d", l1_count, l2_count, l3_count, len(all_segments))

    count = await _embed_store(db, vs, embedder, collection, all_segments, dry_run)
    return {"l1": l1_count, "l2": l2_count, "l3": l3_count, "total": count}


# ===========================================================================
# HADITH: L1 (collection summary) → L2 (book/chapter) → L3 (individual hadith)
# ===========================================================================
async def project_hadith(db, vs, embedder, collection, gw_url, api_key, colls, dry_run):
    logger.info("=== Hadith: hierarchical L1/L2/L3 ===")

    client = httpx.Client(
        base_url=gw_url, headers={"X-API-Key": api_key},
        timeout=httpx.Timeout(120.0, connect=10.0),
        transport=httpx.HTTPTransport(retries=3),
    )
    try:
        all_colls = client.get("/api/v1/islamic/hadith/collections").json().get("collections", [])
        coll_map = {c["name"]: c for c in all_colls}

        all_segments = []

        for coll_name in colls:
            coll = coll_map.get(coll_name)
            if not coll:
                continue
            title = coll.get("title", coll_name.title())
            doc_id = f"hadith-{coll_name}"
            await _ensure_doc(db, doc_id, f"Hadith - {title}")

            books = client.get(f"/api/v1/islamic/hadith/collections/{coll_name}/books").json().get("books", [])

            # --- L1: Collection summary ---
            l1_id = _uuid(f"hadith-l1-{coll_name}")
            l1_text = f"{title}\n\nA collection of {len(books)} books of authenticated Hadith narrations."
            all_segments.append(_make_segment(
                l1_id, doc_id, 0, l1_text, level=1, parent_id=None,
                source_type="hadith",
                citation=title,
                source_ref={"collection": coll_name, "authority_rank": 2},
                section_header=title,
                summary=f"{title} - {len(books)} books",
            ))

            position_l3 = 0
            for book in books:
                book_num = book.get("book_number", "0")
                book_title = book.get("title", f"Book {book_num}")
                n = book.get("number_of_hadith") or 0
                if n == 0:
                    continue

                # Fetch all hadiths in this book
                book_hadiths = []
                page = 1
                while True:
                    try:
                        resp = client.get(
                            f"/api/v1/islamic/hadith/collections/{coll_name}/books/{book_num}/hadiths",
                            params={"page": page, "limit": 50},
                        )
                        data = resp.json() if resp.status_code == 200 else {}
                    except Exception as exc:
                        logger.warning("Book items fetch failed %s/book%s/page%d: %s", coll_name, book_num, page, exc)
                        break
                    items = data.get("items", [])
                    if not items:
                        break
                    book_hadiths.extend(items)
                    if page >= data.get("pagination", {}).get("total_pages", 1):
                        break
                    page += 1

                if not book_hadiths:
                    continue

                # --- L2: Book/chapter grouping ---
                l2_id = _uuid(f"hadith-l2-{coll_name}-book{book_num}")
                l2_parts = [f"{title}, Book {book_num}: {book_title}\n"]
                for h in book_hadiths[:8]:  # Sample first 8 for L2 summary
                    hnum = h.get("hadith_number", "")
                    preview = h.get("preview_text", "")[:200]
                    l2_parts.append(f"Hadith {hnum}: {preview}")
                if len(book_hadiths) > 8:
                    l2_parts.append(f"... ({len(book_hadiths) - 8} more hadiths)")
                l2_text = "\n".join(l2_parts)

                all_segments.append(_make_segment(
                    l2_id, doc_id, 1_000_000 + int(book_num or 0), l2_text, level=2, parent_id=l1_id,
                    source_type="hadith",
                    citation=f"{title}, Book {book_num}",
                    source_ref={"collection": coll_name, "book_number": book_num, "authority_rank": 2},
                    section_header=f"{title} - Book {book_num}",
                ))

                # --- L3: Individual hadith (full text via detail endpoint) ---
                for h in book_hadiths:
                    hnum = str(h.get("hadith_number", ""))
                    try:
                        resp = client.get(f"/api/v1/islamic/hadith/collections/{coll_name}/hadiths/{hnum}")
                        detail = resp.json().get("hadith", {}) if resp.status_code == 200 else h
                    except Exception as exc:
                        logger.debug("Detail fetch failed %s/%s: %s", coll_name, hnum, exc)
                        detail = h

                    en = detail.get("translation_text") or h.get("preview_text", "") or ""
                    ar = detail.get("arabic_text") or h.get("arabic_preview_text", "") or ""
                    if not en and not ar:
                        continue

                    l3_parts = [f"{title}, Book {book_num}, Hadith {hnum}\n"]
                    if ar:
                        l3_parts.append(f"Arabic:\n{ar}\n")
                    if en:
                        l3_parts.append(f"Translation:\n{en}")
                    l3_text = "\n".join(l3_parts)

                    position_l3 += 1
                    all_segments.append(_make_segment(
                        _uuid(f"hadith-l3-{coll_name}-{hnum}"), doc_id, position_l3, l3_text,
                        level=3, parent_id=l2_id,
                        source_type="hadith",
                        citation=f"{title}, Book {book_num}, Hadith {hnum}",
                        source_ref={"collection": coll_name, "book_number": book_num, "hadith_number": hnum, "authority_rank": 2},
                        section_header=f"{title} - Book {book_num}",
                    ))

            logger.info("  %s: %d hadiths", coll_name, position_l3)

        l1 = sum(1 for s in all_segments if s["level"] == 1)
        l2 = sum(1 for s in all_segments if s["level"] == 2)
        l3 = sum(1 for s in all_segments if s["level"] == 3)
        logger.info("Hadith: L1=%d L2=%d L3=%d total=%d", l1, l2, l3, len(all_segments))
        count = await _embed_store(db, vs, embedder, collection, all_segments, dry_run)
        return {"l1": l1, "l2": l2, "l3": l3, "total": count}
    finally:
        client.close()


# ===========================================================================
# DUA: L1 (overview) → L2 (category) → L3 (individual dua)
# ===========================================================================
async def project_dua(db, vs, embedder, collection, dry_run):
    logger.info("=== Dua: hierarchical L1/L2/L3 ===")

    categories = await _fetch(db, "SELECT * FROM dua_categories ORDER BY category")
    duas = await _fetch(db, "SELECT * FROM dua_items ORDER BY category, dua_id")
    if not duas:
        return {"status": "empty"}

    doc_id = "dua-all"
    await _ensure_doc(db, doc_id, "Islamic Dua and Adhkar")

    # Group duas by category
    cat_duas: dict[str, list[dict]] = {}
    for d in duas:
        cat_duas.setdefault(d["category"], []).append(d)

    all_segments = []

    # --- L1: Overview ---
    l1_id = _uuid("dua-l1-overview")
    l1_text = f"Islamic Dua and Adhkar Collection\n\n{len(categories)} categories, {len(duas)} verified duas.\n\nCategories:\n"
    l1_text += "\n".join(f"- {c['category']} ({c['dua_count']} duas)" for c in categories)
    all_segments.append(_make_segment(
        l1_id, doc_id, 0, l1_text, level=1, parent_id=None,
        source_type="general_islamic", citation="Islamic Dua and Adhkar Collection",
        source_ref={"authority_rank": 3}, section_header="Dua Collection",
        summary=f"{len(duas)} verified duas across {len(categories)} categories",
    ))

    # --- L2: Category grouping ---
    cat_position = 0
    for cat, cat_items in cat_duas.items():
        cat_position += 1
        l2_id = _uuid(f"dua-l2-{cat}")
        l2_parts = [f"Dua Category: {cat}\n"]
        for d in cat_items:
            l2_parts.append(f"- {d['title']}: {d['arabic_text'][:80]}...")
            l2_parts.append(f"  Translation: {d['english_meaning'][:100]}...")
            if d.get("source"):
                l2_parts.append(f"  Source: {d['source']} {d.get('reference', '')}")
            l2_parts.append("")
        l2_text = "\n".join(l2_parts)

        all_segments.append(_make_segment(
            l2_id, doc_id, 1_000_000 + cat_position, l2_text, level=2, parent_id=l1_id,
            source_type="general_islamic", citation=f"Dua - {cat}",
            source_ref={"category": cat, "authority_rank": 3}, section_header=cat,
        ))

        # --- L3: Individual dua ---
        for i, dua in enumerate(cat_items, 1):
            l3_parts = [f"Dua: {dua['title']}\nCategory: {cat}\n"]
            l3_parts.append(f"Arabic:\n{dua['arabic_text']}\n")
            if dua.get("transliteration"):
                l3_parts.append(f"Transliteration:\n{dua['transliteration']}\n")
            l3_parts.append(f"Translation:\n{dua['english_meaning']}")
            if dua.get("occasion"):
                l3_parts.append(f"\nOccasion: {dua['occasion']}")
            if dua.get("source") and dua.get("reference"):
                l3_parts.append(f"\nSource: {dua['source']}, {dua['reference']}")
            if dua.get("authenticity"):
                l3_parts.append(f"Authenticity: {dua['authenticity']}")
            l3_text = "\n".join(l3_parts)

            citation = f"Dua: {dua['title']}"
            if dua.get("source"):
                citation += f" - {dua['source']} {dua.get('reference', '')}"

            all_segments.append(_make_segment(
                _uuid(f"dua-l3-{dua['dua_id']}"), doc_id, cat_position * 1000 + i, l3_text,
                level=3, parent_id=l2_id,
                source_type="general_islamic", citation=citation,
                source_ref={"dua_id": dua["dua_id"], "category": cat, "source": dua.get("source", ""), "reference": dua.get("reference", ""), "authority_rank": 3},
                section_header=cat,
            ))

    l1 = sum(1 for s in all_segments if s["level"] == 1)
    l2 = sum(1 for s in all_segments if s["level"] == 2)
    l3 = sum(1 for s in all_segments if s["level"] == 3)
    logger.info("Dua: L1=%d L2=%d L3=%d total=%d", l1, l2, l3, len(all_segments))
    count = await _embed_store(db, vs, embedder, collection, all_segments, dry_run)
    return {"l1": l1, "l2": l2, "l3": l3, "total": count}


# ===========================================================================
# Main
# ===========================================================================
async def run(args):
    settings = Settings()
    db = DatabaseStorage(dsn=settings.database.dsn, enabled=True, auto_init=False)
    await db.connect()

    vs = VectorStore(
        url=str(settings.knowledge.qdrant.url),
        api_key=settings.knowledge.qdrant.api_key or None,
        timeout_seconds=settings.knowledge.qdrant.timeout_seconds,
        max_retries=settings.knowledge.qdrant.max_retries,
        retry_base_delay=settings.knowledge.qdrant.retry_base_delay,
    )

    econf = EmbeddingConfig(
        provider="gemini", model=settings.knowledge.gemini.model or EMBEDDING_MODEL,
        api_key=settings.knowledge.gemini.api_key,
        base_url=settings.knowledge.gemini.base_url or None,
        timeout_seconds=settings.knowledge.gemini.timeout_seconds,
    )
    embedder = create_embedding(econf, dimension=EMBEDDING_DIM)

    try:
        coll = vs.make_collection_name(DATASET_ID, EMBEDDING_DIM)
        await db.save_dataset({
            "dataset_id": DATASET_ID, "name": "Islamic Knowledge",
            "description": "Unified Islamic KB with hierarchical indexing (L1/L2/L3): Quran, Hadith, Dua",
            "tenant_id": TENANT_ID, "visibility": "public",
            "embedding_provider": "gemini", "embedding_model": EMBEDDING_MODEL,
            "embedding_dimension": EMBEDDING_DIM, "collection_name": coll,
            "created_by": OWNER_ID,
        })
        actual = await vs.ensure_collection(DATASET_ID, EMBEDDING_DIM, coll)
        logger.info("Dataset %s -> collection %s", DATASET_ID, actual)

        sources = [s.strip() for s in args.source.split(",")]
        results = {}

        if "quran" in sources or "all" in sources:
            results["quran"] = await project_quran(db, vs, embedder, actual, args.dry_run)
        if "dua" in sources or "all" in sources:
            results["dua"] = await project_dua(db, vs, embedder, actual, args.dry_run)
        if "hadith" in sources or "all" in sources:
            hadith_colls = [c.strip() for c in args.hadith_collections.split(",") if c.strip()]
            results["hadith"] = await project_hadith(db, vs, embedder, actual, args.gateway_url, args.api_key, hadith_colls, args.dry_run)

        print(json.dumps(results, indent=2, ensure_ascii=False))
        return 0
    finally:
        await embedder.close()
        await vs.close()
        await db.close()


def main():
    logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(name)s - %(message)s")
    p = argparse.ArgumentParser()
    p.add_argument("--source", default="all")
    p.add_argument("--hadith-collections", default=",".join(DEFAULT_HADITH_COLLECTIONS))
    p.add_argument("--gateway-url", default="http://127.0.0.1:8080")
    p.add_argument("--api-key", default=os.getenv("GATEWAY_API_KEY"))
    p.add_argument("--dry-run", action="store_true")
    args = p.parse_args()
    if not args.api_key:
        p.error("--api-key or GATEWAY_API_KEY is required")
    raise SystemExit(asyncio.run(run(args)))


if __name__ == "__main__":
    main()
