from __future__ import annotations

import html
import re
from typing import Any

from ..clients.sunnah_client import SunnahClient
from ..config import HadithSettings
from ..domain.constants import SUNNAH_SOURCE_API
from ..repositories.hadith_repository import HadithRepository
from ..repositories.sync_repository import SyncRepository


class HadithSyncService:
    def __init__(
        self,
        settings: HadithSettings,
        client: SunnahClient,
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

    def _pagination_total_pages(self, pagination: dict[str, Any] | None) -> int | None:
        if not pagination:
            return None
        for key in ("totalPages", "total_pages", "lastPage", "last_page"):
            value = pagination.get(key)
            if value is None:
                continue
            try:
                return int(value)
            except (TypeError, ValueError):
                continue
        return None

    def _normalize_collection(self, item: dict[str, Any]) -> dict[str, Any]:
        entries = item.get("collection") or []
        english = next((entry for entry in entries if entry.get("lang") == "en"), entries[0] if entries else {})
        return {
            "name": item.get("name"),
            "title": english.get("title") or item.get("name"),
            "short_intro": english.get("shortIntro"),
            "has_books": item.get("hasBooks"),
            "has_chapters": item.get("hasChapters"),
            "total_books": item.get("totalBooks") or english.get("totalBooks"),
            "total_hadith": item.get("totalHadith") or english.get("totalHadith"),
        }

    def _normalize_book(self, item: dict[str, Any]) -> dict[str, Any]:
        entries = item.get("book") or []
        english = next((entry for entry in entries if entry.get("lang") == "en"), entries[0] if entries else {})
        return {
            "book_number": str(item.get("bookNumber") or ""),
            "title": english.get("name") or english.get("title"),
            "hadith_start_number": item.get("hadithStartNumber"),
            "hadith_end_number": item.get("hadithEndNumber"),
            "number_of_hadith": item.get("numberOfHadith"),
        }

    def _normalize_detail(self, item: dict[str, Any]) -> dict[str, Any]:
        entries = item.get("hadith") or []
        english = next((entry for entry in entries if entry.get("lang") == "en"), {})
        arabic = next((entry for entry in entries if entry.get("lang") == "ar"), {})
        return {
            "source_api": SUNNAH_SOURCE_API,
            "collection": item.get("collection"),
            "book_number": str(item.get("bookNumber") or ""),
            "chapter_id": str(item.get("chapterId") or ""),
            "hadith_number": str(item.get("hadithNumber") or ""),
            "chapter_title": english.get("chapterTitle") or arabic.get("chapterTitle"),
            "translation_text": self._clean_text(english.get("body")),
            "arabic_text": self._clean_text(arabic.get("body")),
            "grades": {
                "en": english.get("grades") or [],
                "ar": arabic.get("grades") or [],
            },
        }

    async def sync(self) -> dict[str, int]:
        page = 1
        collections_raw: list[dict[str, Any]] = []
        while True:
            payload = await self.client.get_collections(page=page, limit=self.settings.page_size)
            await self.sync_repository.save_snapshot(
                source_name="hadith",
                snapshot_kind="collections_page",
                snapshot_key=str(page),
                request_path="collections",
                request_params={"page": page, "limit": self.settings.page_size},
                response_payload=payload,
            )
            data = payload.get("data") or []
            if not data:
                break
            collections_raw.extend(data)
            total_pages = self._pagination_total_pages(payload.get("pagination") or {})
            if total_pages is not None and page >= total_pages:
                break
            if len(data) < self.settings.page_size:
                break
            page += 1

        normalized_collections = [self._normalize_collection(item) for item in collections_raw]
        await self.repository.upsert_collections(normalized_collections)
        wanted = {item.lower() for item in self.settings.sync_collections}
        synced_collections = 0
        synced_books = 0
        synced_hadiths = 0

        for collection in normalized_collections:
            name = str(collection.get("name") or "")
            if wanted and name.lower() not in wanted:
                continue
            synced_collections += 1
            collection_meta = await self.client.get_collection(name)
            await self.sync_repository.save_snapshot(
                source_name="hadith",
                snapshot_kind="collection_meta",
                snapshot_key=name,
                request_path=f"collections/{name}",
                request_params=None,
                response_payload=collection_meta,
            )

            book_page = 1
            books: list[dict[str, Any]] = []
            while True:
                payload = await self.client.get_books(name, page=book_page, limit=self.settings.page_size)
                await self.sync_repository.save_snapshot(
                    source_name="hadith",
                    snapshot_kind="books_page",
                    snapshot_key=f"{name}:{book_page}",
                    request_path=f"collections/{name}/books",
                    request_params={"page": book_page, "limit": self.settings.page_size},
                    response_payload=payload,
                )
                data = payload.get("data") or []
                if not data:
                    break
                books.extend(self._normalize_book(item) for item in data)
                total_pages = self._pagination_total_pages(payload.get("pagination") or {})
                if total_pages is not None and book_page >= total_pages:
                    break
                if len(data) < self.settings.page_size:
                    break
                book_page += 1

            await self.repository.upsert_books(name, books)

            for book in books:
                synced_books += 1
                hadith_page = 1
                while True:
                    payload = await self.client.get_book_hadiths(
                        name,
                        str(book["book_number"]),
                        page=hadith_page,
                        limit=self.settings.page_size,
                    )
                    await self.sync_repository.save_snapshot(
                        source_name="hadith",
                        snapshot_kind="hadith_page",
                        snapshot_key=f"{name}:{book['book_number']}:{hadith_page}",
                        request_path=f"collections/{name}/books/{book['book_number']}/hadiths",
                        request_params={"page": hadith_page, "limit": self.settings.page_size},
                        response_payload=payload,
                    )
                    items = payload.get("data") or []
                    if not items:
                        break
                    for summary in items:
                        hadith_number = str(summary.get("hadithNumber") or "").strip()
                        if not hadith_number:
                            continue
                        detail_payload = await self.client.get_hadith_detail(name, hadith_number)
                        await self.sync_repository.save_snapshot(
                            source_name="hadith",
                            snapshot_kind="hadith_detail",
                            snapshot_key=f"{name}:{hadith_number}",
                            request_path=f"collections/{name}/hadiths/{hadith_number}",
                            request_params=None,
                            response_payload=detail_payload,
                        )
                        await self.repository.upsert_detail(
                            self._normalize_detail(detail_payload.get("data") or detail_payload)
                        )
                        synced_hadiths += 1
                    total_pages = self._pagination_total_pages(payload.get("pagination") or {})
                    if total_pages is not None and hadith_page >= total_pages:
                        break
                    if len(items) < self.settings.page_size:
                        break
                    hadith_page += 1

        return {
            "collections": synced_collections,
            "books": synced_books,
            "hadith_items": synced_hadiths,
        }
