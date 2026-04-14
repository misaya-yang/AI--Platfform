#!/usr/bin/env python3
"""Enrich hadith vectors in Qdrant with book names and grading from registry.

Reads data/hadith_registry.json (produced by build_hadith_registry.py) and
batch-updates Qdrant payloads via set_payload — NO re-embedding needed.

Updates per vector:
  - source_reference: adds book_name, grade, collection display name
  - citation_text: rebuilds with book name, e.g. "Sahih al-Bukhari, Book 1 (Revelation), Hadith 7"
  - section_title: replaces "Book N" with actual book name

Usage:
    python scripts/enrich_hadith_vectors.py
    python scripts/enrich_hadith_vectors.py --registry data/hadith_registry.json --dry-run
"""
from __future__ import annotations

import argparse
import asyncio
import json
import logging
import re
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from src.config.settings import Settings

logger = logging.getLogger("enrich_vectors")

# Qdrant collection for Islamic KB
DATASET_ID = "islamic-knowledge"
EMBEDDING_DIM = 1024
SCROLL_BATCH = 100

# Collection slug → display name (fallback if registry doesn't have it)
COLLECTION_TITLES = {
    "bukhari": "Sahih al-Bukhari", "muslim": "Sahih Muslim",
    "abudawud": "Sunan Abu Dawud", "tirmidhi": "Jami at-Tirmidhi",
    "nasai": "Sunan an-Nasai", "ibnmajah": "Sunan Ibn Majah",
    "malik": "Muwatta Malik", "nawawi": "Forty Hadith an-Nawawi",
    "qudsi": "Forty Hadith Qudsi", "dehlawi": "Forty Hadith Shah Waliullah Dehlawi",
}

# Reverse lookup: display name → slug
DISPLAY_TO_SLUG = {v.lower(): k for k, v in COLLECTION_TITLES.items()}


def _extract_from_citation(citation: str) -> tuple[str | None, int | None, str | None]:
    """Extract (collection_slug, book_number, hadith_number) from citation_text.

    E.g. "Sahih al-Bukhari, Book 1, Hadith 7" → ("bukhari", 1, "7")
    """
    if not citation:
        return None, None, None

    # Try to find collection name
    slug = None
    lower = citation.lower()
    for display, s in DISPLAY_TO_SLUG.items():
        if display in lower:
            slug = s
            break

    # Extract book number
    book_match = re.search(r"Book\s+(\d+)", citation, re.IGNORECASE)
    book_num = int(book_match.group(1)) if book_match else None

    # Extract hadith number
    hadith_match = re.search(r"Hadith\s+(\d+)", citation, re.IGNORECASE)
    hadith_num = hadith_match.group(1) if hadith_match else None

    return slug, book_num, hadith_num


def _extract_from_source_ref(source_ref: dict | str | None) -> tuple[str | None, int | None, str | None]:
    """Extract (collection_slug, book_number, hadith_number) from source_reference."""
    if not source_ref:
        return None, None, None

    if isinstance(source_ref, str):
        try:
            source_ref = json.loads(source_ref)
        except (json.JSONDecodeError, TypeError):
            return None, None, None

    collection = source_ref.get("collection", "")
    # Normalize: could be slug ("bukhari") or display name ("Sahih al-Bukhari")
    slug = DISPLAY_TO_SLUG.get(collection.lower(), collection.lower())

    book = source_ref.get("book") or source_ref.get("book_number")
    book_num = int(book) if book is not None else None

    hadith_num = source_ref.get("hadith_number")
    hadith_str = str(hadith_num) if hadith_num is not None else None

    return slug, book_num, hadith_str


def _build_citation(display_name: str, book_num: int | None, book_name: str, hadith_num: str | None) -> str:
    """Build enriched citation text."""
    parts = [display_name]
    if book_num is not None:
        if book_name:
            parts.append(f"Book {book_num} ({book_name})")
        else:
            parts.append(f"Book {book_num}")
    if hadith_num:
        parts.append(f"Hadith {hadith_num}")
    return ", ".join(parts)


