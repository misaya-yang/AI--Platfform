from __future__ import annotations

import asyncio
import html
import re
from collections import defaultdict
from typing import Any

from ..clients.quran_foundation_client import QuranFoundationClient
from ..config import QuranSettings
from ..domain.constants import QURAN_SOURCE_API
from ..repositories.quran_repository import QuranRepository
from ..repositories.sync_repository import SyncRepository


class QuranSyncService:
    _VERSE_AUDIO_BASE_URL = "https://verses.quran.foundation/"

    def __init__(
        self,
        settings: QuranSettings,
        client: QuranFoundationClient,
        repository: QuranRepository,
        sync_repository: SyncRepository,
    ) -> None:
        self.settings = settings
        self.client = client
        self.repository = repository
        self.sync_repository = sync_repository

    def is_configured(self) -> bool:
        return self.client.is_configured()

    def _clean_text(self, value: Any) -> str:
        if isinstance(value, dict):
            value = value.get("text")
        elif isinstance(value, (list, tuple)):
            return " ".join(
                part for item in value if (part := self._clean_text(item))
            ).strip()
        text = str(value or "").strip()
        if not text:
            return ""
        text = re.sub(r"<sup\b[^>]*>\s*\d+\s*</sup>", "", text, flags=re.IGNORECASE)
        text = re.sub(r"<[^>]+>", " ", text)
        text = html.unescape(text)
        return re.sub(r"\s+", " ", text).strip()

    _BARE_DOMAIN_RE = re.compile(r"^[A-Za-z0-9-]+(?:\.[A-Za-z0-9-]+)+/")

    def _normalize_audio_url(self, value: Any) -> str | None:
        url = str(value or "").strip()
        if not url:
            return None
        if url.startswith(("http://", "https://")):
            return url
        if url.startswith("//"):
            return f"https:{url}"
        stripped = url.lstrip("/")
        if self._BARE_DOMAIN_RE.match(stripped):
            return f"https://{stripped}"
        return f"{self._VERSE_AUDIO_BASE_URL}{stripped}"

    def _normalize_word(self, word: dict[str, Any]) -> dict[str, Any] | None:
        if word.get("char_type_name") != "word":
            return None
        translation = word.get("translation") or {}
        transliteration = word.get("transliteration") or {}
        return {
            "position": word.get("position"),
            "arabic": word.get("text_uthmani") or word.get("text_imlaei") or word.get("text"),
            "arabic_simple": word.get("text_uthmani_simple"),
            "transliteration": self._clean_text(
                transliteration.get("text") or word.get("transliteration")
            ),
            "translation": self._clean_text(translation.get("text") or word.get("translation")),
            "char_type": word.get("char_type_name"),
            "audio_url": self._normalize_audio_url(word.get("audio_url")),
        }

    def _normalize_ayah_base(self, verse: dict[str, Any]) -> dict[str, Any]:
        verse_key = str(verse.get("verse_key") or "")
        chapter_part, _, ayah_part = verse_key.partition(":")
        words = [
            normalized
            for item in verse.get("words") or []
            if (normalized := self._normalize_word(item)) is not None
        ]
        transliteration_text = " ".join(
            word["transliteration"] for word in words if word.get("transliteration")
        ).strip()
        return {
            "source_api": QURAN_SOURCE_API,
            "source_type": "quran",
            "verse_key": verse_key,
            "surah_number": int(chapter_part) if chapter_part.isdigit() else None,
            "ayah_number": int(ayah_part) if ayah_part.isdigit() else None,
            "juz_number": verse.get("juz_number"),
            "hizb_number": verse.get("hizb_number"),
            "rub_number": verse.get("rub_number") or verse.get("rub_el_hizb_number"),
            "page_number": verse.get("page_number"),
            "arabic_text": (
                verse.get("text_uthmani") or verse.get("text_uthmani_simple") or ""
            ).strip(),
            "transliteration_text": transliteration_text,
            "words": words,
        }

    def _normalize_translation_rows(
        self,
        verse: dict[str, Any],
    ) -> dict[int, dict[str, Any]]:
        verse_key = str(verse.get("verse_key") or "").strip()
        rows: dict[int, dict[str, Any]] = {}
        if not verse_key:
            return rows
        for item in verse.get("translations") or []:
            translation_id = item.get("resource_id") or item.get("id")
            if translation_id is None:
                continue
            try:
                resource_id = int(translation_id)
            except (TypeError, ValueError):
                continue
            rows[resource_id] = {
                "verse_key": verse_key,
                "translation_text": self._clean_text(item.get("text")),
            }
        return rows

    def _normalize_audio_row(self, verse: dict[str, Any]) -> dict[str, Any] | None:
        verse_key = str(verse.get("verse_key") or "").strip()
        if not verse_key:
            return None
        audio = verse.get("audio") or {}
        return {
            "verse_key": verse_key,
            "audio_url": self._normalize_audio_url(audio.get("url") or audio.get("audio_url")),
        }

    def _build_triplet_ranges(self, chapter_id: int, ayahs: list[dict[str, Any]]) -> list[dict[str, Any]]:
        ranges = []
        for start in range(0, len(ayahs), 3):
            chunk = ayahs[start : start + 3]
            if not chunk:
                continue
            start_ayah = int(chunk[0]["ayah_number"])
            end_ayah = int(chunk[-1]["ayah_number"])
            ranges.append(
                {
                    "block_id": f"quran:{chapter_id}:{start_ayah}-{end_ayah}",
                    "chapter_id": chapter_id,
                    "start_ayah_number": start_ayah,
                    "end_ayah_number": end_ayah,
                    "verse_keys": [item["verse_key"] for item in chunk],
                }
            )
        return ranges

    def _extract_audio_url(self, payload: dict[str, Any]) -> str | None:
        stack: list[Any] = [payload]
        while stack:
            current = stack.pop()
            if isinstance(current, dict):
                for key in ("audio_url", "url"):
                    if current.get(key):
                        return self._normalize_audio_url(current[key])
                stack.extend(current.values())
            elif isinstance(current, list):
                stack.extend(current)
        return None

    def _normalize_segment(self, value: Any) -> dict[str, int] | None:
        if not isinstance(value, (list, tuple)) or len(value) < 3:
            return None
        try:
            return {
                "word_index": int(value[0]),
                "start_ms": int(float(value[1])),
                "end_ms": int(float(value[2])),
            }
        except (TypeError, ValueError):
            return None

    def _normalize_audio_timings(
        self,
        chapter_id: int,
        payload: dict[str, Any],
    ) -> list[dict[str, Any]]:
        audio_file = payload.get("audio_file") or {}
        timings = []
        for item in audio_file.get("timestamps") or audio_file.get("verse_timings") or []:
            verse_key = str(item.get("verse_key") or "").strip()
            if not verse_key:
                continue
            segments = [
                normalized
                for raw_segment in item.get("segments") or []
                if (normalized := self._normalize_segment(raw_segment)) is not None
            ]
            timings.append(
                {
                    "chapter_id": chapter_id,
                    "verse_key": verse_key,
                    "timestamp_from_ms": int(float(item.get("timestamp_from") or 0)),
                    "timestamp_to_ms": int(float(item.get("timestamp_to") or 0)),
                    "duration_ms": int(float(item.get("duration") or 0)),
                    "segments": segments,
                }
            )
        return timings

    def _chunk_ids(self, values: list[int], size: int) -> list[list[int]]:
        chunk_size = max(size, 1)
        return [values[index : index + chunk_size] for index in range(0, len(values), chunk_size)]

    async def _run_limited(
        self,
        items: list[Any],
        *,
        concurrency: int,
        runner,
    ) -> list[Any]:
        semaphore = asyncio.Semaphore(max(concurrency, 1))

        async def _wrapped(item: Any) -> Any:
            async with semaphore:
                return await runner(item)

        return await asyncio.gather(*(_wrapped(item) for item in items))

    def _select_translation_ids(self, payload: dict[str, Any]) -> list[int]:
        available = [
            int(item.get("id") or item.get("resource_id"))
            for item in payload.get("translations") or []
            if item.get("id") or item.get("resource_id")
        ]
        if self.settings.sync_all_translations or not self.settings.translation_ids:
            selected = available
        else:
            allowed = set(self.settings.translation_ids)
            selected = [item for item in available if item in allowed]
        if self.settings.default_translation_id not in selected and self.settings.default_translation_id in available:
            selected.append(self.settings.default_translation_id)
        return sorted(set(selected))

    def _select_recitation_ids(self, payload: dict[str, Any]) -> list[int]:
        available = [
            int(item.get("id") or item.get("reciter_id"))
            for item in payload.get("recitations") or []
            if item.get("id") or item.get("reciter_id")
        ]
        if self.settings.sync_all_recitations or not self.settings.recitation_ids:
            selected = available
        else:
            allowed = set(self.settings.recitation_ids)
            selected = [item for item in available if item in allowed]
        if self.settings.default_recitation_id not in selected and self.settings.default_recitation_id in available:
            selected.append(self.settings.default_recitation_id)
        return sorted(set(selected))

    async def _sync_base_chapter(self, chapter_id: int) -> tuple[list[dict[str, Any]], int]:
        page = 1
        normalized_ayahs: list[dict[str, Any]] = []
        request_count = 0
        while True:
            payload = await self.client.get_chapter_verses(
                chapter_id,
                translation_ids=None,
                recitation_id=None,
                word_fields=self.settings.word_fields,
                page=page,
                per_page=self.settings.page_size,
                include_words=True,
            )
            request_count += 1
            await self.sync_repository.save_snapshot(
                source_name="quran",
                snapshot_kind="chapter_page_base",
                snapshot_key=f"{chapter_id}:{page}",
                request_path=f"verses/by_chapter/{chapter_id}",
                request_params={
                    "page": page,
                    "per_page": self.settings.page_size,
                    "words": True,
                    "fields": "text_uthmani",
                },
                response_payload=payload,
            )
            verses = payload.get("verses") or []
            if not verses:
                break
            normalized_ayahs.extend(self._normalize_ayah_base(item) for item in verses)
            if len(verses) < self.settings.page_size:
                break
            page += 1
        await self.repository.upsert_ayah_bases(
            chapter_id=chapter_id,
            placeholder_translation_id=self.settings.default_translation_id,
            placeholder_recitation_id=self.settings.default_recitation_id,
            ayahs=normalized_ayahs,
        )
        await self.repository.upsert_triplet_ranges(
            chapter_id=chapter_id,
            ranges=self._build_triplet_ranges(chapter_id, normalized_ayahs),
        )
        return normalized_ayahs, request_count

    async def _sync_translation_batch(
        self,
        chapter_id: int,
        translation_batch: list[int],
    ) -> int:
        page = 1
        request_count = 0
        chapter_rows: dict[int, list[dict[str, Any]]] = defaultdict(list)
        while True:
            payload = await self.client.get_chapter_verses(
                chapter_id,
                translation_ids=translation_batch,
                recitation_id=None,
                word_fields=self.settings.word_fields,
                page=page,
                per_page=self.settings.page_size,
                include_words=False,
            )
            request_count += 1
            verses = payload.get("verses") or []
            if not verses:
                break
            for verse in verses:
                normalized_rows = self._normalize_translation_rows(verse)
                for translation_id in translation_batch:
                    row = normalized_rows.get(
                        translation_id,
                        {"verse_key": verse.get("verse_key"), "translation_text": ""},
                    )
                    chapter_rows[translation_id].append(row)
            if len(verses) < self.settings.page_size:
                break
            page += 1
        for translation_id, rows in chapter_rows.items():
            await self.repository.upsert_ayah_translations(
                translation_id=translation_id,
                items=rows,
            )
        return request_count

    async def _sync_recitation(
        self,
        chapter_id: int,
        recitation_id: int,
    ) -> tuple[int, int]:
        audio_payload = await self.client.get_chapter_audio(
            chapter_id,
            recitation_id=recitation_id,
            segments=True,
        )
        await self.sync_repository.save_snapshot(
            source_name="quran",
            snapshot_kind="chapter_audio",
            snapshot_key=f"{recitation_id}:{chapter_id}",
            request_path=f"chapter_recitations/{recitation_id}/{chapter_id}",
            request_params={"segments": True},
            response_payload=audio_payload,
        )
        normalized_timings = self._normalize_audio_timings(chapter_id, audio_payload)
        await self.repository.upsert_chapter_audio_track(
            chapter_id,
            recitation_id=recitation_id,
            audio_url=self._extract_audio_url(audio_payload),
        )
        await self.repository.upsert_chapter_audio_timings(
            chapter_id=chapter_id,
            recitation_id=recitation_id,
            timings=normalized_timings,
        )

        page = 1
        request_count = 0
        audio_rows: list[dict[str, Any]] = []
        while True:
            payload = await self.client.get_chapter_verses(
                chapter_id,
                translation_ids=None,
                recitation_id=recitation_id,
                word_fields=self.settings.word_fields,
                page=page,
                per_page=self.settings.page_size,
                include_words=False,
            )
            request_count += 1
            verses = payload.get("verses") or []
            if not verses:
                break
            audio_rows.extend(
                row
                for verse in verses
                if (row := self._normalize_audio_row(verse)) is not None
            )
            if len(verses) < self.settings.page_size:
                break
            page += 1
        await self.repository.upsert_ayah_audio(recitation_id=recitation_id, items=audio_rows)
        return request_count, len(normalized_timings)

    async def sync(self) -> dict[str, int]:
        chapter_payload = await self.client.get_chapters()
        await self.sync_repository.save_snapshot(
            source_name="quran",
            snapshot_kind="chapters",
            snapshot_key="all",
            request_path="chapters",
            request_params=None,
            response_payload=chapter_payload,
        )
        chapters = [
            {
                "chapter_id": raw.get("id"),
                "name_simple": raw.get("name_simple"),
                "name_complex": raw.get("name_complex"),
                "name_arabic": raw.get("name_arabic"),
                "translated_name": ((raw.get("translated_name") or {}).get("name")),
                "revelation_place": raw.get("revelation_place"),
                "verses_count": raw.get("verses_count"),
            }
            for raw in chapter_payload.get("chapters") or []
        ]
        await self.repository.upsert_chapters(chapters)

        translations_payload = await self.client.get_translations()
        await self.sync_repository.save_snapshot(
            source_name="quran",
            snapshot_kind="translations",
            snapshot_key="all",
            request_path="resources/translations",
            request_params={"language": "en"},
            response_payload=translations_payload,
        )
        await self.repository.upsert_translations(translations_payload.get("translations") or [])
        translation_ids = self._select_translation_ids(translations_payload)

        recitations_payload = await self.client.get_recitations()
        await self.sync_repository.save_snapshot(
            source_name="quran",
            snapshot_kind="recitations",
            snapshot_key="all",
            request_path="resources/recitations",
            request_params=None,
            response_payload=recitations_payload,
        )
        await self.repository.upsert_recitations(recitations_payload.get("recitations") or [])
        recitation_ids = self._select_recitation_ids(recitations_payload)

        ayah_count = 0
        audio_timing_count = 0
        triplet_count = 0
        request_count = 3

        for chapter in chapters:
            chapter_id = int(chapter["chapter_id"])
            normalized_ayahs, base_requests = await self._sync_base_chapter(chapter_id)
            request_count += base_requests
            ayah_count += len(normalized_ayahs)
            triplet_count += (len(normalized_ayahs) + 2) // 3

            translation_batches = self._chunk_ids(
                translation_ids,
                self.settings.translation_batch_size,
            )
            translation_results = await self._run_limited(
                translation_batches,
                concurrency=self.settings.translation_concurrency,
                runner=lambda batch: self._sync_translation_batch(chapter_id, batch),
            )
            request_count += sum(int(value) for value in translation_results)

            recitation_results = await self._run_limited(
                recitation_ids,
                concurrency=self.settings.recitation_concurrency,
                runner=lambda recitation_id: self._sync_recitation(chapter_id, recitation_id),
            )
            request_count += sum(int(requests) + 1 for requests, _ in recitation_results)
            audio_timing_count += sum(int(timing_rows) for _, timing_rows in recitation_results)

        return {
            "chapters": len(chapters),
            "ayahs": ayah_count,
            "audio_timings": audio_timing_count,
            "triplet_blocks": triplet_count,
            "translations_synced": len(translation_ids),
            "recitations_synced": len(recitation_ids),
            "requests": request_count,
        }
