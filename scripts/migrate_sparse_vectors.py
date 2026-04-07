#!/usr/bin/env python3
"""Migrate existing Qdrant collection to add BM25 sparse vectors.

Creates a new collection with dense + sparse config, copies all points
with generated sparse vectors, then swaps via rename.

Usage:
    python3 scripts/migrate_sparse_vectors.py [--collection kb_imam_v2_1024] [--batch-size 100]
"""

from __future__ import annotations

import argparse
import asyncio
import logging
import os
import re
import sys
import time
from collections import Counter

logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(message)s")
logger = logging.getLogger(__name__)

QDRANT_URL = os.getenv("QDRANT_URL", "http://localhost:6333")

# =============================================================================
# Inline tokenizer (same as retrieval.py — avoid import path issues in scripts)
# =============================================================================

_RE_LATIN_WORD = re.compile(r"[A-Za-z0-9\-_]+")
_RE_CJK_RUN = re.compile(r"[\u4e00-\u9fff]+")
_RE_ARABIC_RUN = re.compile(
    r"[\u0600-\u06ff\u0750-\u077f\u08a0-\u08ff\ufb50-\ufdff\ufe70-\ufeff]+"
)
_RE_ARABIC_DIACRITICS = re.compile(r"[\u064b-\u0652\u0670]")
_RE_ALEF = re.compile(r"[أإآٱ]")


def _normalize_arabic(text: str) -> str:
    t = _RE_ARABIC_DIACRITICS.sub("", text)
    t = _RE_ALEF.sub("ا", t)
    t = t.replace("ة", "ه").replace("ى", "ي")
    return t


def tokenize(text: str) -> list[str]:
    t = (text or "").strip()
    if not t:
        return []
    tokens: list[str] = []
    t_clean = t.lower()
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
                        stem = word[len(prefix) :]
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
    values = [tf[i] for i in indices]
    return indices, values


# =============================================================================
# Migration
# =============================================================================

