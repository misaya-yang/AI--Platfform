#!/usr/bin/env python3
"""Repair Hadith Arabic localizations that disagree with source text.

This is a deterministic repair script. It uses fawazahmed0/hadith-api Arabic
editions as the replacement source and, when HuggingFace has the same hadith
reference, requires the HuggingFace Arabic text to agree with fawaz before
updating. It never generates religious text.
"""

from __future__ import annotations

import argparse
import asyncio
import csv
import difflib
import io
import json
import os
import re
import time
import urllib.parse
import urllib.request
from dataclasses import asdict, dataclass
from typing import Any

import asyncpg

SCHEMA = "islamic_content"
COLLECTIONS = ("bukhari", "abudawud", "tirmidhi", "nasai", "ibnmajah", "nawawi")
FAWAZ_BASE_URL = "https://cdn.jsdelivr.net/gh/fawazahmed0/hadith-api@1/editions"
HF_BASE_URL = "https://huggingface.co/datasets/meeAtif/hadith_datasets/resolve/main"
HF_FILES = {
    "bukhari": "Sahih al-Bukhari.csv",
    "abudawud": "Sunan Abi Dawud.csv",
    "tirmidhi": "Jami` at-Tirmidhi.csv",
    "nasai": "Sunan an-Nasa'i.csv",
    "ibnmajah": "Sunan Ibn Majah.csv",
}
BIDI_NOISE_CHARS = (
    "\u200e",
    "\u200f",
    "\u202a",
    "\u202b",
    "\u202c",
    "\u202d",
    "\u202e",
    "\u2066",
    "\u2067",
    "\u2068",
    "\u2069",
    "\ufffd",
)


@dataclass(frozen=True)
class Candidate:
    hadith_item_id: int
    localization_id: int
    collection_name: str
    book_number: str
    hadith_number: str
    current_text: str
    source_text: str
    current_source_similarity: float
    hf_source_similarity: float | None
    hf_present: bool

    @property
    def key(self) -> str:
        return f"{self.collection_name}/{self.book_number}/{self.hadith_number}"


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(description="Repair Arabic Hadith text mismatches from sources.")
    parser.add_argument(
        "--dsn",
        default=os.getenv("ISLAMIC_CONTENT_DATABASE__DSN") or os.getenv("GATEWAY_DATABASE__DSN"),
    )
    parser.add_argument("--apply", action="store_true", help="Write changes. Default is dry-run.")
    parser.add_argument("--timeout", type=float, default=120.0)
    parser.add_argument("--mismatch-threshold", type=float, default=0.90)
    parser.add_argument("--hf-agreement-threshold", type=float, default=0.70)
    parser.add_argument("--backup-out", default="/tmp/hadith_arabic_text_mismatch_backup.json")
    parser.add_argument("--collections", default=",".join(COLLECTIONS))
    return parser.parse_args()


def _clean(value: Any) -> str:
    text = str(value or "")
    for char in BIDI_NOISE_CHARS:
        text = text.replace(char, "")
    text = re.sub(r"<[^>]+>", " ", text)
    return re.sub(r"\s+", " ", text).strip()


def _norm(value: Any) -> str:
    text = _clean(value)
    text = re.sub(r"[\u064b-\u065f\u0670\u0640]", "", text)
    return text.casefold()


def _similarity(left: str, right: str) -> float:
    a = _norm(left)
    b = _norm(right)
    if not a or not b:
        return 0.0
    if a == b or a in b or b in a:
        return 1.0
    return difflib.SequenceMatcher(None, a[:3500], b[:3500]).ratio()


def _fetch_bytes(url: str, timeout: float) -> bytes:
    request = urllib.request.Request(url, headers={"User-Agent": "islamic-content-repair/1.0"})
    with urllib.request.urlopen(request, timeout=timeout) as response:
        return response.read()


def _fetch_json(url: str, timeout: float) -> dict[str, Any]:
    payload = json.loads(_fetch_bytes(url, timeout).decode("utf-8", "replace"))
    if not isinstance(payload, dict):
        raise TypeError(f"Expected JSON object from {url}")
    return payload


def _reference_number(reference: str) -> str:
    match = re.search(r":([^/:?#]+)(?:[?#].*)?$", reference or "")
    return match.group(1) if match else ""


def load_fawaz_arabic(collections: tuple[str, ...], timeout: float) -> dict[tuple[str, str], str]:
    rows: dict[tuple[str, str], str] = {}
    for collection in collections:
        url = f"{FAWAZ_BASE_URL}/ara-{urllib.parse.quote(collection)}.min.json"
        payload = _fetch_json(url, timeout)
        for hadith in payload.get("hadiths", []):
            number = _clean(hadith.get("hadithnumber"))
            text = _clean(hadith.get("text"))
            if number and text:
                rows[(collection, number)] = text
    return rows


def load_hf_arabic(collections: tuple[str, ...], timeout: float) -> dict[tuple[str, str], str]:
    rows: dict[tuple[str, str], str] = {}
    for collection in collections:
        filename = HF_FILES.get(collection)
        if not filename:
            continue
        url = f"{HF_BASE_URL}/{urllib.parse.quote(filename)}"
        wrapper = io.StringIO(_fetch_bytes(url, timeout).decode("utf-8-sig", "replace"))
        for row in csv.DictReader(wrapper):
            number = _reference_number(row.get("Reference", ""))
            text = _clean(row.get("Arabic_Text"))
            if number and text:
                rows[(collection, number)] = text
    return rows


