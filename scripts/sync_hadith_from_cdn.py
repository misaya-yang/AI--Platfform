#!/usr/bin/env python3
"""Bulk-download Hadith from CDN and embed into unified KB.

Downloads entire collections in ONE request each (not per-hadith).
Then embeds with hierarchical L1/L2/L3 indexing.

Usage:
    conda run -n ai_gateway python scripts/sync_hadith_from_cdn.py
    conda run -n ai_gateway python scripts/sync_hadith_from_cdn.py --collections bukhari,muslim
"""
from __future__ import annotations

import argparse
import asyncio
import hashlib
import json
import logging
import sys
import uuid
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

logger = logging.getLogger("hadith_cdn_sync")

CDN_BASE = "https://cdn.jsdelivr.net/gh/fawazahmed0/hadith-api@1"
COLLECTIONS = ["bukhari", "muslim", "abudawud", "tirmidhi", "nasai", "ibnmajah", "malik", "nawawi", "qudsi", "dehlawi"]
COLLECTION_TITLES = {
    "bukhari": "Sahih al-Bukhari", "muslim": "Sahih Muslim",
    "abudawud": "Sunan Abu Dawud", "tirmidhi": "Jami at-Tirmidhi",
    "nasai": "Sunan an-Nasai", "ibnmajah": "Sunan Ibn Majah",
    "malik": "Muwatta Malik", "nawawi": "Forty Hadith an-Nawawi",
    "qudsi": "Forty Hadith Qudsi", "dehlawi": "Forty Hadith Shah Waliullah Dehlawi",
}

DATASET_ID = "islamic-knowledge"
TENANT_ID = "islamic-content"
OWNER_ID = "system-projector"
EMBEDDING_DIM = 1024
BATCH_EMBED = 20
BATCH_QDRANT = 32
_NS = uuid.UUID("a1b2c3d4-e5f6-7890-abcd-ef1234567890")


def _uuid(key): return str(uuid.uuid5(_NS, key))
def _sha256(text): return hashlib.sha256(text.encode()).hexdigest()


async def _embed_store(db, vs, embedder, collection, segments, dry_run=False):
    from qdrant_client.http import models as qmodels
    if dry_run:
        logger.info("[DRY-RUN] %d segments", len(segments))
        return len(segments)
    stored = 0
    for i in range(0, len(segments), BATCH_EMBED):
        batch = segments[i:i + BATCH_EMBED]
        try:
            vectors = await embedder.embed_documents([s["text"] for s in batch])
        except Exception as exc:
            logger.error("Embed failed batch %d: %s", i, exc)
            continue
        points = [
            qmodels.PointStruct(
                id=seg["segment_id"], vector=vec,
                payload={
                    "dataset_id": DATASET_ID, "document_id": seg["document_id"],
                    "segment_id": seg["segment_id"], "tenant_id": TENANT_ID,
                    "text": seg["text"][:1000], "source_type": "hadith",
                    "citation_text": seg.get("citation_text", ""),
                    "language": "ar_en", "section_title": seg.get("section_header", ""),
                    "level": seg.get("level", 3),
                },
            )
            for seg, vec in zip(batch, vectors)
        ]
        for j in range(0, len(points), BATCH_QDRANT):
            try:
                await vs.upsert(collection, points[j:j + BATCH_QDRANT])
            except Exception as exc:
                logger.error("Qdrant upsert: %s", exc)
        for seg in batch:
            seg["vector_id"] = seg["segment_id"]
            seg["status"] = "completed"
        try:
            await db.insert_segments(batch)
        except Exception as exc:
            logger.error("DB insert: %s", exc)
        stored += len(batch)
        if stored % 500 == 0 or stored == len(segments):
            logger.info("  embed progress: %d / %d", stored, len(segments))
        await asyncio.sleep(0.05)
    return stored


