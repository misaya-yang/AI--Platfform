#!/usr/bin/env python3
"""Re-index KB collection with contextual prefix + new embedding model.

Reads all points from the source collection, generates contextual prefix
per chunk (Anthropic approach), re-embeds with Gemini Embedding 2, generates
BM25 sparse vectors, and writes to a new collection. Then swaps via alias.

Usage:
    python3 scripts/reindex_with_context.py \
        --collection kb_imam_v2_1024 \
        --model gemini-embedding-2-preview \
        --batch-size 50 \
        --concurrency 5
"""

from __future__ import annotations

import argparse
import asyncio
import hashlib
import logging
import math
import os
import re
import sys
import time
from typing import Any

import httpx

logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(message)s")
logger = logging.getLogger(__name__)

QDRANT_URL = os.getenv("QDRANT_URL", "http://localhost:6333")
GOOGLE_API_KEY = os.getenv("GOOGLE_API_KEY", "")
GEMINI_BASE = "https://generativelanguage.googleapis.com/v1beta"

# ── Inline tokenizer (same as retrieval.py) ──────────────────────────────────

_RE_LATIN_WORD = re.compile(r"[A-Za-z0-9\-_]+")
_RE_CJK_RUN = re.compile(r"[\u4e00-\u9fff]+")
_RE_ARABIC_RUN = re.compile(
    r"[\u0600-\u06ff\u0750-\u077f\u08a0-\u08ff\ufb50-\ufdff\ufe70-\ufeff]+"
)
_RE_ARABIC_DIACRITICS = re.compile(r"[\u064b-\u0652\u0670]")
_RE_ALEF = re.compile(r"[أإآٱ]")

SURAH_NAMES = {
    1: "Al-Fatiha", 2: "Al-Baqarah", 3: "Ali 'Imran", 4: "An-Nisa", 5: "Al-Ma'idah",
    6: "Al-An'am", 7: "Al-A'raf", 8: "Al-Anfal", 9: "At-Tawbah", 10: "Yunus",
    11: "Hud", 12: "Yusuf", 13: "Ar-Ra'd", 14: "Ibrahim", 15: "Al-Hijr",
    16: "An-Nahl", 17: "Al-Isra", 18: "Al-Kahf", 19: "Maryam", 20: "Taha",
    21: "Al-Anbiya", 22: "Al-Hajj", 23: "Al-Mu'minun", 24: "An-Nur", 25: "Al-Furqan",
    36: "Ya-Sin", 55: "Ar-Rahman", 56: "Al-Waqi'ah", 67: "Al-Mulk", 112: "Al-Ikhlas",
    113: "Al-Falaq", 114: "An-Nas",
}


def _normalize_arabic(text: str) -> str:
    t = _RE_ARABIC_DIACRITICS.sub("", text)
    t = _RE_ALEF.sub("ا", t)
    return t.replace("ة", "ه").replace("ى", "ي")


def tokenize(text: str) -> list[str]:
    t = (text or "").strip()
    if not t:
        return []
    tokens, t_clean = [], t.lower()
    tokens.extend(_RE_LATIN_WORD.findall(t_clean))
    for run in _RE_CJK_RUN.findall(t_clean):
        if len(run) >= 2:
            tokens.append(run)
            for i in range(len(run) - 1):
                tokens.append(run[i : i + 2])
        tokens.extend(list(run))
    normalized = _normalize_arabic(t_clean)
    for run in _RE_ARABIC_RUN.findall(normalized):
        for word in run.split():
            word = word.strip()
            if not word or len(word) < 2:
                continue
            tokens.append(word)
            if len(word) > 3:
                for prefix in ["و", "ف", "ب", "ل", "ك", "ال", "وال", "فال", "بال", "لل"]:
                    if word.startswith(prefix):
                        stem = word[len(prefix):]
                        if len(stem) >= 2:
                            tokens.append(stem)
                        break
    seen = set()
    return [t for t in tokens if t and not (t in seen or seen.add(t))]


def _token_hash(token: str) -> int:
    h = 0x811C9DC5
    for b in token.encode("utf-8"):
        h ^= b
        h = (h * 0x01000193) & 0xFFFFFFFF
    return h


def text_to_sparse(text: str) -> tuple[list[int], list[float]]:
    tokens = tokenize(text)
    if not tokens:
        return [], []
    tf: dict[int, float] = {}
    for token in tokens:
        idx = _token_hash(token)
        tf[idx] = tf.get(idx, 0.0) + 1.0
    indices = sorted(tf.keys())
    return indices, [tf[i] for i in indices]


# ── Contextual prefix (same as contextual_retrieval.py) ──────────────────────

