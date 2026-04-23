from __future__ import annotations

from datetime import datetime, timezone
from typing import Any, Awaitable, Callable

from ..cache import RedisCache
from ..config import CacheSettings, QuranSettings
from ..domain.constants import QURAN_SOURCE_API
from ..domain.errors import NotReadyError
from ..domain.juz_data import JUZ_NAMES, TOTAL_JUZ_COUNT
from ..domain.sajdah_data import SAJDAH_POINTS, TOTAL_SAJDAH_COUNT
from ..repositories.quran_repository import QuranRepository

JUZ_SOURCE_API = f"{QURAN_SOURCE_API}+internal"


class QuranQueryService:
    def __init__(
        self,
        settings: QuranSettings,
        cache_settings: CacheSettings,
        repository: QuranRepository,
        cache: RedisCache,
    ) -> None:
        self.settings = settings
        self.cache_settings = cache_settings
        self.repository = repository
        self.cache = cache

    async def _cached(
        self,
        key: str,
        ttl: int,
        loader: Callable[[], Awaitable[dict[str, Any]]],
    ) -> dict[str, Any]:
        cached = await self.cache.get_json(key)
        if cached is not None:
            return cached
        payload = await loader()
        await self.cache.set_json(key, payload, ttl)
        return payload

    async def invalidate(self) -> None:
        await self.cache.clear_prefix("quran:")

    async def _resolve_translation_id(self, translation_id: int | None) -> int:
        resolved = translation_id or self.settings.default_translation_id
        if await self.repository.has_translation(resolved):
            return resolved
        raise NotReadyError(f"Translation {resolved} has not been bootstrapped yet")

    async def _resolve_recitation_id(self, recitation_id: int | None) -> int:
        resolved = recitation_id or self.settings.default_recitation_id
        if await self.repository.has_recitation(resolved):
            return resolved
        raise NotReadyError(f"Recitation {resolved} has not been bootstrapped yet")

    def _build_triplets(
        self,
        chapter_id: int,
        ayahs: list[dict[str, Any]],
    ) -> list[dict[str, Any]]:
        blocks = []
        for start in range(0, len(ayahs), 3):
            chunk = ayahs[start : start + 3]
            if not chunk:
                continue
            ref = f"{chapter_id}:{chunk[0]['ayah_number']}-{chunk[-1]['ayah_number']}"
            translation_id = (chunk[0].get("audio") or {}).get("translation_id")
            recitation_id = (chunk[0].get("audio") or {}).get("recitation_id")
            blocks.append(
                {
                    "block_id": f"quran:{ref}:t{translation_id}:r{recitation_id}",
                    "ref": ref,
                    "chapter_id": chapter_id,
                    "group_size": len(chunk),
                    "verse_keys": [item["verse_key"] for item in chunk],
                    "arabic_text": "\n".join(item["arabic_text"] for item in chunk),
                    "transliteration_text": "\n".join(
                        item.get("transliteration_text") or "" for item in chunk
                    ).strip(),
                    "translation_text": "\n".join(
                        item.get("translation_text") or "" for item in chunk
                    ).strip(),
                    "audio_urls": [
                        {
                            "verse_key": item["verse_key"],
                            "url": ((item.get("audio") or {}).get("url")),
                        }
                        for item in chunk
                    ],
                    "children": chunk,
                }
            )
        return blocks

    def _ensure_ayah_ready(
        self,
        ayah: dict[str, Any],
        *,
        translation_id: int,
        recitation_id: int,
    ) -> None:
        if not (ayah.get("translation_text") or "").strip():
            raise NotReadyError(
                f"Translation {translation_id} has not been fully bootstrapped for {ayah['verse_key']}"
            )
        audio = ayah.get("audio") or {}
        if not (audio.get("url") or "").strip():
            raise NotReadyError(
                f"Recitation {recitation_id} has not been fully bootstrapped for {ayah['verse_key']}"
            )

    def _ensure_chapter_ready(
        self,
        ayahs: list[dict[str, Any]],
        *,
        chapter_id: int,
        translation_id: int,
        recitation_id: int,
    ) -> None:
        if not ayahs:
            raise NotReadyError(f"No Quran ayah data found for chapter {chapter_id}")
        for ayah in ayahs:
            self._ensure_ayah_ready(
                ayah,
                translation_id=translation_id,
                recitation_id=recitation_id,
            )

    async def _build_chapter_audio_text_payload(
        self,
        chapter_id: int,
        *,
        translation_id: int,
        recitation_id: int,
        screen: str | None = None,
    ) -> dict[str, Any]:
        ayahs = await self.repository.get_chapter_ayahs(
            chapter_id,
            translation_id=translation_id,
            recitation_id=recitation_id,
        )
        self._ensure_chapter_ready(
            ayahs,
            chapter_id=chapter_id,
            translation_id=translation_id,
            recitation_id=recitation_id,
        )
        chapter_audio = await self.repository.get_chapter_audio(
            chapter_id,
            recitation_id=recitation_id,
        )
        payload = {
            "generated_at": datetime.now(timezone.utc).isoformat(),
            "chapter_id": chapter_id,
            "translation_id": translation_id,
            "recitation_id": recitation_id,
            "chapter_audio": chapter_audio,
            "ayahs": ayahs,
        }
        if screen and chapter_audio is None:
            raise NotReadyError(
                f"No chapter audio found for chapter {chapter_id} and recitation {recitation_id}"
            )
        if screen:
            payload["screen"] = screen
        else:
            payload["source_api"] = QURAN_SOURCE_API
        return payload

    async def get_chapters(self) -> dict[str, Any]:
        async def _load() -> dict[str, Any]:
            return {
                "generated_at": datetime.now(timezone.utc).isoformat(),
                "source_api": QURAN_SOURCE_API,
                "chapters": await self.repository.get_chapters(),
            }

        return await self._cached("quran:chapters", self.cache_settings.ttl_seconds, _load)

    async def get_translations(self) -> dict[str, Any]:
        async def _load() -> dict[str, Any]:
            return {
                "generated_at": datetime.now(timezone.utc).isoformat(),
                "source_api": QURAN_SOURCE_API,
                "translations": await self.repository.get_translations(),
                "synced_translation_ids": await self.repository.get_synced_translation_ids(),
            }

        return await self._cached(
            "quran:resources:translations",
            self.cache_settings.meta_ttl_seconds,
            _load,
        )

    async def get_recitations(self) -> dict[str, Any]:
        async def _load() -> dict[str, Any]:
            return {
                "generated_at": datetime.now(timezone.utc).isoformat(),
                "source_api": QURAN_SOURCE_API,
                "recitations": await self.repository.get_recitations(),
                "synced_recitation_ids": await self.repository.get_synced_recitation_ids(),
            }

        return await self._cached(
            "quran:resources:recitations",
            self.cache_settings.meta_ttl_seconds,
            _load,
        )

    async def get_chapter_ayahs(
        self,
        chapter_id: int,
        *,
        translation_id: int | None = None,
        recitation_id: int | None = None,
    ) -> dict[str, Any]:
        resolved_translation_id = await self._resolve_translation_id(translation_id)
        resolved_recitation_id = await self._resolve_recitation_id(recitation_id)

        async def _load() -> dict[str, Any]:
            return await self._build_chapter_audio_text_payload(
                chapter_id,
                translation_id=resolved_translation_id,
                recitation_id=resolved_recitation_id,
            )

        return await self._cached(
            f"quran:ayahs:{chapter_id}:t{resolved_translation_id}:r{resolved_recitation_id}",
            self.cache_settings.ttl_seconds,
            _load,
        )

    async def get_ayah_detail(
        self,
        verse_key: str,
        *,
        translation_id: int | None = None,
        recitation_id: int | None = None,
    ) -> dict[str, Any]:
        resolved_translation_id = await self._resolve_translation_id(translation_id)
        resolved_recitation_id = await self._resolve_recitation_id(recitation_id)

        async def _load() -> dict[str, Any]:
            ayah = await self.repository.get_ayah(
                verse_key,
                translation_id=resolved_translation_id,
                recitation_id=resolved_recitation_id,
            )
            if ayah is None:
                raise NotReadyError(f"No Quran ayah data found for {verse_key}")
            self._ensure_ayah_ready(
                ayah,
                translation_id=resolved_translation_id,
                recitation_id=resolved_recitation_id,
            )
            chapter_audio = await self.repository.get_chapter_audio(
                ayah["surah_number"],
                recitation_id=resolved_recitation_id,
            )
            return {
                "generated_at": datetime.now(timezone.utc).isoformat(),
                "screen": "quran_ayah_detail",
                "translation_id": resolved_translation_id,
                "recitation_id": resolved_recitation_id,
                "chapter_audio": chapter_audio,
                "ayah": ayah,
            }

        return await self._cached(
            f"quran:ayah:{verse_key}:t{resolved_translation_id}:r{resolved_recitation_id}",
            self.cache_settings.ttl_seconds,
            _load,
        )

    async def get_ayah_minimal(
        self,
        verse_key: str,
        *,
        translation_id: int | None = None,
        recitation_id: int | None = None,
    ) -> dict[str, Any]:
        resolved_translation_id = await self._resolve_translation_id(translation_id)
        resolved_recitation_id = await self._resolve_recitation_id(recitation_id)

        async def _load() -> dict[str, Any]:
            ayah = await self.repository.get_ayah(
                verse_key,
                translation_id=resolved_translation_id,
                recitation_id=resolved_recitation_id,
            )
            if ayah is None:
                raise NotReadyError(f"No Quran ayah data found for {verse_key}")
            self._ensure_ayah_ready(
                ayah,
                translation_id=resolved_translation_id,
                recitation_id=resolved_recitation_id,
            )
            return {
                "generated_at": datetime.now(timezone.utc).isoformat(),
                "source_api": QURAN_SOURCE_API,
                "source_type": "quran",
                "verse_key": ayah["verse_key"],
                "surah_number": ayah["surah_number"],
                "ayah_number": ayah["ayah_number"],
                "translation_id": resolved_translation_id,
                "recitation_id": resolved_recitation_id,
                "arabic_text": ayah["arabic_text"],
                "transliteration_text": ayah.get("transliteration_text") or "",
                "translation_text": ayah.get("translation_text") or "",
            }

        return await self._cached(
            f"quran:ayah_minimal:{verse_key}:t{resolved_translation_id}:r{resolved_recitation_id}",
            self.cache_settings.ttl_seconds,
            _load,
        )

    async def get_chapter_triplets(
        self,
        chapter_id: int,
        *,
        translation_id: int | None = None,
        recitation_id: int | None = None,
    ) -> dict[str, Any]:
        resolved_translation_id = await self._resolve_translation_id(translation_id)
        resolved_recitation_id = await self._resolve_recitation_id(recitation_id)

        async def _load() -> dict[str, Any]:
            chapter_payload = await self._build_chapter_audio_text_payload(
                chapter_id,
                translation_id=resolved_translation_id,
                recitation_id=resolved_recitation_id,
            )
            blocks = self._build_triplets(chapter_id, chapter_payload["ayahs"])
            if not blocks:
                raise NotReadyError(f"No Quran triplet data found for chapter {chapter_id}")
            return {
                "generated_at": datetime.now(timezone.utc).isoformat(),
                "screen": "quran_triplets",
                "chapter_id": chapter_id,
                "translation_id": resolved_translation_id,
                "recitation_id": resolved_recitation_id,
                "chapter_audio": chapter_payload["chapter_audio"],
                "blocks": blocks,
            }

        return await self._cached(
            f"quran:triplets:{chapter_id}:t{resolved_translation_id}:r{resolved_recitation_id}",
            self.cache_settings.ttl_seconds,
            _load,
        )

    async def get_ayah_translation(
        self,
        verse_key: str,
        *,
        translation_id: int | None = None,
    ) -> dict[str, Any]:
        resolved_translation_id = await self._resolve_translation_id(translation_id)
        ayah_payload = await self.get_ayah_detail(
            verse_key,
            translation_id=resolved_translation_id,
            recitation_id=self.settings.default_recitation_id,
        )
        ayah = ayah_payload["ayah"]
        return {
            "generated_at": datetime.now(timezone.utc).isoformat(),
            "source_api": QURAN_SOURCE_API,
            "translation_id": resolved_translation_id,
            "item": {
                "verse_key": ayah["verse_key"],
                "surah_number": ayah["surah_number"],
                "ayah_number": ayah["ayah_number"],
                "translation_id": resolved_translation_id,
                "translation_text": ayah["translation_text"],
            },
        }

    async def get_chapter_translations(
        self,
        chapter_id: int,
        *,
        translation_id: int | None = None,
    ) -> dict[str, Any]:
        resolved_translation_id = await self._resolve_translation_id(translation_id)
        chapter_payload = await self.get_chapter_ayahs(
            chapter_id,
            translation_id=resolved_translation_id,
            recitation_id=self.settings.default_recitation_id,
        )
        items = [
            {
                "verse_key": ayah["verse_key"],
                "surah_number": ayah["surah_number"],
                "ayah_number": ayah["ayah_number"],
                "translation_id": resolved_translation_id,
                "translation_text": ayah["translation_text"],
            }
            for ayah in chapter_payload["ayahs"]
        ]
        return {
            "generated_at": datetime.now(timezone.utc).isoformat(),
            "source_api": QURAN_SOURCE_API,
            "chapter_id": chapter_id,
            "translation_id": resolved_translation_id,
            "items": items,
        }

    async def get_chapter_audio(
        self,
        chapter_id: int,
        *,
        recitation_id: int | None = None,
    ) -> dict[str, Any]:
        resolved_recitation_id = await self._resolve_recitation_id(recitation_id)

        async def _load() -> dict[str, Any]:
            payload = await self.repository.get_chapter_audio(
                chapter_id,
                recitation_id=resolved_recitation_id,
            )
            if payload is None:
                raise NotReadyError(
                    f"No chapter audio found for chapter {chapter_id} and recitation {resolved_recitation_id}"
                )
            return {
                "generated_at": datetime.now(timezone.utc).isoformat(),
                **payload,
            }

        return await self._cached(
            f"quran:chapter_audio:{chapter_id}:r{resolved_recitation_id}",
            self.cache_settings.ttl_seconds,
            _load,
        )

    def _merge_juz_with_names(self, summary: dict[str, Any]) -> dict[str, Any]:
        juz_number = summary["juz_number"]
        name = JUZ_NAMES.get(juz_number, {
            "name_arabic": "",
            "name_simple": f"Juz {juz_number}",
            "name_transliteration": f"Juz {juz_number}",
        })
        return {**summary, **name}

    async def get_juzs(self) -> dict[str, Any]:
        async def _load() -> dict[str, Any]:
            summaries = await self.repository.list_juz_summaries()
            if not summaries:
                raise NotReadyError("No Quran ayah data available — cannot derive Juz list")
            juzs = [self._merge_juz_with_names(s) for s in summaries]
            return {
                "generated_at": datetime.now(timezone.utc).isoformat(),
                "screen": "quran_juzs",
                "source_api": JUZ_SOURCE_API,
                "juzs": juzs,
            }

        return await self._cached(
            "quran:juzs",
            self.cache_settings.meta_ttl_seconds,
            _load,
        )

    async def get_juz_ayahs(
        self,
        juz_number: int,
        *,
        translation_id: int | None = None,
        recitation_id: int | None = None,
    ) -> dict[str, Any]:
        if juz_number < 1 or juz_number > TOTAL_JUZ_COUNT:
            raise NotReadyError(f"Juz {juz_number} is out of range (1..{TOTAL_JUZ_COUNT})")
        resolved_translation_id = await self._resolve_translation_id(translation_id)
        resolved_recitation_id = await self._resolve_recitation_id(recitation_id)

        async def _load() -> dict[str, Any]:
            ayahs = await self.repository.get_juz_ayahs(
                juz_number,
                translation_id=resolved_translation_id,
                recitation_id=resolved_recitation_id,
            )
            if not ayahs:
                raise NotReadyError(f"No ayah data found for Juz {juz_number}")
            summaries = await self.repository.list_juz_summaries()
            match = next((s for s in summaries if s["juz_number"] == juz_number), None)
            if match is None:
                raise NotReadyError(f"Juz {juz_number} summary missing")
            return {
                "generated_at": datetime.now(timezone.utc).isoformat(),
                "screen": "quran_juz_ayahs",
                "source_api": JUZ_SOURCE_API,
                "juz": self._merge_juz_with_names(match),
                "translation_id": resolved_translation_id,
                "recitation_id": resolved_recitation_id,
                "ayahs": ayahs,
            }

        return await self._cached(
            f"quran:juz_ayahs:{juz_number}:t{resolved_translation_id}:r{resolved_recitation_id}",
            self.cache_settings.ttl_seconds,
            _load,
        )

    async def get_chapter_detail(self, chapter_id: int) -> dict[str, Any]:
        async def _load() -> dict[str, Any]:
            chapter = await self.repository.get_chapter(chapter_id)
            if chapter is None:
                raise NotReadyError(f"No Quran chapter found for id {chapter_id}")
            return {
                "generated_at": datetime.now(timezone.utc).isoformat(),
                "screen": "quran_chapter_detail",
                "source_api": QURAN_SOURCE_API,
                "chapter": chapter,
            }

        return await self._cached(
            f"quran:chapter_detail:{chapter_id}",
            self.cache_settings.meta_ttl_seconds,
            _load,
        )

    async def get_random_ayah(
        self,
        *,
        translation_id: int | None = None,
        recitation_id: int | None = None,
    ) -> dict[str, Any]:
        resolved_translation_id = await self._resolve_translation_id(translation_id)
        resolved_recitation_id = await self._resolve_recitation_id(recitation_id)
        # Do NOT cache — random needs to vary per request
        ayah = await self.repository.get_random_ayah(
            translation_id=resolved_translation_id,
            recitation_id=resolved_recitation_id,
        )
        if ayah is None:
            raise NotReadyError("No Quran ayah data available")
        self._ensure_ayah_ready(
            ayah,
            translation_id=resolved_translation_id,
            recitation_id=resolved_recitation_id,
        )
        return {
            "generated_at": datetime.now(timezone.utc).isoformat(),
            "screen": "quran_random_ayah",
            "source_api": QURAN_SOURCE_API,
            "translation_id": resolved_translation_id,
            "recitation_id": resolved_recitation_id,
            "ayah": ayah,
        }

    async def get_page_ayahs(
        self,
        page_number: int,
        *,
        translation_id: int | None = None,
        recitation_id: int | None = None,
    ) -> dict[str, Any]:
        if page_number < 1 or page_number > 604:
            raise NotReadyError(f"Page {page_number} out of range (1..604)")
        resolved_translation_id = await self._resolve_translation_id(translation_id)
        resolved_recitation_id = await self._resolve_recitation_id(recitation_id)

        async def _load() -> dict[str, Any]:
            ayahs = await self.repository.get_page_ayahs(
                page_number,
                translation_id=resolved_translation_id,
                recitation_id=resolved_recitation_id,
            )
            if not ayahs:
                raise NotReadyError(f"No ayah data found for page {page_number}")
            return {
                "generated_at": datetime.now(timezone.utc).isoformat(),
                "screen": "quran_page_ayahs",
                "source_api": QURAN_SOURCE_API,
                "page_number": page_number,
                "translation_id": resolved_translation_id,
                "recitation_id": resolved_recitation_id,
                "ayahs": ayahs,
            }

        return await self._cached(
            f"quran:page:{page_number}:t{resolved_translation_id}:r{resolved_recitation_id}",
            self.cache_settings.ttl_seconds,
            _load,
        )

    async def get_ayahs_range(
        self,
        from_verse_key: str,
        to_verse_key: str,
        *,
        translation_id: int | None = None,
        recitation_id: int | None = None,
    ) -> dict[str, Any]:
        try:
            fc, fa = (int(x) for x in from_verse_key.split(":", 1))
            tc, ta = (int(x) for x in to_verse_key.split(":", 1))
        except (ValueError, AttributeError) as exc:
            raise NotReadyError(f"Invalid verse_key format: {from_verse_key}/{to_verse_key}") from exc
        if (fc, fa) > (tc, ta):
            raise NotReadyError(f"'from' {from_verse_key} must be <= 'to' {to_verse_key}")
        resolved_translation_id = await self._resolve_translation_id(translation_id)
        resolved_recitation_id = await self._resolve_recitation_id(recitation_id)

        async def _load() -> dict[str, Any]:
            ayahs = await self.repository.get_ayahs_range(
                from_chapter=fc, from_ayah=fa,
                to_chapter=tc, to_ayah=ta,
                translation_id=resolved_translation_id,
                recitation_id=resolved_recitation_id,
            )
            if not ayahs:
                raise NotReadyError(f"No ayahs in range {from_verse_key}..{to_verse_key}")
            return {
                "generated_at": datetime.now(timezone.utc).isoformat(),
                "screen": "quran_ayahs_range",
                "source_api": QURAN_SOURCE_API,
                "from_verse_key": from_verse_key,
                "to_verse_key": to_verse_key,
                "translation_id": resolved_translation_id,
                "recitation_id": resolved_recitation_id,
                "ayahs": ayahs,
            }

        return await self._cached(
            f"quran:range:{from_verse_key}:{to_verse_key}:t{resolved_translation_id}:r{resolved_recitation_id}",
            self.cache_settings.ttl_seconds,
            _load,
        )

    async def get_sajdahs(
        self,
        *,
        translation_id: int | None = None,
    ) -> dict[str, Any]:
        resolved_translation_id = await self._resolve_translation_id(translation_id)
        resolved_recitation_id = self.settings.default_recitation_id

        async def _load() -> dict[str, Any]:
            verse_keys = [s["verse_key"] for s in SAJDAH_POINTS]
            ayahs = await self.repository.get_ayahs_by_verse_keys(
                verse_keys,
                translation_id=resolved_translation_id,
                recitation_id=resolved_recitation_id,
            )
            ayah_by_key = {a["verse_key"]: a for a in ayahs}
            enriched = []
            for s in SAJDAH_POINTS:
                ayah = ayah_by_key.get(s["verse_key"])
                chapter = await self.repository.get_chapter(s["surah_number"]) if ayah else None
                enriched.append({
                    **s,
                    "chapter_name_simple": chapter["name_simple"] if chapter else None,
                    "chapter_name_arabic": chapter["name_arabic"] if chapter else None,
                    "arabic_text": ayah["arabic_text"] if ayah else "",
                    "translation_text": ayah.get("translation_text", "") if ayah else "",
                })
            return {
                "generated_at": datetime.now(timezone.utc).isoformat(),
                "screen": "quran_sajdahs",
                "source_api": f"{QURAN_SOURCE_API}+internal",
                "translation_id": resolved_translation_id,
                "total": TOTAL_SAJDAH_COUNT,
                "sajdahs": enriched,
            }

        return await self._cached(
            f"quran:sajdahs:t{resolved_translation_id}",
            self.cache_settings.meta_ttl_seconds,
            _load,
        )

    async def get_hizbs(self) -> dict[str, Any]:
        async def _load() -> dict[str, Any]:
            hizbs = await self.repository.list_hizb_summaries()
            if not hizbs:
                raise NotReadyError("No Hizb data available")
            return {
                "generated_at": datetime.now(timezone.utc).isoformat(),
                "screen": "quran_hizbs",
                "source_api": f"{QURAN_SOURCE_API}+internal",
                "hizbs": hizbs,
            }

        return await self._cached(
            "quran:hizbs",
            self.cache_settings.meta_ttl_seconds,
            _load,
        )

    async def search_ayahs(
        self,
        query: str,
        *,
        translation_id: int | None = None,
        limit: int = 20,
        offset: int = 0,
    ) -> dict[str, Any]:
        resolved_translation_id = await self._resolve_translation_id(translation_id)
        query = (query or "").strip()
        if not query:
            raise NotReadyError("Query parameter 'q' must not be empty")

        async def _load() -> dict[str, Any]:
            items, total = await self.repository.search_ayahs(
                query,
                translation_id=resolved_translation_id,
                limit=limit,
                offset=offset,
            )
            return {
                "generated_at": datetime.now(timezone.utc).isoformat(),
                "screen": "quran_search",
                "source_api": QURAN_SOURCE_API,
                "query": query,
                "translation_id": resolved_translation_id,
                "total": total,
                "limit": limit,
                "offset": offset,
                "items": items,
            }

        return await self._cached(
            f"quran:search:{query}:t{resolved_translation_id}:l{limit}:o{offset}",
            self.cache_settings.ttl_seconds,
            _load,
        )

    async def get_juz_detail(self, juz_number: int) -> dict[str, Any]:
        if juz_number < 1 or juz_number > TOTAL_JUZ_COUNT:
            raise NotReadyError(f"Juz {juz_number} is out of range (1..{TOTAL_JUZ_COUNT})")

        async def _load() -> dict[str, Any]:
            summaries = await self.repository.list_juz_summaries()
            match = next((s for s in summaries if s["juz_number"] == juz_number), None)
            if match is None:
                raise NotReadyError(f"No ayah data found for Juz {juz_number}")
            return {
                "generated_at": datetime.now(timezone.utc).isoformat(),
                "screen": "quran_juz_detail",
                "source_api": JUZ_SOURCE_API,
                "juz": self._merge_juz_with_names(match),
            }

        return await self._cached(
            f"quran:juz:{juz_number}",
            self.cache_settings.meta_ttl_seconds,
            _load,
        )

    async def get_chapter_audio_text(
        self,
        chapter_id: int,
        *,
        translation_id: int | None = None,
        recitation_id: int | None = None,
    ) -> dict[str, Any]:
        resolved_translation_id = await self._resolve_translation_id(translation_id)
        resolved_recitation_id = await self._resolve_recitation_id(recitation_id)

        async def _load() -> dict[str, Any]:
            return await self._build_chapter_audio_text_payload(
                chapter_id,
                translation_id=resolved_translation_id,
                recitation_id=resolved_recitation_id,
                screen="quran_audio_text",
            )

        return await self._cached(
            f"quran:audio_text:{chapter_id}:t{resolved_translation_id}:r{resolved_recitation_id}",
            self.cache_settings.ttl_seconds,
            _load,
        )
