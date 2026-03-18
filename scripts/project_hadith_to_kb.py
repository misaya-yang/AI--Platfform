#!/usr/bin/env python3
"""Project Hadith data from gateway API into Knowledge Base.

Fetches Hadith data from the gateway's live CDN-backed API and
embeds it into the KB system with proper Islamic metadata.

Usage:
    conda run -n ai_gateway python scripts/project_hadith_to_kb.py
    conda run -n ai_gateway python scripts/project_hadith_to_kb.py --collections bukhari,muslim
    conda run -n ai_gateway python scripts/project_hadith_to_kb.py --dry-run
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

logger = logging.getLogger("hadith_kb_projection")

# ---------------------------------------------------------------------------
# Constants
# ---------------------------------------------------------------------------
TENANT_ID = "islamic-content"
OWNER_ID = "system-projector"
DATASET_ID = "islamic-hadith-canonical"
EMBEDDING_PROVIDER = "gemini"
EMBEDDING_MODEL = "gemini-embedding-001"
EMBEDDING_DIM = 1024
BATCH_EMBED = 20
BATCH_QDRANT = 32

_NS = uuid.UUID("a1b2c3d4-e5f6-7890-abcd-ef1234567890")

# Priority collections for KB embedding (most authentic first)
DEFAULT_COLLECTIONS = ["bukhari", "muslim", "abudawud", "tirmidhi", "nasai", "ibnmajah", "malik", "nawawi", "qudsi", "dehlawi"]


def _uuid(key: str) -> str:
    return str(uuid.uuid5(_NS, key))


def _sha256(text: str) -> str:
    return hashlib.sha256(text.encode()).hexdigest()


# ---------------------------------------------------------------------------
# Gateway API client
# ---------------------------------------------------------------------------
class GatewayHadithClient:
    def __init__(self, base_url: str, api_key: str):
        self.client = httpx.Client(
            base_url=base_url,
            headers={"X-API-Key": api_key},
            timeout=30.0,
        )

    def get_collections(self) -> list[dict]:
        r = self.client.get("/api/v1/islamic/hadith/collections")
        r.raise_for_status()
        return r.json().get("collections", [])

    def get_books(self, collection: str) -> list[dict]:
        r = self.client.get(f"/api/v1/islamic/hadith/collections/{collection}/books")
        r.raise_for_status()
        return r.json().get("books", [])

    def get_book_items(self, collection: str, book_number: str, page: int = 1, limit: int = 50) -> dict:
        r = self.client.get(
            f"/api/v1/islamic/hadith/collections/{collection}/books/{book_number}/hadiths",
            params={"page": page, "limit": limit},
        )
        r.raise_for_status()
        return r.json()

    def get_detail(self, collection: str, hadith_number: str) -> dict:
        r = self.client.get(
            f"/api/v1/islamic/hadith/collections/{collection}/hadiths/{hadith_number}"
        )
        r.raise_for_status()
        return r.json().get("hadith", r.json())

    def close(self):
        self.client.close()


# ---------------------------------------------------------------------------
# Embedding + storage (reuse from project_islamic_to_kb.py)
# ---------------------------------------------------------------------------
async def embed_and_store(db, vs, embedder, collection, segments, dry_run=False):
    from qdrant_client.http import models as qmodels

    if dry_run:
        logger.info("[DRY-RUN] Would process %d segments", len(segments))
        return len(segments)

    stored = 0
    for i in range(0, len(segments), BATCH_EMBED):
        batch = segments[i : i + BATCH_EMBED]
        texts = [s["text"] for s in batch]

        try:
            vectors = await embedder.embed_documents(texts)
        except Exception as exc:
            logger.error("Embedding failed at batch %d: %s", i, exc)
            continue

        points = []
        for seg, vec in zip(batch, vectors):
            payload = {
                "dataset_id": seg["dataset_id"],
                "document_id": seg["document_id"],
                "segment_id": seg["segment_id"],
                "tenant_id": TENANT_ID,
                "text": seg["text"][:500],
                "source_type": seg.get("source_type", "hadith"),
                "citation_text": seg.get("citation_text", ""),
                "language": seg.get("language", "ar_en"),
                "section_title": seg.get("section_header", ""),
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


# ---------------------------------------------------------------------------
# Main projection
# ---------------------------------------------------------------------------
async def project_hadith(
    db: DatabaseStorage,
    vs: VectorStore,
    embedder,
    qdrant_collection: str,
    gw: GatewayHadithClient,
    collections_to_sync: list[str],
    dry_run: bool = False,
) -> dict[str, Any]:
    logger.info("=== Projecting Hadith ===")

    all_collections = gw.get_collections()
    coll_map = {c["name"]: c for c in all_collections}
    logger.info("Available: %s", [c["name"] for c in all_collections])

    segments = []
    total_fetched = 0

    for coll_name in collections_to_sync:
        coll = coll_map.get(coll_name)
        if not coll:
            logger.warning("Collection %s not found, skipping", coll_name)
            continue

        title = coll.get("title", coll_name.title())
        doc_id = f"hadith-{coll_name}"

        await db.save_document({
            "document_id": doc_id,
            "dataset_id": DATASET_ID,
            "title": f"Hadith - {title}",
            "source_type": "islamic_projection",
            "status": "completed",
            "progress": 100.0,
        })

        # Fetch all books for this collection
        books = gw.get_books(coll_name)
        logger.info("Collection %s: %d books", coll_name, len(books))

        position = 0
        for book in books:
            book_num = book.get("book_number", "0")
            book_title = book.get("title", f"Book {book_num}")
            n_hadith = book.get("number_of_hadith") or 0

            if n_hadith == 0:
                continue

            # Fetch hadiths in this book (paginated)
            page = 1
            while True:
                data = gw.get_book_items(coll_name, book_num, page=page, limit=50)
                items = data.get("items", [])
                if not items:
                    break

                for h in items:
                    hadith_num = str(h.get("hadith_number", ""))
                    en_text = h.get("preview_text", "") or ""
                    ar_text = h.get("arabic_preview_text", "") or ""

                    if not en_text and not ar_text:
                        continue

                    # Build segment text
                    text_parts = [f"{title}, Book {book_num}, Hadith {hadith_num}"]
                    if book_title and book_title != f"Book {book_num}":
                        text_parts.append(f"Chapter: {book_title}")
                    if ar_text:
                        text_parts.append(f"Arabic: {ar_text}")
                    if en_text:
                        text_parts.append(f"Translation: {en_text}")
                    text = "\n".join(text_parts)

                    citation = f"{title}, Book {book_num}, Hadith {hadith_num}"
                    position += 1

                    segments.append({
                        "segment_id": _uuid(f"hadith-{coll_name}-{hadith_num}"),
                        "dataset_id": DATASET_ID,
                        "document_id": doc_id,
                        "position": position,
                        "text": text,
                        "token_count": len(text.split()),
                        "word_count": len(text.split()),
                        "content_hash": _sha256(text),
                        "source_type": "hadith",
                        "citation_text": citation,
                        "source_reference": json.dumps({
                            "collection": coll_name,
                            "book_number": book_num,
                            "hadith_number": hadith_num,
                            "authority_rank": 2,
                        }),
                        "language": "ar_en",
                        "section_header": book_title,
                        "contextual_prefix": f"Hadith - {title} - Book {book_num}",
                        "metadata": json.dumps({
                            "source_type": "hadith",
                            "authority_rank": 2,
                            "collection": coll_name,
                        }),
                        "enabled": True,
                        "created_by": OWNER_ID,
                    })

                pagination = data.get("pagination", {})
                total_pages = pagination.get("total_pages", 1)
                if page >= total_pages:
                    break
                page += 1

        total_fetched += position
        logger.info("  %s: %d hadiths fetched", coll_name, position)

    logger.info("Total segments to embed: %d", len(segments))
    count = await embed_and_store(db, vs, embedder, qdrant_collection, segments, dry_run)
    return {"collections": len(collections_to_sync), "hadiths": count}


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
        provider="gemini",
        model=settings.knowledge.gemini.model or EMBEDDING_MODEL,
        api_key=settings.knowledge.gemini.api_key,
        base_url=settings.knowledge.gemini.base_url or None,
        timeout_seconds=settings.knowledge.gemini.timeout_seconds,
    )
    embedder = create_embedding(econf, dimension=EMBEDDING_DIM)

    gw = GatewayHadithClient(
        base_url=args.gateway_url,
        api_key=args.api_key,
    )

    try:
        # Create dataset
        coll_name_qdrant = vs.make_collection_name(DATASET_ID, EMBEDDING_DIM)
        await db.save_dataset({
            "dataset_id": DATASET_ID,
            "name": "Islamic Hadith",
            "description": "Hadith collections (Bukhari, Muslim, Abu Dawud, Tirmidhi, etc.) with Arabic and English text",
            "tenant_id": TENANT_ID,
            "visibility": "public",
            "embedding_provider": EMBEDDING_PROVIDER,
            "embedding_model": EMBEDDING_MODEL,
            "embedding_dimension": EMBEDDING_DIM,
            "collection_name": coll_name_qdrant,
            "created_by": OWNER_ID,
        })
        actual = await vs.ensure_collection(DATASET_ID, EMBEDDING_DIM, coll_name_qdrant)
        logger.info("Dataset %s -> collection %s", DATASET_ID, actual)

        colls = [c.strip() for c in args.collections.split(",") if c.strip()]
        result = await project_hadith(db, vs, embedder, actual, gw, colls, args.dry_run)
        print(json.dumps(result, indent=2))
        return 0
    finally:
        gw.close()
        await embedder.close()
        await vs.close()
        await db.close()


def main():
    logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(name)s - %(message)s")
    parser = argparse.ArgumentParser(description="Project Hadith to KB")
    parser.add_argument("--gateway-url", default="http://127.0.0.1:8080")
    parser.add_argument("--api-key", default="gw_gEtIPdAxdXI4D-WyWxvgFNPkdd7CU2VPdeFg9XdqFhs")
    parser.add_argument("--collections", default=",".join(DEFAULT_COLLECTIONS))
    parser.add_argument("--dry-run", action="store_true")
    raise SystemExit(asyncio.run(run(parser.parse_args())))


if __name__ == "__main__":
    main()