def generate_context_prefix(text: str, metadata: dict[str, Any]) -> str:
    """Generate contextual prefix for a chunk based on metadata."""
    source_type = metadata.get("source_type") or ""
    source_ref = metadata.get("source_reference") or {}
    doc_title = metadata.get("book_title") or metadata.get("document_title") or ""

    if source_type == "quran":
        # Try to extract from text if no source_ref
        surah = source_ref.get("surah") or metadata.get("chapter_id")
        verse = source_ref.get("verse_start") or metadata.get("ayah_number")
        surah_name = source_ref.get("surah_name") or metadata.get("chapter_name_en") or SURAH_NAMES.get(int(surah), "") if surah else ""
        if surah and verse:
            return f"This Quranic passage is from Surah {surah_name} ({surah}), verse {verse}. "
        return "This is a Quranic text. "

    elif source_type == "hadith":
        collection = source_ref.get("collection") or metadata.get("collection_name") or ""
        book = source_ref.get("book") or metadata.get("book_number") or ""
        narrator = source_ref.get("narrator") or ""
        parts = []
        if collection:
            parts.append(f"from {collection}")
        if book:
            parts.append(f"Book {book}")
        if narrator:
            parts.append(f"narrated by {narrator}")
        if parts:
            return f"This hadith {', '.join(parts)}. "
        return "This is a Hadith narration. "

    elif source_type == "tafseer":
        author = source_ref.get("author") or doc_title or ""
        if author:
            return f"This tafseer commentary from {author}. "
        return "This is Tafseer (Quran commentary). "

    elif source_type in ("fiqh", "general_islamic"):
        school = source_ref.get("school") or metadata.get("madhab") or ""
        if school:
            return f"This Islamic jurisprudence ruling from the {school.title()} school. "
        if doc_title:
            return f"This passage is from '{doc_title}'. "
        return "This is Islamic scholarly text. "

    # Fallback: use whatever title is available
    if doc_title:
        return f"From '{doc_title}'. "
    return ""


# ── Gemini Embedding ─────────────────────────────────────────────────────────

async def embed_batch(
    client: httpx.AsyncClient,
    texts: list[str],
    model: str,
    dimension: int,
    semaphore: asyncio.Semaphore,
) -> list[list[float]]:
    """Embed a batch of texts using Gemini API."""
    async with semaphore:
        requests = [
            {
                "model": f"models/{model}",
                "content": {"parts": [{"text": t[:8000]}]},
                "outputDimensionality": dimension,
                "taskType": "RETRIEVAL_DOCUMENT",
            }
            for t in texts
        ]
        resp = await client.post(
            f"{GEMINI_BASE}/models/{model}:batchEmbedContents?key={GOOGLE_API_KEY}",
            json={"requests": requests},
            timeout=60,
        )
        resp.raise_for_status()
        embeddings = resp.json().get("embeddings", [])
        return [e.get("values", []) for e in embeddings]


# ── Main reindex ─────────────────────────────────────────────────────────────

