#!/usr/bin/env python3
"""Tafseer Metadata Enrichment Script.

Uses Gemini Flash to identify surah/verse references from tafseer text content,
then updates Qdrant payload metadata in-place (no re-embedding needed).

Also deletes garbage OCR vectors.

Usage:
    python3 enrich_tafseer_metadata.py [--dry-run] [--concurrency 10] [--batch-size 20]
"""

import argparse
import asyncio
import json
import logging
import os
import re
import sys
import time
from dataclasses import dataclass
from typing import Any

import httpx

logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(message)s")
logger = logging.getLogger(__name__)

QDRANT_URL = os.getenv("QDRANT_URL", "http://localhost:6333")
COLLECTION = os.getenv("QDRANT_COLLECTION", "kb_imam_v2_1024")
DASHSCOPE_API_KEY = os.getenv("DASHSCOPE_API_KEY", "")
DASHSCOPE_BASE_URL = os.getenv("DASHSCOPE_BASE_URL", "https://dashscope.aliyuncs.com/compatible-mode/v1")
QWEN_MODEL = os.getenv("QWEN_MODEL", "qwen3.6-plus")

EXTRACTION_PROMPT = """You are a Quran/Tafseer metadata extraction tool. Given a text chunk from a Tafseer (Quranic commentary), identify which Surah and Verse(s) it is commenting on.

Return ONLY a JSON object with these fields:
- "surah_number": integer (1-114) or null if cannot determine
- "verse_start": integer or null
- "verse_end": integer or null (same as verse_start if single verse)
- "surah_name_en": string or null (e.g., "Al-Baqarah")
- "confidence": "high" | "medium" | "low"

If the text discusses multiple surahs, return the PRIMARY one being discussed.
If you cannot determine the surah/verse at all, return all nulls with confidence "low".

IMPORTANT: Only return the JSON object, no other text.

Text chunk:
---
{text}
---"""


@dataclass
class EnrichmentStats:
    total: int = 0
    enriched: int = 0
    skipped: int = 0
    garbage_deleted: int = 0
    errors: int = 0
    high_confidence: int = 0
    medium_confidence: int = 0
    low_confidence: int = 0


def is_garbage(text: str) -> bool:
    """Detect garbage OCR content."""
    if not text or len(text.strip()) < 50:
        return True
    if text.count("that that") > 2 or text.count("that") > 20:
        return True
    if text.count("space") > 5 and len(text) < 300:
        return True
    return False


async def extract_metadata_qwen(
    client: httpx.AsyncClient,
    text: str,
    semaphore: asyncio.Semaphore,
) -> dict[str, Any] | None:
    """Call Qwen 3.6-Plus via DashScope to extract surah/verse from tafseer text."""
    async with semaphore:
        prompt = EXTRACTION_PROMPT.format(text=text[:2000])
        payload = {
            "model": QWEN_MODEL,
            "messages": [{"role": "user", "content": prompt}],
            "temperature": 0.0,
            "max_tokens": 200,
            "enable_thinking": False,
        }
        try:
            resp = await client.post(
                f"{DASHSCOPE_BASE_URL}/chat/completions",
                headers={"Authorization": f"Bearer {DASHSCOPE_API_KEY}"},
                json=payload,
                timeout=30,
            )
            resp.raise_for_status()
            data = resp.json()
            text_out = data["choices"][0]["message"]["content"]
            # Parse JSON from response
            text_out = text_out.strip()
            # Strip markdown code fences
            if "```" in text_out:
                text_out = re.sub(r"```\w*\n?", "", text_out).strip()
            result = json.loads(text_out)
            # Normalize confidence: numeric -> string
            conf = result.get("confidence", 0)
            if isinstance(conf, (int, float)):
                if conf >= 0.8:
                    result["confidence"] = "high"
                elif conf >= 0.5:
                    result["confidence"] = "medium"
                else:
                    result["confidence"] = "low"
            return result
        except Exception as e:
            logger.warning("Qwen extraction failed: %s (raw=%s)", e, text_out[:100] if 'text_out' in dir() else "no response")
            return None