async def load_local_rows(conn: asyncpg.Connection, collections: tuple[str, ...]) -> list[asyncpg.Record]:
    return await conn.fetch(
        f"""
        SELECT
            hi.id AS hadith_item_id,
            hl.id AS localization_id,
            hi.collection_name,
            hi.book_number,
            hi.hadith_number,
            hl.body_text AS current_text
        FROM {SCHEMA}.hadith_items hi
        JOIN {SCHEMA}.hadith_localizations hl
            ON hl.hadith_item_id = hi.id AND hl.language = 'ar'
        WHERE hi.collection_name = ANY($1::text[])
          AND hi.book_number <> '0'
        ORDER BY hi.collection_name, hi.book_number, hi.hadith_number
        """,
        list(collections),
    )


def find_candidates(
    rows: list[asyncpg.Record],
    *,
    fawaz_ar: dict[tuple[str, str], str],
    hf_ar: dict[tuple[str, str], str],
    mismatch_threshold: float,
    hf_agreement_threshold: float,
) -> tuple[list[Candidate], list[dict[str, Any]]]:
    candidates: list[Candidate] = []
    skipped: list[dict[str, Any]] = []
    for row in rows:
        key = (row["collection_name"], row["hadith_number"])
        source_text = fawaz_ar.get(key, "")
        if not source_text:
            continue
        current_text = _clean(row["current_text"])
        current_source_similarity = _similarity(current_text, source_text)
        if current_source_similarity >= mismatch_threshold:
            continue

        hf_text = hf_ar.get(key, "")
        hf_source_similarity = _similarity(hf_text, source_text) if hf_text else None
        if hf_text and (hf_source_similarity or 0.0) < hf_agreement_threshold:
            skipped.append(
                {
                    "key": f"{row['collection_name']}/{row['book_number']}/{row['hadith_number']}",
                    "reason": "HF Arabic does not agree with fawaz Arabic",
                    "current_source_similarity": round(current_source_similarity, 3),
                    "hf_source_similarity": round(hf_source_similarity or 0.0, 3),
                }
            )
            continue

        candidates.append(
            Candidate(
                hadith_item_id=row["hadith_item_id"],
                localization_id=row["localization_id"],
                collection_name=row["collection_name"],
                book_number=row["book_number"],
                hadith_number=row["hadith_number"],
                current_text=current_text,
                source_text=source_text,
                current_source_similarity=round(current_source_similarity, 3),
                hf_source_similarity=round(hf_source_similarity, 3) if hf_source_similarity is not None else None,
                hf_present=bool(hf_text),
            )
        )
    return candidates, skipped


async def repair(args: argparse.Namespace) -> dict[str, Any]:
    collections = tuple(item.strip() for item in args.collections.split(",") if item.strip())
    fawaz_ar = load_fawaz_arabic(collections, args.timeout)
    hf_ar = load_hf_arabic(collections, args.timeout)
    conn = await asyncpg.connect(args.dsn)
    try:
        rows = await load_local_rows(conn, collections)
        candidates, skipped = find_candidates(
            rows,
            fawaz_ar=fawaz_ar,
            hf_ar=hf_ar,
            mismatch_threshold=args.mismatch_threshold,
            hf_agreement_threshold=args.hf_agreement_threshold,
        )
        backup = {
            "generated_at": time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime()),
            "applied": args.apply,
            "candidates": [asdict(candidate) | {"key": candidate.key} for candidate in candidates],
            "skipped": skipped,
        }
        with open(args.backup_out, "w", encoding="utf-8") as handle:
            json.dump(backup, handle, ensure_ascii=False, indent=2)

        updated = 0
        if args.apply and candidates:
            async with conn.transaction():
                for candidate in candidates:
                    await conn.execute(
                        f"""
                        UPDATE {SCHEMA}.hadith_localizations
                        SET body_text = $1, updated_at = NOW()
                        WHERE id = $2 AND language = 'ar'
                        """,
                        candidate.source_text,
                        candidate.localization_id,
                    )
                    updated += 1
        return {
            "applied": args.apply,
            "local_rows_checked": len(rows),
            "source_rows_loaded": len(fawaz_ar),
            "candidates": len(candidates),
            "updated": updated,
            "skipped": len(skipped),
            "backup_out": args.backup_out,
            "examples": [
                {
                    "key": candidate.key,
                    "current_source_similarity": candidate.current_source_similarity,
                    "hf_source_similarity": candidate.hf_source_similarity,
                    "hf_present": candidate.hf_present,
                }
                for candidate in candidates[:20]
            ],
            "skipped_examples": skipped[:20],
        }
    finally:
        await conn.close()


async def main() -> int:
    args = parse_args()
    if not args.dsn:
        raise SystemExit("--dsn or ISLAMIC_CONTENT_DATABASE__DSN is required")
    result = await repair(args)
    print(json.dumps(result, ensure_ascii=False, indent=2))
    return 0


if __name__ == "__main__":
    raise SystemExit(asyncio.run(main()))