async def reindex(
    collection: str,
    model: str = "gemini-embedding-2-preview",
    dimension: int = 1024,
    batch_size: int = 50,
    concurrency: int = 5,
    qdrant_url: str = QDRANT_URL,
) -> None:
    from qdrant_client import AsyncQdrantClient
    from qdrant_client.http import models as qm

    qdrant = AsyncQdrantClient(url=qdrant_url, timeout=120)
    embed_sem = asyncio.Semaphore(concurrency)

    # 1. Get source info
    # Resolve alias: try to get the actual collection name
    try:
        aliases = await qdrant.get_collection_aliases(collection)
        actual_source = collection
    except Exception:
        actual_source = collection

    info = await qdrant.get_collection(actual_source)
    points_count = info.points_count
    logger.info(f"Source: {actual_source}, {points_count} points")

    # 2. Create new collection
    new_col = f"{collection}_ctx_{model.replace('-', '_')}"
    try:
        await qdrant.delete_collection(new_col)
    except Exception:
        pass

    await qdrant.create_collection(
        collection_name=new_col,
        vectors_config=qm.VectorParams(size=dimension, distance=qm.Distance.COSINE),
        sparse_vectors_config={"bm25": qm.SparseVectorParams(modifier=qm.Modifier.IDF)},
    )

    # Copy payload indexes
    old_indexes = getattr(info, "payload_schema", {}) or {}
    for field_name, schema_info in old_indexes.items():
        try:
            field_type = getattr(schema_info, "data_type", None)
            if field_type:
                schema_type = {
                    "keyword": qm.PayloadSchemaType.KEYWORD,
                    "integer": qm.PayloadSchemaType.INTEGER,
                }.get(str(field_type).lower(), qm.PayloadSchemaType.KEYWORD)
                await qdrant.create_payload_index(new_col, field_name, schema_type)
        except Exception:
            pass
    for fn in ("metadata.topic", "metadata.keywords"):
        try:
            await qdrant.create_payload_index(new_col, fn, qm.PayloadSchemaType.KEYWORD)
        except Exception:
            pass

    logger.info(f"Created {new_col} with {model} ({dimension}d) + BM25 sparse")

    # 3. Scroll, embed, upsert
    total = 0
    total_with_prefix = 0
    scroll_offset = None
    start_time = time.time()

    async with httpx.AsyncClient(timeout=60) as http_client:
        while True:
            results, next_offset = await qdrant.scroll(
                collection_name=actual_source,
                limit=batch_size,
                offset=scroll_offset,
                with_payload=True,
                with_vectors=False,  # Don't need old vectors
            )

            if not results:
                break

            # Prepare texts with contextual prefix
            embed_texts = []
            original_texts = []
            payloads = []
            point_ids = []
            sparse_vectors = []

            for p in results:
                payload = dict(p.payload or {})
                text = str(payload.get("text") or "")
                meta = payload.get("metadata") or {}

                # Merge top-level metadata into meta for prefix generation
                combined_meta = {**meta}
                for k in ("source_type", "citation_text", "source_reference",
                          "collection_name", "book_number", "hadith_number",
                          "chapter_id", "ayah_number", "chapter_name_en",
                          "book_title", "document_title", "madhab", "language"):
                    if k in payload and k not in combined_meta:
                        combined_meta[k] = payload[k]

                # Generate contextual prefix
                prefix = generate_context_prefix(text, combined_meta)
                embed_text = f"{prefix}{text}" if prefix else text
                if prefix:
                    total_with_prefix += 1

                embed_texts.append(embed_text)
                original_texts.append(text)
                payloads.append(payload)
                point_ids.append(p.id)

                # BM25 sparse vector (from original text, not prefix)
                si, sv = text_to_sparse(text)
                sparse_vectors.append((si, sv))

            # Embed batch
            try:
                embeddings = await embed_batch(
                    http_client, embed_texts, model, dimension, embed_sem,
                )
            except Exception as e:
                logger.error(f"Embedding failed at offset {total}: {e}")
                if next_offset is None:
                    break
                scroll_offset = next_offset
                continue

            # Build points
            points = []
            for i, (pid, payload, emb, (si, sv)) in enumerate(
                zip(point_ids, payloads, embeddings, sparse_vectors)
            ):
                if not emb:
                    continue
                vector: dict = {"": emb}
                if si:
                    vector["bm25"] = qm.SparseVector(indices=si, values=sv)
                points.append(qm.PointStruct(id=pid, vector=vector, payload=payload))

            # Upsert
            if points:
                await qdrant.upsert(collection_name=new_col, points=points)

            total += len(results)
            elapsed = time.time() - start_time
            rate = total / elapsed if elapsed > 0 else 0
            pct = total * 100 / max(points_count, 1)
            logger.info(
                f"  {total}/{points_count} ({pct:.1f}%) "
                f"[{rate:.0f} pts/s, prefix={total_with_prefix}]"
            )

            if next_offset is None:
                break
            scroll_offset = next_offset

    elapsed = time.time() - start_time
    logger.info(
        f"Re-index complete: {total} points, {total_with_prefix} with context prefix, "
        f"{elapsed:.0f}s ({total / max(elapsed, 1):.0f} pts/s), model={model}"
    )

    # 4. Verify
    new_info = await qdrant.get_collection(new_col)
    if new_info.points_count < points_count * 0.95:
        logger.error(f"New collection has {new_info.points_count} but expected ~{points_count}. Keeping both.")
        await qdrant.close()
        return

    # 5. Swap alias
    logger.info(f"Swapping alias: {collection} → {new_col}")
    try:
        # Remove old alias first
        from qdrant_client.http.models import DeleteAliasOperation, CreateAliasOperation
        await qdrant.update_collection_aliases(
            change_aliases_operations=[
                DeleteAliasOperation(delete_alias=qm.DeleteAlias(alias_name=collection)),
            ]
        )
    except Exception:
        pass  # Alias might not exist

    try:
        # Delete old actual collection if it's not an alias target for something else
        old_actual = f"{collection}_bm25"
        await qdrant.delete_collection(old_actual)
        logger.info(f"Deleted old collection {old_actual}")
    except Exception:
        pass

    try:
        from qdrant_client.http.models import CreateAliasOperation
        await qdrant.update_collection_aliases(
            change_aliases_operations=[
                CreateAliasOperation(
                    create_alias=qm.CreateAlias(collection_name=new_col, alias_name=collection)
                ),
            ]
        )
        logger.info(f"Created alias {collection} → {new_col}")
    except Exception as e:
        logger.warning(f"Alias failed ({e}), collection available as {new_col}")

    await qdrant.close()
    logger.info("DONE!")


def main():
    parser = argparse.ArgumentParser(description="Re-index with contextual prefix + new embedding model")
    parser.add_argument("--collection", default="kb_imam_v2_1024", help="Collection to re-index")
    parser.add_argument("--model", default="gemini-embedding-2-preview", help="Gemini embedding model")
    parser.add_argument("--dimension", type=int, default=1024, help="Embedding dimension")
    parser.add_argument("--batch-size", type=int, default=50, help="Batch size per API call")
    parser.add_argument("--concurrency", type=int, default=5, help="Concurrent API calls")
    parser.add_argument("--qdrant-url", default=QDRANT_URL)
    args = parser.parse_args()

    if not GOOGLE_API_KEY:
        logger.error("GOOGLE_API_KEY not set")
        sys.exit(1)

    asyncio.run(reindex(
        collection=args.collection,
        model=args.model,
        dimension=args.dimension,
        batch_size=args.batch_size,
        concurrency=args.concurrency,
        qdrant_url=args.qdrant_url,
    ))


if __name__ == "__main__":
    main()