async def update_qdrant_payload(
    client: httpx.AsyncClient,
    point_id: str,
    payload_update: dict[str, Any],
) -> bool:
    """Update a single point's payload in Qdrant."""
    try:
        resp = await client.post(
            f"{QDRANT_URL}/collections/{COLLECTION}/points/payload",
            json={
                "payload": payload_update,
                "points": [point_id],
            },
            timeout=10,
        )
        resp.raise_for_status()
        return True
    except Exception as e:
        logger.warning("Qdrant update failed for %s: %s", point_id, e)
        return False


async def delete_qdrant_points(
    client: httpx.AsyncClient,
    point_ids: list[str],
) -> bool:
    """Delete garbage points from Qdrant."""
    if not point_ids:
        return True
    try:
        resp = await client.post(
            f"{QDRANT_URL}/collections/{COLLECTION}/points/delete",
            json={"points": point_ids},
            timeout=30,
        )
        resp.raise_for_status()
        return True
    except Exception as e:
        logger.warning("Qdrant delete failed: %s", e)
        return False


async def scroll_tafseer(client: httpx.AsyncClient) -> list[dict]:
    """Scroll all tafseer points from Qdrant."""
    all_points = []
    offset = None

    while True:
        body: dict[str, Any] = {
            "limit": 200,
            "with_payload": True,
            "with_vector": False,
            "filter": {"must": [{"key": "source_type", "match": {"value": "tafseer"}}]},
        }
        if offset:
            body["offset"] = offset

        resp = await client.post(
            f"{QDRANT_URL}/collections/{COLLECTION}/points/scroll",
            json=body,
            timeout=30,
        )
        data = resp.json()
        points = data.get("result", {}).get("points", [])
        offset = data.get("result", {}).get("next_page_offset")

        if not points:
            break
        all_points.extend(points)
        if not offset:
            break

    return all_points


async def process_batch(
    gemini_client: httpx.AsyncClient,
    qdrant_client: httpx.AsyncClient,
    batch: list[dict],
    semaphore: asyncio.Semaphore,
    stats: EnrichmentStats,
    dry_run: bool,
) -> None:
    """Process a batch of tafseer points."""
    tasks = []
    for point in batch:
        text = point.get("payload", {}).get("text", "")
        tasks.append(extract_metadata_qwen(gemini_client, text, semaphore))

    results = await asyncio.gather(*tasks)

    for point, result in zip(batch, results):
        point_id = point["id"]
        stats.total += 1

        if result is None:
            stats.errors += 1
            continue

        surah = result.get("surah_number")
        verse_start = result.get("verse_start")
        confidence = result.get("confidence", "low")

        if confidence == "high":
            stats.high_confidence += 1
        elif confidence == "medium":
            stats.medium_confidence += 1
        else:
            stats.low_confidence += 1

        if not surah:
            stats.skipped += 1
            continue

        # Build metadata update
        update = {
            "surah_number": surah,
            "chapter_id": surah,
        }
        if verse_start:
            update["verse_number"] = verse_start
            update["ayah_number"] = verse_start
        verse_end = result.get("verse_end")
        if verse_end and verse_end != verse_start:
            update["verse_end"] = verse_end

        surah_name = result.get("surah_name_en")
        if surah_name:
            update["chapter_name_en"] = surah_name

        # Update citation_text with surah/verse
        book_title = point.get("payload", {}).get("book_title", "Tafsir")
        if verse_start:
            update["citation_text"] = f"{book_title}, Surah {surah} Verse {verse_start}"
        else:
            update["citation_text"] = f"{book_title}, Surah {surah}"

        update["metadata_enriched"] = True
        update["enrichment_confidence"] = confidence

        if dry_run:
            logger.info("[DRY-RUN] Would update %s: surah=%s verse=%s (%s)", point_id, surah, verse_start, confidence)
        else:
            ok = await update_qdrant_payload(qdrant_client, point_id, update)
            if ok:
                stats.enriched += 1
            else:
                stats.errors += 1


