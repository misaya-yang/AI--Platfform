from __future__ import annotations

import csv
import json
import logging
from collections import Counter
from pathlib import Path
from typing import Any

from ..repositories.dua_repository import DuaRepository

logger = logging.getLogger(__name__)

# Column name mapping: CSV header variants -> canonical key
_COLUMN_MAP: dict[str, str] = {
    "dua_id": "dua_id",
    "category": "category",
    "title": "title",
    "arabic_text": "arabic_text",
    "transliteration": "transliteration",
    "english_meaning": "english_meaning",
    "urdu_meaning": "urdu_meaning",
    "source": "source",
    "reference": "reference",
    "authenticity": "authenticity",
    "occasion": "occasion",
    "verification_status": "verification_status",
    "data_source": "data_source",
}


def _normalize_row(raw: dict[str, str]) -> dict[str, str]:
    result: dict[str, str] = {}
    for raw_key, value in raw.items():
        canonical = _COLUMN_MAP.get(raw_key.strip().lower().replace(" ", "_"))
        if canonical:
            result[canonical] = (value or "").strip()
    return result


def _load_csv(path: Path) -> list[dict[str, str]]:
    rows: list[dict[str, str]] = []
    with path.open(encoding="utf-8-sig") as fh:
        reader = csv.DictReader(fh)
        for raw in reader:
            row = _normalize_row(raw)
            if row.get("dua_id"):
                rows.append(row)
    return rows


def _load_json(path: Path) -> list[dict[str, str]]:
    data = json.loads(path.read_text(encoding="utf-8-sig"))
    if isinstance(data, list):
        return [_normalize_row(item) for item in data if isinstance(item, dict)]
    return []


class DuaSyncService:
    def __init__(self, data_path: str, repository: DuaRepository) -> None:
        self.data_path = data_path
        self.repository = repository

    def is_configured(self) -> bool:
        p = Path(self.data_path)
        return p.is_file()

    async def sync(self) -> dict[str, Any]:
        p = Path(self.data_path)
        if not p.is_file():
            raise FileNotFoundError(f"Dua data file not found: {p}")

        if p.suffix.lower() == ".json":
            rows = _load_json(p)
        else:
            rows = _load_csv(p)

        if not rows:
            raise ValueError(f"No valid dua rows found in {p}")

        category_counts: Counter[str] = Counter()
        for row in rows:
            category_counts[row.get("category", "General")] += 1

        for category, count in sorted(category_counts.items()):
            await self.repository.upsert_category(category, count)

        for row in rows:
            await self.repository.upsert_item(row)

        logger.info("Dua sync complete: %d items, %d categories", len(rows), len(category_counts))
        return {
            "dua_items": len(rows),
            "dua_categories": len(category_counts),
        }