def _make_seg(seg_id, doc_id, pos, text, level, parent_id, citation, source_ref, section):
    return {
        "segment_id": seg_id, "dataset_id": DATASET_ID, "document_id": doc_id,
        "position": pos, "level": level, "parent_segment_id": parent_id,
        "summary": None, "text": text,
        "token_count": len(text.split()), "word_count": len(text.split()),
        "content_hash": _sha256(text), "source_type": "hadith",
        "citation_text": citation,
        "source_reference": json.dumps(source_ref) if isinstance(source_ref, dict) else source_ref,
        "language": "ar_en", "section_header": section, "contextual_prefix": "",
        "metadata": json.dumps({"source_type": "hadith", "authority_rank": 2}),
        "enabled": True, "created_by": OWNER_ID,
    }


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
        provider="gemini", model=settings.knowledge.gemini.model or "gemini-embedding-001",
        api_key=settings.knowledge.gemini.api_key,
        base_url=settings.knowledge.gemini.base_url or None,
        timeout_seconds=settings.knowledge.gemini.timeout_seconds,
    )
    embedder = create_embedding(econf, dimension=EMBEDDING_DIM)

    try:
        qdrant_coll = vs.make_collection_name(DATASET_ID, EMBEDDING_DIM)
        actual = await vs.ensure_collection(DATASET_ID, EMBEDDING_DIM, qdrant_coll)
        logger.info("Using collection: %s", actual)

        colls = [c.strip() for c in args.collections.split(",") if c.strip()]
        client = httpx.Client(timeout=120.0, transport=httpx.HTTPTransport(retries=3))
        total_segments = 0

        for coll_name in colls:
            title = COLLECTION_TITLES.get(coll_name, coll_name.title())
            logger.info("=== %s ===", title)

            # Download English + Arabic in bulk (2 requests total per collection)
            try:
                eng_data = client.get(f"{CDN_BASE}/editions/eng-{coll_name}.min.json").json()
                eng_hadiths = eng_data.get("hadiths", [])
            except Exception as exc:
                logger.error("Failed to download eng-%s: %s", coll_name, exc)
                continue

            ara_hadiths = {}
            try:
                ara_data = client.get(f"{CDN_BASE}/editions/ara-{coll_name}.min.json").json()
                for h in ara_data.get("hadiths", []):
                    ara_hadiths[h.get("hadithnumber")] = h.get("text", "")
            except Exception:
                logger.warning("Arabic edition not available for %s", coll_name)

            logger.info("  Downloaded: %d English + %d Arabic hadiths", len(eng_hadiths), len(ara_hadiths))

            # Create document
            doc_id = f"hadith-{coll_name}"
            from datetime import datetime, timezone
            await db.save_document({
                "document_id": doc_id, "dataset_id": DATASET_ID,
                "title": f"Hadith - {title}", "source_type": "islamic_projection",
                "status": "completed", "progress": 100.0,
                "completed_at": datetime.now(timezone.utc),
            })

            # Group hadiths by reference.book (section)
            books: dict[int, list[dict]] = {}
            for h in eng_hadiths:
                ref = h.get("reference", {})
                book_num = ref.get("book", 0) if isinstance(ref, dict) else 0
                books.setdefault(book_num, []).append(h)

            segments = []

            # L1: Collection summary
            l1_id = _uuid(f"hadith-l1-{coll_name}")
            l1_text = f"{title}\n\n{len(eng_hadiths)} authenticated hadith narrations across {len(books)} books.\n"
            l1_text += f"This is one of the most important collections in Islamic scholarship."
            segments.append(_make_seg(
                l1_id, doc_id, 0, l1_text, 1, None,
                title, {"collection": coll_name, "authority_rank": 2},
                title,
            ))

            # L2: Book groupings (~1500 tokens each, multiple hadiths)
            # L3: Individual hadiths
            l3_pos = 0
            for book_num, book_items in sorted(books.items()):
                l2_id = _uuid(f"hadith-l2-{coll_name}-b{book_num}")

                # L2: group hadiths in this book (summary)
                l2_parts = [f"{title}, Book {book_num}\n"]
                for h in book_items[:6]:
                    hnum = h.get("hadithnumber", "")
                    l2_parts.append(f"Hadith {hnum}: {h.get('text', '')[:200]}...")
                if len(book_items) > 6:
                    l2_parts.append(f"... ({len(book_items) - 6} more hadiths)")
                l2_text = "\n".join(l2_parts)

                segments.append(_make_seg(
                    l2_id, doc_id, 1_000_000 + book_num, l2_text, 2, l1_id,
                    f"{title}, Book {book_num}",
                    {"collection": coll_name, "book_number": book_num, "authority_rank": 2},
                    f"{title} - Book {book_num}",
                ))

                # L3: individual hadiths (full text)
                for h in book_items:
                    hnum = h.get("hadithnumber", "")
                    en_text = h.get("text", "")
                    ar_text = ara_hadiths.get(hnum, "")

                    if not en_text and not ar_text:
                        continue

                    l3_parts = [f"{title}, Book {book_num}, Hadith {hnum}\n"]
                    if ar_text:
                        l3_parts.append(f"Arabic:\n{ar_text}\n")
                    l3_parts.append(f"Translation:\n{en_text}")
                    l3_text = "\n".join(l3_parts)

                    l3_pos += 1
                    segments.append(_make_seg(
                        _uuid(f"hadith-l3-{coll_name}-{hnum}"), doc_id, l3_pos, l3_text,
                        3, l2_id,
                        f"{title}, Book {book_num}, Hadith {hnum}",
                        {"collection": coll_name, "book_number": book_num, "hadith_number": str(hnum), "authority_rank": 2},
                        f"{title} - Book {book_num}",
                    ))

            l1c = sum(1 for s in segments if s["level"] == 1)
            l2c = sum(1 for s in segments if s["level"] == 2)
            l3c = sum(1 for s in segments if s["level"] == 3)
            logger.info("  %s: L1=%d L2=%d L3=%d total=%d", coll_name, l1c, l2c, l3c, len(segments))

            count = await _embed_store(db, vs, embedder, actual, segments, args.dry_run)
            total_segments += count

        client.close()
        logger.info("=== ALL DONE: %d total hadith segments embedded ===", total_segments)
        print(json.dumps({"total_segments": total_segments}))
        return 0
    finally:
        await embedder.close()
        await vs.close()
        await db.close()


def main():
    logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(name)s - %(message)s")
    p = argparse.ArgumentParser()
    p.add_argument("--collections", default=",".join(COLLECTIONS))
    p.add_argument("--dry-run", action="store_true")
    raise SystemExit(asyncio.run(run(p.parse_args())))


if __name__ == "__main__":
    main()
