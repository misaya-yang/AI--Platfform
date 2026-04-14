"""Enrich hadith_grades from HuggingFace dataset (meeAtif/hadith_datasets).

CDN source (fawazahmed0) has empty grades for most collections.
HuggingFace dataset provides grading for the 6 major Sahih/Sunan collections.

Bukhari and Muslim are implicitly all Sahih by scholarly consensus;
Tirmidhi, Abu Dawud, Nasai, Ibn Majah have per-hadith grades.
"""
from __future__ import annotations

import json
import logging
import re
import urllib.parse
from typing import Any

import httpx

from ..db import Database

logger = logging.getLogger(__name__)

HF_BASE = "https://huggingface.co/datasets/meeAtif/hadith_datasets/resolve/main"

# Exact HuggingFace filenames → our collection slugs
HF_FILES: dict[str, str] = {
    "Sahih al-Bukhari.json": "bukhari",
    "Sahih Muslim.json": "muslim",
    "Sunan Abi Dawud.json": "abudawud",
    "Jami` at-Tirmidhi.json": "tirmidhi",
    "Sunan an-Nasa'i.json": "nasai",
    "Sunan Ibn Majah.json": "ibnmajah",
}

# Default grades for collections where all hadiths are sahih
IMPLICIT_SAHIH = {"bukhari", "muslim"}


def _extract_hadith_number(ref: str) -> str | None:
    """Extract trailing number from Reference like 'Sahih al-Bukhari 1'."""
    m = re.search(r"(\d+)\s*$", (ref or "").strip())
    return m.group(1) if m else None


async def enrich_grades(db: Database, *, dry_run: bool = False) -> dict[str, Any]:
    """Download grading from HuggingFace and insert into hadith_grades.

    Only inserts grades for hadiths that don't already have them.
    """
    stats: dict[str, Any] = {"collections": {}, "total_inserted": 0, "total_skipped": 0}

    client = httpx.AsyncClient(timeout=120.0, follow_redirects=True)
    try:
        for filename, slug in HF_FILES.items():
            encoded = urllib.parse.quote(filename)
            url = f"{HF_BASE}/{encoded}"
            logger.info("Downloading %s ...", filename)

            try:
                resp = await client.get(url)
                resp.raise_for_status()
                data = resp.json()
            except Exception as exc:
                logger.warning("Failed to download %s: %s", filename, exc)
                stats["collections"][slug] = {"error": str(exc)}
                continue

            logger.info("  %s: %d items downloaded", slug, len(data))

            # Build grade lookup: hadith_number → grade string
            grade_lookup: dict[str, str] = {}
            for item in data:
                ref = item.get("Reference", "")
                hnum = _extract_hadith_number(ref)
                if not hnum:
                    continue
                grade = (item.get("Grade") or "").strip()
                if grade:
                    grade_lookup[hnum] = grade

            # For Bukhari/Muslim: all are Sahih if not explicitly graded
            if slug in IMPLICIT_SAHIH:
                for item in data:
                    ref = item.get("Reference", "")
                    hnum = _extract_hadith_number(ref)
                    if hnum and hnum not in grade_lookup:
                        grade_lookup[hnum] = "Sahih"

            if not grade_lookup:
                logger.info("  %s: no grades found, skipping", slug)
                stats["collections"][slug] = {"grades": 0}
                continue

            # Get hadith_item IDs for this collection
            rows = await db.fetch(
                "SELECT id, hadith_number FROM hadith_items WHERE collection_name = $1",
                slug,
            )
            item_map = {str(r["hadith_number"]): r["id"] for r in rows}

            # Check existing grades
            existing = await db.fetch(
                """SELECT DISTINCT hi.hadith_number
                   FROM hadith_grades hg
                   JOIN hadith_items hi ON hi.id = hg.hadith_item_id
                   WHERE hi.collection_name = $1""",
                slug,
            )
            existing_nums = {str(r["hadith_number"]) for r in existing}

            # Build insert rows
            grade_rows: list[tuple[Any, ...]] = []
            for hnum, grade in grade_lookup.items():
                if hnum in existing_nums:
                    continue
                item_id = item_map.get(hnum)
                if item_id is None:
                    continue
                grade_rows.append((
                    item_id,
                    "en",
                    grade,
                    "HuggingFace/meeAtif",
                    json.dumps({"grade": grade, "source": "meeAtif/hadith_datasets"}),
                ))

            inserted = 0
            skipped = len(existing_nums)

            if grade_rows and not dry_run:
                await db.executemany(
                    """INSERT INTO hadith_grades
                       (hadith_item_id, language, grade, graded_by, raw_payload, updated_at)
                       VALUES ($1, $2, $3, $4, $5::jsonb, NOW())
                       ON CONFLICT (hadith_item_id, language, COALESCE(graded_by, '')) DO NOTHING""",
                    grade_rows,
                )
                inserted = len(grade_rows)

            if dry_run:
                inserted = len(grade_rows)
                logger.info("  [DRY-RUN] %s: would insert %d grades, skip %d existing",
                            slug, inserted, skipped)
            else:
                logger.info("  %s: inserted %d grades, skipped %d existing",
                            slug, inserted, skipped)

            stats["collections"][slug] = {
                "downloaded": len(data),
                "grades_found": len(grade_lookup),
                "inserted": inserted,
                "skipped": skipped,
            }
            stats["total_inserted"] += inserted
            stats["total_skipped"] += skipped

    finally:
        await client.aclose()

    return stats
