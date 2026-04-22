from __future__ import annotations

import html
import logging
import re
from typing import Any

from ..clients.hadith_cdn_client import HadithCdnClient
from ..config import HadithSettings
from ..domain.constants import HADITH_SOURCE_API
from ..domain.errors import UpstreamAPIError
from ..repositories.hadith_repository import HadithRepository
from ..repositories.sync_repository import SyncRepository

logger = logging.getLogger(__name__)
UNMAPPED_SECTION_TITLE = "Unmapped (CDN section gap)"


class HadithSyncService:
    def __init__(
        self,
        settings: HadithSettings,
        client: HadithCdnClient,
        repository: HadithRepository,
        sync_repository: SyncRepository,
    ) -> None:
        self.settings = settings
        self.client = client
        self.repository = repository
        self.sync_repository = sync_repository

    def is_configured(self) -> bool:
        return self.client.is_configured()

    def _clean_text(self, value: Any) -> str:
        text = str(value or "").strip()
        if not text:
            return ""
        text = re.sub(r"<[^>]+>", " ", text)
        text = html.unescape(text)
        return re.sub(r"\s+", " ", text).strip()

    def _find_edition(
        self,
        catalog: dict[str, Any],
        collection_name: str,
        language_prefix: str,
    ) -> dict[str, Any] | None:
        entry = catalog.get(collection_name) or {}
        editions = entry.get("collection") or []
        if not editions:
            return None
        target_name = f"{language_prefix}-{collection_name}"
        exact = next((item for item in editions if item.get("name") == target_name), None)
        if exact is not None:
            return exact
        return next(
            (item for item in editions if str(item.get("name") or "").startswith(f"{target_name}")),
            None,
        )

    def _normalize_sections(
        self,
        metadata: dict[str, Any],
    ) -> tuple[dict[str, str], dict[str, dict[str, Any]]]:
        raw_sections = metadata.get("sections") or metadata.get("section") or {}
        raw_details = metadata.get("section_details") or metadata.get("section_detail") or {}
        sections: dict[str, str] = {}
        details: dict[str, dict[str, Any]] = {}
        for key, value in raw_sections.items():
            section_number = str(key or "").strip()
            section_title = str(value or "").strip()
            if not section_number or (section_number == "0" and not section_title):
                continue
            detail = raw_details.get(key) or raw_details.get(section_number) or {}
            sections[section_number] = section_title
            details[section_number] = detail if isinstance(detail, dict) else {}
        return sections, details

    def _build_books(
        self,
        sections: dict[str, str],
        section_details: dict[str, dict[str, Any]],
    ) -> list[dict[str, Any]]:
        books: list[dict[str, Any]] = []
        for section_number in sorted(sections, key=lambda item: int(item) if item.isdigit() else item):
            detail = section_details.get(section_number) or {}
            start = detail.get("hadithnumber_first")
            end = detail.get("hadithnumber_last")
            count = None
            try:
                if start is not None and end is not None:
                    count = int(end) - int(start) + 1
            except (TypeError, ValueError):
                count = None
            books.append(
                {
                    "book_number": section_number,
                    "title": sections.get(section_number) or None,
                    "hadith_start_number": start,
                    "hadith_end_number": end,
                    "number_of_hadith": count,
                }
            )
        return books

    def _section_lookup(
        self,
        sections: dict[str, str],
        section_details: dict[str, dict[str, Any]],
    ) -> dict[int, tuple[str, str]]:
        mapping: dict[int, tuple[str, str]] = {}
        for section_number, title in sections.items():
            detail = section_details.get(section_number) or {}
            first = detail.get("hadithnumber_first")
            last = detail.get("hadithnumber_last")
            try:
                start = int(first)
                end = int(last)
            except (TypeError, ValueError):
                continue
            for hadith_number in range(start, end + 1):
                mapping[hadith_number] = (section_number, title)
        return mapping

    def _normalize_payload(
        self,
        *,
        collection_name: str,
        catalog_entry: dict[str, Any],
        english_payload: dict[str, Any],
        arabic_payload: dict[str, Any] | None,
    ) -> tuple[dict[str, Any], list[dict[str, Any]], list[dict[str, Any]]]:
        metadata = english_payload.get("metadata") or {}
        sections, section_details = self._normalize_sections(metadata)
        if not sections:
            raise UpstreamAPIError(f"{collection_name}: no section metadata found in CDN payload")

        books = self._build_books(sections, section_details)
        lookup = self._section_lookup(sections, section_details)
        english_hadiths = english_payload.get("hadiths") or []
        arabic_map = {
            int(item.get("hadithnumber")): self._clean_text(item.get("text"))
            for item in (arabic_payload or {}).get("hadiths", [])
            if item.get("hadithnumber") is not None
        }
        normalized_hadiths: list[dict[str, Any]] = []
        missing_sections: list[str] = []
        zero_book_hadiths: list[str] = []
        duplicate_hadith_numbers: list[str] = []
        seen_hadith_numbers: set[str] = set()
        for item in english_hadiths:
            hadith_number_raw = item.get("hadithnumber")
            try:
                hadith_number_int = int(hadith_number_raw)
            except (TypeError, ValueError):
                continue
            hadith_number = str(hadith_number_int)
            reference = item.get("reference") if isinstance(item.get("reference"), dict) else {}
            raw_section_number = reference.get("book")
            section_number = "" if raw_section_number is None else str(raw_section_number).strip()
            section_title = sections.get(section_number)
            if section_number == "0":
                section_title = UNMAPPED_SECTION_TITLE
                zero_book_hadiths.append(hadith_number)
            if not section_title:
                section_number, section_title = lookup.get(hadith_number_int, ("", ""))
            if not section_number or not section_title:
                missing_sections.append(hadith_number)
                continue
            if hadith_number in seen_hadith_numbers:
                duplicate_hadith_numbers.append(hadith_number)
                continue
            seen_hadith_numbers.add(hadith_number)
            normalized_hadiths.append(
                {
                    "source_api": HADITH_SOURCE_API,
                    "collection": collection_name,
                    "book_number": section_number,
                    "chapter_id": section_number,
                    "hadith_number": hadith_number,
                    "chapter_title": section_title,
                    "translation_text": self._clean_text(item.get("text")),
                    "arabic_text": arabic_map.get(hadith_number_int, ""),
                    "grades": {"en": item.get("grades") or []},
                }
            )

        if missing_sections:
            raise UpstreamAPIError(
                f"{collection_name}: {len(missing_sections)} hadiths missing section mapping"
            )
        if zero_book_hadiths and not any(book["book_number"] == "0" for book in books):
            books = [
                {
                    "book_number": "0",
                    "title": UNMAPPED_SECTION_TITLE,
                    "hadith_start_number": None,
                    "hadith_end_number": None,
                    "number_of_hadith": len(zero_book_hadiths),
                },
                *books,
            ]
            logger.warning(
                "Collection %s contains %d hadiths with CDN reference.book=0; grouped into synthetic section 0",
                collection_name,
                len(zero_book_hadiths),
            )
        if duplicate_hadith_numbers:
            logger.warning(
                "Collection %s contains %d duplicate hadithnumber values in CDN; keeping first occurrence per number",
                collection_name,
                len(duplicate_hadith_numbers),
            )

        # Nested chapter hierarchy (collection → book → chapter → hadith) is
        # populated out-of-band by scripts/sync_hadith_chapters_sunnah.py from
        # sunnah.com. nawawi's 40 Hadith is flat on sunnah.com (no book/chapter
        # layer), so we mark it explicitly; every other collection we sync is
        # known to have real chapter data.
        has_chapters = collection_name != "nawawi"
        collection = {
            "name": collection_name,
            "title": metadata.get("name") or catalog_entry.get("name") or collection_name,
            "short_intro": catalog_entry.get("comments") or "",
            "has_books": True,
            "has_chapters": has_chapters,
            "total_books": len(books),
            "total_hadith": len(normalized_hadiths),
            "source_api": HADITH_SOURCE_API,
        }
        return collection, books, normalized_hadiths

    async def sync(self) -> dict[str, int]:
        catalog = await self.client.get_editions()
        await self.sync_repository.save_snapshot(
            source_name="hadith",
            snapshot_kind="editions_catalog",
            snapshot_key="all",
            request_path="editions",
            request_params=None,
            response_payload=catalog,
        )
        wanted = {item.lower() for item in self.settings.sync_collections}
        synced_collections = 0
        synced_books = 0
        synced_hadiths = 0

        collection_names = sorted(catalog)
        for name in collection_names:
            if wanted and name.lower() not in wanted:
                continue
            english_edition = self._find_edition(catalog, name, self.settings.default_language)
            if english_edition is None:
                logger.warning("Skipping %s: no %s edition found", name, self.settings.default_language)
                continue
            arabic_edition = self._find_edition(catalog, name, self.settings.arabic_language)

            english_payload = await self.client.get_edition(str(english_edition["name"]))
            await self.sync_repository.save_snapshot(
                source_name="hadith",
                snapshot_kind="edition",
                snapshot_key=str(english_edition["name"]),
                request_path=f"editions/{english_edition['name']}",
                request_params=None,
                response_payload=english_payload,
            )
            arabic_payload = None
            if arabic_edition is not None:
                arabic_payload = await self.client.get_edition(str(arabic_edition["name"]))
                await self.sync_repository.save_snapshot(
                    source_name="hadith",
                    snapshot_kind="edition",
                    snapshot_key=str(arabic_edition["name"]),
                    request_path=f"editions/{arabic_edition['name']}",
                    request_params=None,
                    response_payload=arabic_payload,
                )
            catalog_entry = catalog.get(name) or {}
            collection, books, hadiths = self._normalize_payload(
                collection_name=name,
                catalog_entry=catalog_entry,
                english_payload=english_payload,
                arabic_payload=arabic_payload,
            )
            await self.repository.replace_collection(collection, books, hadiths)
            logger.info(
                "Synced hadith collection %s via CDN: books=%d hadiths=%d",
                name,
                len(books),
                len(hadiths),
            )
            synced_collections += 1
            synced_books += len(books)
            synced_hadiths += len(hadiths)

        return {
            "collections": synced_collections,
            "books": synced_books,
            "hadith_items": synced_hadiths,
        }