async def migrate(
    collection: str,
    qdrant_url: str,
    batch_size: int = 100,
    dry_run: bool = False,
) -> None:
    from qdrant_client import AsyncQdrantClient
    from qdrant_client.http import models as qm

    client = AsyncQdrantClient(url=qdrant_url, timeout=120)

    # 1. Get old collection info
    old_info = await client.get_collection(collection)
    old_vectors = old_info.config.params.vectors
    if isinstance(old_vectors, dict):
        dim = old_vectors.get("", {}).size
    else:
        dim = old_vectors.size
    distance = old_vectors.distance if hasattr(old_vectors, "distance") else "Cosine"
    points_count = old_info.points_count
    logger.info(f"Source: {collection}, {points_count} points, dim={dim}, distance={distance}")

    new_collection = f"{collection}_bm25"

    # 2. Create new collection with sparse vectors
    try:
        await client.delete_collection(new_collection)
    except Exception:
        pass

    _dist_map = {"cosine": qm.Distance.COSINE, "dot": qm.Distance.DOT, "euclid": qm.Distance.EUCLID}
    dist = _dist_map.get(str(distance).lower(), qm.Distance.COSINE)
    await client.create_collection(
        collection_name=new_collection,
        vectors_config=qm.VectorParams(size=dim, distance=dist),
        sparse_vectors_config={
            "bm25": qm.SparseVectorParams(modifier=qm.Modifier.IDF),
        },
    )
    logger.info(f"Created {new_collection} with dense({dim}) + BM25 sparse")

    # 3. Copy payload indexes from old collection
    old_indexes = getattr(old_info, "payload_schema", {}) or {}
    for field_name, schema_info in old_indexes.items():
        try:
            field_type = getattr(schema_info, "data_type", None)
            if field_type:
                schema_type = {
                    "keyword": qm.PayloadSchemaType.KEYWORD,
                    "integer": qm.PayloadSchemaType.INTEGER,
                    "float": qm.PayloadSchemaType.FLOAT,
                    "text": qm.PayloadSchemaType.TEXT,
                }.get(str(field_type).lower(), qm.PayloadSchemaType.KEYWORD)
                await client.create_payload_index(
                    collection_name=new_collection,
                    field_name=field_name,
                    field_schema=schema_type,
                )
        except Exception:
            pass
    # Also add new indexes for topic/keywords
    for fn in ("metadata.topic", "metadata.keywords"):
        try:
            await client.create_payload_index(
                new_collection, field_name=fn, field_schema=qm.PayloadSchemaType.KEYWORD,
            )
        except Exception:
            pass
    logger.info(f"Copied payload indexes")

    if dry_run:
        logger.info("DRY RUN — stopping before data copy")
        await client.delete_collection(new_collection)
        await client.close()
        return

    # 4. Scroll and copy all points with sparse vectors
    total_migrated = 0
    total_sparse = 0
    scroll_offset = None
    start_time = time.time()

    while True:
        results, next_offset = await client.scroll(
            collection_name=collection,
            limit=batch_size,
            offset=scroll_offset,
            with_payload=True,
            with_vectors=True,
        )

        if not results:
            break

        points = []
        for p in results:
            # Get dense vector
            vec = p.vector
            if vec is None:
                logger.warning(f"Point {p.id} has no vector, skipping")
                continue
            if isinstance(vec, dict):
                dense = vec.get("") or vec.get("default")
                if dense is None and vec:
                    dense = list(vec.values())[0]
                if dense is None:
                    logger.warning(f"Point {p.id} has no usable dense vector, skipping")
                    continue
            else:
                dense = vec

            # Get text for sparse vector generation
            payload = dict(p.payload or {})
            text = str(payload.get("text") or "")

            # Generate sparse vector
            sparse_indices, sparse_values = text_to_sparse(text)

            vector_data: dict = {"": dense}
            if sparse_indices:
                vector_data["bm25"] = qm.SparseVector(
                    indices=sparse_indices, values=sparse_values,
                )
                total_sparse += 1

            points.append(qm.PointStruct(
                id=p.id,
                vector=vector_data,
                payload=payload,
            ))

        await client.upsert(collection_name=new_collection, points=points)
        total_migrated += len(points)

        elapsed = time.time() - start_time
        rate = total_migrated / elapsed if elapsed > 0 else 0
        logger.info(
            f"  Migrated {total_migrated}/{points_count} "
            f"({total_migrated * 100 / max(points_count, 1):.1f}%) "
            f"[{rate:.0f} pts/s, sparse={total_sparse}]"
        )

        if next_offset is None:
            break
        scroll_offset = next_offset

    elapsed = time.time() - start_time
    logger.info(
        f"Migration complete: {total_migrated} points, "
        f"{total_sparse} with sparse vectors, "
        f"{elapsed:.1f}s ({total_migrated / max(elapsed, 1):.0f} pts/s)"
    )

    # 5. Swap: delete old, rename new
    logger.info(f"Swapping: delete {collection}, rename {new_collection} → {collection}")

    # Verify new collection has all points
    new_info = await client.get_collection(new_collection)
    new_count = new_info.points_count
    if new_count < points_count * 0.95:
        logger.error(
            f"New collection has {new_count} points but expected ~{points_count}. "
            f"Aborting swap. New collection kept as {new_collection}."
        )
        await client.close()
        return

    await client.delete_collection(collection)
    # Qdrant doesn't have rename — recreate old name as alias or copy
    # Workaround: create alias
    try:
        from qdrant_client.http.models import CreateAliasOperation, AliasOperations
        await client.update_collection_aliases(
            change_aliases_operations=[
                CreateAliasOperation(
                    create_alias=qm.CreateAlias(
                        collection_name=new_collection,
                        alias_name=collection,
                    )
                )
            ]
        )
        logger.info(f"Created alias {collection} → {new_collection}")
    except Exception as alias_err:
        logger.warning(f"Alias creation failed ({alias_err}), collection available as {new_collection}")
        logger.info(f"Update dataset collection_name to '{new_collection}' manually if needed")

    await client.close()
    logger.info("DONE!")


def main():
    parser = argparse.ArgumentParser(description="Migrate Qdrant collection to add BM25 sparse vectors")
    parser.add_argument("--collection", default="kb_imam_v2_1024", help="Collection to migrate")
    parser.add_argument("--qdrant-url", default=QDRANT_URL, help="Qdrant URL")
    parser.add_argument("--batch-size", type=int, default=100, help="Scroll batch size")
    parser.add_argument("--dry-run", action="store_true", help="Dry run (don't copy data)")
    args = parser.parse_args()

    asyncio.run(migrate(args.collection, args.qdrant_url, args.batch_size, args.dry_run))


if __name__ == "__main__":
    main()
