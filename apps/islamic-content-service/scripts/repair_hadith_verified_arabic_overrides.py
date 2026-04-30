#!/usr/bin/env python3
"""Apply verified Arabic Hadith text overrides from external sources.

Use this only for rows where the normal dual-source repair cannot decide
because sources disagree, but a live/source inspection identified the better
source. The script fetches the replacement text from the named source at run
time and writes a JSON backup before applying changes.
"""

from __future__ import annotations

import argparse
import asyncio
import csv
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
FAWAZ_BASE_URL = "https://cdn.jsdelivr.net/gh/fawazahmed0/hadith-api@1/editions"
HF_BASE_URL = "https://huggingface.co/datasets/meeAtif/hadith_datasets/resolve/main"
HF_FILES = {
    "nasai": "Sunan an-Nasa'i.csv",
}

OVERRIDES = (
    {
        "collection": "tirmidhi",
        "book": "17",
        "hadith": "1424",
        "source": "fawaz",
        "reason": "fawaz Arabic agrees with live sunnah.com; HF row is shifted/commentary text",
    },
    {
        "collection": "tirmidhi",
        "book": "17",
        "hadith": "1425",
        "source": "fawaz",
        "reason": "fawaz Arabic agrees with live sunnah.com; HF row is shifted/commentary text",
    },
    {
        "collection": "nasai",
        "book": "1",
        "hadith": "135",
        "source": "hf",
        "reason": "fawaz Arabic contains U+FFFD at this row; HF/live sunnah.com preserve the missing Arabic letter",
    },
)
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
class AppliedOverride:
    collection: str
    book: str
    hadith: str
    source: str
    reason: str
    localization_id: int
    old_text: str
    new_text: str

    @property
    def key(self) -> str:
        return f"{self.collection}/{self.book}/{self.hadith}"


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(description="Apply verified Arabic source overrides.")
    parser.add_argument(
        "--dsn",
        default=os.getenv("ISLAMIC_CONTENT_DATABASE__DSN") or os.getenv("GATEWAY_DATABASE__DSN"),
    )
    parser.add_argument("--apply", action="store_true")
    parser.add_argument("--timeout", type=float, default=120.0)
    parser.add_argument("--backup-out", default="/tmp/hadith_verified_arabic_overrides_backup.json")
    return parser.parse_args()


def _clean(value: Any) -> str:
    text = str(value or "")
    for char in BIDI_NOISE_CHARS:
        text = text.replace(char, "")
    return re.sub(r"\s+", " ", text).strip()


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


def fetch_fawaz_arabic(collection: str, hadith: str, timeout: float) -> str:
    payload = _fetch_json(f"{FAWAZ_BASE_URL}/ara-{urllib.parse.quote(collection)}.min.json", timeout)
    for row in payload.get("hadiths", []):
        if _clean(row.get("hadithnumber")) == hadith:
            return _clean(row.get("text"))
    return ""


def fetch_hf_arabic(collection: str, hadith: str, timeout: float) -> str:
    filename = HF_FILES[collection]
    url = f"{HF_BASE_URL}/{urllib.parse.quote(filename)}"
    wrapper = io.StringIO(_fetch_bytes(url, timeout).decode("utf-8-sig", "replace"))
    for row in csv.DictReader(wrapper):
        if _reference_number(row.get("Reference", "")) == hadith:
            return _clean(row.get("Arabic_Text"))
    return ""


async def apply_overrides(args: argparse.Namespace) -> dict[str, Any]:
    conn = await asyncpg.connect(args.dsn)
    applied: list[AppliedOverride] = []
    try:
        for override in OVERRIDES:
            collection = override["collection"]
            hadith = override["hadith"]
            source = override["source"]
            if source == "fawaz":
                new_text = fetch_fawaz_arabic(collection, hadith, args.timeout)
            elif source == "hf":
                new_text = fetch_hf_arabic(collection, hadith, args.timeout)
            else:
                raise ValueError(f"Unsupported source: {source}")
            if not new_text:
                raise RuntimeError(f"No replacement text for {collection}:{hadith} from {source}")
            row = await conn.fetchrow(
                f"""
                SELECT hl.id AS localization_id, hl.body_text AS old_text
                FROM {SCHEMA}.hadith_items hi
                JOIN {SCHEMA}.hadith_localizations hl
                    ON hl.hadith_item_id = hi.id AND hl.language = 'ar'
                WHERE hi.collection_name = $1
                  AND hi.book_number = $2
                  AND hi.hadith_number = $3
                """,
                collection,
                override["book"],
                hadith,
            )
            if row is None:
                raise RuntimeError(f"Missing local row for {collection}/{override['book']}/{hadith}")
            applied.append(
                AppliedOverride(
                    collection=collection,
                    book=override["book"],
                    hadith=hadith,
                    source=source,
                    reason=override["reason"],
                    localization_id=row["localization_id"],
                    old_text=_clean(row["old_text"]),
                    new_text=new_text,
                )
            )
        backup = {
            "generated_at": time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime()),
            "applied": args.apply,
            "overrides": [asdict(item) | {"key": item.key} for item in applied],
        }
        with open(args.backup_out, "w", encoding="utf-8") as handle:
            json.dump(backup, handle, ensure_ascii=False, indent=2)
        if args.apply:
            async with conn.transaction():
                for item in applied:
                    await conn.execute(
                        f"""
                        UPDATE {SCHEMA}.hadith_localizations
                        SET body_text = $1, updated_at = NOW()
                        WHERE id = $2 AND language = 'ar'
                        """,
                        item.new_text,
                        item.localization_id,
                    )
        return {
            "applied": args.apply,
            "updated": len(applied) if args.apply else 0,
            "backup_out": args.backup_out,
            "overrides": [
                {"key": item.key, "source": item.source, "reason": item.reason}
                for item in applied
            ],
        }
    finally:
        await conn.close()


async def main() -> int:
    args = parse_args()
    if not args.dsn:
        raise SystemExit("--dsn or ISLAMIC_CONTENT_DATABASE__DSN is required")
    result = await apply_overrides(args)
    print(json.dumps(result, ensure_ascii=False, indent=2))
    return 0


if __name__ == "__main__":
    raise SystemExit(asyncio.run(main()))