async def main():
    parser = argparse.ArgumentParser(description="Enrich tafseer metadata via Gemini Flash")
    parser.add_argument("--dry-run", action="store_true", help="Don't write to Qdrant")
    parser.add_argument("--concurrency", type=int, default=20, help="Qwen API concurrency")
    parser.add_argument("--batch-size", type=int, default=20, help="Points per batch")
    args = parser.parse_args()

    if not DASHSCOPE_API_KEY:
        logger.error("DASHSCOPE_API_KEY not set")
        sys.exit(1)

    stats = EnrichmentStats()
    semaphore = asyncio.Semaphore(args.concurrency)

    async with httpx.AsyncClient() as qdrant_client:
        logger.info("Scrolling all tafseer points from Qdrant...")
        all_points = await scroll_tafseer(qdrant_client)
        logger.info("Found %d tafseer points", len(all_points))

        # Phase 1: Filter garbage + skip already-enriched
        garbage_ids = []
        good_points = []
        already_enriched = 0
        for p in all_points:
            text = p.get("payload", {}).get("text", "")
            if is_garbage(text):
                garbage_ids.append(p["id"])
            elif p.get("payload", {}).get("metadata_enriched"):
                already_enriched += 1
            else:
                good_points.append(p)
        logger.info("Already enriched (skipping): %d", already_enriched)

        logger.info("Garbage vectors: %d, Good vectors: %d", len(garbage_ids), len(good_points))

        if garbage_ids:
            if args.dry_run:
                logger.info("[DRY-RUN] Would delete %d garbage vectors", len(garbage_ids))
            else:
                # Delete in batches of 100
                for i in range(0, len(garbage_ids), 100):
                    batch_ids = garbage_ids[i : i + 100]
                    ok = await delete_qdrant_points(qdrant_client, batch_ids)
                    if ok:
                        stats.garbage_deleted += len(batch_ids)
                        logger.info("Deleted %d garbage vectors (%d/%d)", len(batch_ids), i + len(batch_ids), len(garbage_ids))

        # Phase 2: Enrich metadata via Gemini Flash
        logger.info("Starting metadata enrichment with concurrency=%d...", args.concurrency)
        start_time = time.time()

        async with httpx.AsyncClient() as gemini_client:
            for i in range(0, len(good_points), args.batch_size):
                batch = good_points[i : i + args.batch_size]
                await process_batch(gemini_client, qdrant_client, batch, semaphore, stats, args.dry_run)

                # Progress log every 200 points
                if (i + args.batch_size) % 200 == 0 or i + args.batch_size >= len(good_points):
                    elapsed = time.time() - start_time
                    rate = stats.total / max(elapsed, 1)
                    remaining = (len(good_points) - stats.total) / max(rate, 0.1)
                    logger.info(
                        "Progress: %d/%d (%.1f%%) | Enriched: %d | Errors: %d | %.1f pts/s | ETA: %.0fs",
                        stats.total, len(good_points),
                        100 * stats.total / max(len(good_points), 1),
                        stats.enriched, stats.errors, rate, remaining,
                    )

    elapsed = time.time() - start_time
    logger.info("=" * 60)
    logger.info("DONE in %.1fs", elapsed)
    logger.info("  Total processed: %d", stats.total)
    logger.info("  Enriched: %d", stats.enriched)
    logger.info("  Skipped (no surah found): %d", stats.skipped)
    logger.info("  Errors: %d", stats.errors)
    logger.info("  Garbage deleted: %d", stats.garbage_deleted)
    logger.info("  Confidence: high=%d medium=%d low=%d", stats.high_confidence, stats.medium_confidence, stats.low_confidence)


if __name__ == "__main__":
    asyncio.run(main())