async def run(args):
    from qdrant_client import AsyncQdrantClient
    from qdrant_client.http import models as qmodels

    # Load registry
    registry_path = Path(args.registry)
    if not registry_path.exists():
        logger.error("Registry file not found: %s", registry_path)
        logger.error("Run build_hadith_registry.py first to create it.")
        return 1

    with open(registry_path, encoding="utf-8") as f:
        registry = json.load(f)

    collections = registry.get("collections", {})
    logger.info("Loaded registry: %d collections", len(collections))

    # Connect to Qdrant
    settings = Settings()
    qdrant_url = str(settings.knowledge.qdrant.url)
    qdrant_key = settings.knowledge.qdrant.api_key or None

    client = AsyncQdrantClient(url=qdrant_url, api_key=qdrant_key, timeout=120)

    # Determine collection name
    coll_name = f"kb_{DATASET_ID}_{EMBEDDING_DIM}"
    try:
        info = await client.get_collection(coll_name)
        total_points = info.points_count
        logger.info("Collection %s: %d total points", coll_name, total_points)
    except Exception as exc:
        logger.error("Collection %s not found: %s", coll_name, exc)
        return 1

    # Scroll through all hadith vectors
    updated = 0
    skipped = 0
    not_found = 0
    offset = None

    while True:
        scroll_result = await client.scroll(
            collection_name=coll_name,
            scroll_filter=qmodels.Filter(
                must=[qmodels.FieldCondition(
                    key="source_type",
                    match=qmodels.MatchValue(value="hadith"),
                )]
            ),
            limit=SCROLL_BATCH,
            offset=offset,
            with_payload=True,
            with_vectors=False,
        )

        points, next_offset = scroll_result
        if not points:
            break

        batch_updates: list[tuple[str, dict]] = []

        for point in points:
            payload = point.payload or {}
            point_id = point.id

            citation = payload.get("citation_text", "")
            source_ref = payload.get("source_reference")

            # Try source_reference first, then citation_text
            slug, book_num, hadith_num = _extract_from_source_ref(source_ref)
            if not slug:
                slug, book_num, hadith_num = _extract_from_citation(citation)

            if not slug or slug not in collections:
                skipped += 1
                continue

            coll_data = collections[slug]
            display_name = coll_data.get("name_en", COLLECTION_TITLES.get(slug, slug.title()))

            # Look up hadith in registry
            hadith_info = None
            if hadith_num:
                hadith_info = coll_data.get("hadiths", {}).get(str(hadith_num))

            # Look up book name
            book_name_en = ""
            book_name_ar = ""
            grade = ""

            if hadith_info:
                book_name_en = hadith_info.get("book_name_en", "")
                book_name_ar = hadith_info.get("book_name_ar", "")
                grade = hadith_info.get("grade", "")
                # If book_num not found from source_ref/citation, use registry
                if book_num is None:
                    book_num = hadith_info.get("book")
            elif book_num is not None:
                # Fallback: look up book name from books dict
                book_info = coll_data.get("books", {}).get(str(book_num), {})
                book_name_en = book_info.get("name_en", "")
                book_name_ar = book_info.get("name_ar", "")

            if not book_name_en and not grade:
                not_found += 1
                continue

            # Build enriched payload
            new_citation = _build_citation(display_name, book_num, book_name_en, hadith_num)

            # Build enriched source_reference
            new_source_ref = {
                "collection": display_name,
                "authority_rank": 2,
            }
            if book_num is not None:
                new_source_ref["book"] = book_num
            if book_name_en:
                new_source_ref["book_name"] = book_name_en
            if book_name_ar:
                new_source_ref["book_name_ar"] = book_name_ar
            if hadith_num:
                new_source_ref["hadith_number"] = str(hadith_num)
            if grade:
                new_source_ref["grade"] = grade

            new_payload = {
                "citation_text": new_citation,
                "source_reference": json.dumps(new_source_ref, ensure_ascii=False),
            }
            if book_name_en:
                new_payload["section_title"] = book_name_en

            batch_updates.append((point_id, new_payload))

        # Apply batch updates
        if batch_updates and not args.dry_run:
            for point_id, new_payload in batch_updates:
                try:
                    await client.set_payload(
                        collection_name=coll_name,
                        payload=new_payload,
                        points=[point_id],
                    )
                except Exception as exc:
                    logger.error("Failed to update point %s: %s", point_id, exc)

        updated += len(batch_updates)
        if batch_updates and args.dry_run:
            # Show sample
            sample_id, sample_payload = batch_updates[0]
            logger.info("[DRY-RUN] Sample update: %s → %s", sample_id, json.dumps(sample_payload, ensure_ascii=False)[:200])

        if updated % 500 == 0 or not next_offset:
            logger.info("Progress: %d updated, %d skipped, %d not_found", updated, skipped, not_found)

        offset = next_offset
        if not next_offset:
            break

    logger.info("=== DONE ===")
    logger.info("  Updated: %d", updated)
    logger.info("  Skipped (no slug match): %d", skipped)
    logger.info("  Not found in registry: %d", not_found)
    logger.info("  Dry run: %s", args.dry_run)

    await client.close()
    return 0


def main():
    logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(name)s - %(message)s")
    p = argparse.ArgumentParser(description="Enrich hadith vectors in Qdrant with book names and grading")
    p.add_argument("--registry", default="data/hadith_registry.json", help="Path to hadith_registry.json")
    p.add_argument("--dry-run", action="store_true", help="Preview changes without writing to Qdrant")
    raise SystemExit(asyncio.run(run(p.parse_args())))


if __name__ == "__main__":
    main()
