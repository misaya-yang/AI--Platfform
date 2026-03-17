from __future__ import annotations

import asyncio
import csv
import html
import json
import re
from collections.abc import Iterable
from datetime import date, datetime, timedelta, timezone
from pathlib import Path
from typing import Any

import httpx

from ..config.settings import IslamicContentSettings
from ..core.observability.logging import get_logger
from ..persistence.islamic_content_repository import IslamicContentRepository

logger = get_logger(__name__)

QURAN_SOURCE_API = "quran.foundation"
SUNNAH_SOURCE_API = "sunnah"
HADITH_CDN_SOURCE_API = "hadith-cdn"
ALADHAN_SOURCE_API = "aladhan"

# Mapping from sunnah.com collection names to CDN edition names
_CDN_COLLECTION_MAP: dict[str, str] = {
    "bukhari": "bukhari",
    "muslim": "muslim",
    "tirmidhi": "tirmidhi",
    "nasai": "nasai",
    "ibnmajah": "ibnmajah",
    "abudawud": "abudawud",
    "malik": "malik",
    "nawawi": "nawawi",
    "qudsi": "qudsi",
    "dehlawi": "dehlawi",
}


class IslamicContentError(RuntimeError):
    """Raised when an upstream Islamic content source fails."""


class IslamicContentService:
    """Aggregates Quran, Hadith, Dua, Prayer, and Qiblah data for third parties."""

    def __init__(
        self,
        settings: IslamicContentSettings,
        client: httpx.AsyncClient | None = None,
        repository: IslamicContentRepository | None = None,
    ) -> None:
        self.settings = settings
        self.cache_dir = Path(settings.cache_dir)
        self.cache_dir.mkdir(parents=True, exist_ok=True)
        self._client = client or httpx.AsyncClient(
            timeout=httpx.Timeout(settings.request_timeout_seconds),
            headers={
                "User-Agent": "ai-gateway-islamic-content/1.0",
                "Accept": "application/json",
            },
        )
        self._owns_client = client is None
        self.repository = repository
        self._quran_token_lock = asyncio.Lock()
        self._quran_token: str | None = settings.quran_access_token.strip() or None
        self._quran_token_expires_at: datetime | None = None

    async def close(self) -> None:
        if self._owns_client and not self._client.is_closed:
            await self._client.aclose()

    async def _request_json(
        self,
        base_url: str,
        path: str,
        *,
        params: dict[str, Any] | None = None,
        headers: dict[str, str] | None = None,
    ) -> dict[str, Any]:
        url = f"{base_url.rstrip('/')}/{path.lstrip('/')}"
        response = await self._client.get(url, params=params, headers=headers)
        try:
            response.raise_for_status()
        except httpx.HTTPStatusError as exc:
            detail = exc.response.text[:500]
            raise IslamicContentError(
                f"Upstream request failed ({exc.response.status_code}) for {url}: {detail}"
            ) from exc
        data = response.json()
        if not isinstance(data, dict):
            raise IslamicContentError(f"Unexpected non-object response from {url}")
        return data

    def _cache_path(self, *parts: str) -> Path:
        path = self.cache_dir.joinpath(*parts)
        path.parent.mkdir(parents=True, exist_ok=True)
        return path

    def _read_json_file(self, path: Path) -> Any:
        if not path.exists():
            return None
        return json.loads(path.read_text(encoding="utf-8"))

    def _write_json_file(self, path: Path, payload: Any) -> None:
        path.write_text(json.dumps(payload, ensure_ascii=False, indent=2), encoding="utf-8")

    def _read_cached(self, *parts: str) -> Any:
        return self._read_json_file(self._cache_path(*parts))

    def _write_cached(self, payload: Any, *parts: str) -> None:
        self._write_json_file(self._cache_path(*parts), payload)

    def _default_metadata(self) -> dict[str, Any]:
        return {"generated_at": datetime.now(timezone.utc).isoformat()}

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

    def _clear_quran_token_cache(self) -> None:
        if self.settings.quran_access_token.strip():
            return
        self._quran_token = None
        self._quran_token_expires_at = None

    async def _fetch_quran_access_token(self) -> str:
        client_id = self.settings.quran_client_id.strip()
        client_secret = self.settings.quran_client_secret.strip()
        if not client_id or not client_secret:
            raise IslamicContentError(
                "Quran OAuth is not configured; set both quran_client_id and quran_client_secret"
            )
        url = f"{self.settings.quran_auth_url.rstrip('/')}/oauth2/token"
        response = await self._client.post(
            url,
            auth=(client_id, client_secret),
            headers={"Content-Type": "application/x-www-form-urlencoded"},
            data={
                "grant_type": "client_credentials",
                "scope": self.settings.quran_scope,
            },
        )
        try:
            response.raise_for_status()
        except httpx.HTTPStatusError as exc:
            detail = exc.response.text[:500]
            raise IslamicContentError(
                "Failed to obtain access token from Quran Foundation OAuth2 "
                f"({exc.response.status_code}): {detail}"
            ) from exc
        payload = response.json()
        if not isinstance(payload, dict):
            raise IslamicContentError("Quran OAuth token response was not a JSON object")
        access_token = str(payload.get("access_token") or "").strip()
        expires_in = int(payload.get("expires_in") or 3600)
        if not access_token:
            raise IslamicContentError("Quran OAuth token response did not include access_token")
        self._quran_token = access_token
        self._quran_token_expires_at = datetime.now(timezone.utc) + timedelta(
            seconds=max(expires_in - 60, 60)
        )
        return access_token

    async def _get_quran_access_token(self) -> str | None:
        static_token = self.settings.quran_access_token.strip()
        if static_token:
            self._quran_token = static_token
            return static_token

        client_id = self.settings.quran_client_id.strip()
        client_secret = self.settings.quran_client_secret.strip()
        if not client_id and not client_secret:
            return None
        if not client_id or not client_secret:
            raise IslamicContentError(
                "Quran OAuth is partially configured; set both quran_client_id and quran_client_secret"
            )

        if (
            self._quran_token
            and self._quran_token_expires_at
            and datetime.now(timezone.utc) < self._quran_token_expires_at
        ):
            return self._quran_token

        async with self._quran_token_lock:
            if (
                self._quran_token
                and self._quran_token_expires_at
                and datetime.now(timezone.utc) < self._quran_token_expires_at
            ):
                return self._quran_token
            return await self._fetch_quran_access_token()

    async def _quran_headers(self) -> dict[str, str] | None:
        client_id = self.settings.quran_client_id.strip()
        access_token = await self._get_quran_access_token()
        if not client_id and not access_token:
            return None
        if not client_id or not access_token:
            raise IslamicContentError(
                "Quran API auth is partially configured; set quran_client_id and provide either quran_access_token or quran_client_secret"
            )
        return {"x-client-id": client_id, "x-auth-token": access_token}

    async def _quran_request_json(
        self,
        path: str,
        *,
        params: dict[str, Any] | None = None,
    ) -> dict[str, Any]:
        headers = await self._quran_headers()
        try:
            return await self._request_json(
                self.settings.quran_base_url,
                path,
                params=params,
                headers=headers,
            )
        except IslamicContentError as exc:
            if "(401)" not in str(exc):
                raise
            self._clear_quran_token_cache()
            headers = await self._quran_headers()
            return await self._request_json(
                self.settings.quran_base_url,
                path,
                params=params,
                headers=headers,
            )

    async def get_manifest(self) -> dict[str, Any]:
        manifest = self._read_cached("manifest.json")
        if manifest is None:
            raise IslamicContentError("Islamic content manifest not found")
        return manifest

    async def get_canonical_summary(self) -> dict[str, Any]:
        if not self.repository:
            return {
                "database_enabled": False,
                "counts": {},
                "generated_at": datetime.now(timezone.utc).isoformat(),
            }
        return await self.repository.get_canonical_summary()

    async def get_quran_translations(self, *, use_cache: bool = True) -> dict[str, Any]:
        if use_cache:
            cached = self._read_cached("quran", "resources_translations.json")
            if cached is not None:
                return cached
        payload = await self._quran_request_json("resources/translations", params={"language": "en"})
        result = {
            **self._default_metadata(),
            "source_api": QURAN_SOURCE_API,
            "translations": payload.get("translations") or [],
        }
        self._write_cached(result, "quran", "resources_translations.json")
        return result

    async def get_quran_recitations(self, *, use_cache: bool = True) -> dict[str, Any]:
        if use_cache:
            cached = self._read_cached("quran", "resources_recitations.json")
            if cached is not None:
                return cached
        payload = await self._quran_request_json("resources/recitations")
        result = {
            **self._default_metadata(),
            "source_api": QURAN_SOURCE_API,
            "recitations": payload.get("recitations") or [],
        }
        self._write_cached(result, "quran", "resources_recitations.json")
        return result

    async def get_quran_chapters(self, *, use_cache: bool = True) -> dict[str, Any]:
        if use_cache:
            cached = self._read_cached("quran", "chapters.json")
            if cached is not None:
                return cached

        chapter_payload = await self._quran_request_json("chapters")
        chapters = []
        for raw in chapter_payload.get("chapters") or []:
            chapters.append(
                {
                    "chapter_id": raw.get("id"),
                    "name_simple": raw.get("name_simple"),
                    "name_complex": raw.get("name_complex"),
                    "name_arabic": raw.get("name_arabic"),
                    "translated_name": ((raw.get("translated_name") or {}).get("name")),
                    "revelation_place": raw.get("revelation_place"),
                    "verses_count": raw.get("verses_count"),
                }
            )
        result = {
            **self._default_metadata(),
            "source_api": QURAN_SOURCE_API,
            "chapters": chapters,
        }
        self._write_cached(result, "quran", "chapters.json")
        return result

    async def get_quran_home(
        self,
        *,
        continue_verse_key: str | None = None,
        use_cache: bool = True,
    ) -> dict[str, Any]:
        chapters_payload = await self.get_quran_chapters(use_cache=use_cache)
        continue_reading = None
        if continue_verse_key:
            continue_reading = await self.get_quran_ayah(continue_verse_key, use_cache=use_cache)
        return {
            "screen": "quran_home",
            "version": "v1",
            "generated_at": datetime.now(timezone.utc).isoformat(),
            "header": {
                "title": "Quran",
                "tabs": ["surah", "juz", "bookmarked"],
                "default_tab": "surah",
            },
            "continue_reading": continue_reading,
            "chapters": chapters_payload["chapters"],
        }

    async def get_quran_chapter_ayahs(
        self,
        chapter_id: int,
        *,
        translation_id: int | None = None,
        recitation_id: int | None = None,
        use_cache: bool = True,
    ) -> dict[str, Any]:
        translation_id = translation_id or self.settings.quran_default_translation_id
        recitation_id = recitation_id or self.settings.quran_default_recitation_id
        cache_name = f"chapter_{chapter_id}_t{translation_id}_r{recitation_id}.json"
        if use_cache:
            cached = self._read_cached("quran", "ayahs", cache_name)
            if cached is not None:
                return cached

        page = 1
        ayahs: list[dict[str, Any]] = []
        while True:
            payload = await self._quran_request_json(
                f"verses/by_chapter/{chapter_id}",
                params={
                    "language": "en",
                    "words": "true",
                    "translations": translation_id,
                    "word_fields": self.settings.quran_word_fields,
                    "fields": "text_uthmani",
                    "audio": recitation_id,
                    "per_page": self.settings.quran_page_size,
                    "page": page,
                },
            )
            verses = payload.get("verses") or []
            if not verses:
                break
            ayahs.extend(
                self._normalize_quran_ayah(v, translation_id=translation_id, recitation_id=recitation_id)
                for v in verses
            )
            if len(verses) < self.settings.quran_page_size:
                break
            page += 1

        result = {
            **self._default_metadata(),
            "source_api": QURAN_SOURCE_API,
            "chapter_id": chapter_id,
            "translation_id": translation_id,
            "recitation_id": recitation_id,
            "ayahs": ayahs,
        }
        self._write_cached(result, "quran", "ayahs", cache_name)
        return result

    async def get_quran_triplets(
        self,
        chapter_id: int,
        *,
        translation_id: int | None = None,
        recitation_id: int | None = None,
        group_size: int = 3,
        use_cache: bool = True,
    ) -> dict[str, Any]:
        translation_id = translation_id or self.settings.quran_default_translation_id
        recitation_id = recitation_id or self.settings.quran_default_recitation_id
        cache_name = f"chapter_{chapter_id}_triplets_t{translation_id}_r{recitation_id}.json"
        if use_cache:
            cached = self._read_cached("quran", "triplets", cache_name)
            if cached is not None:
                return cached

        ayah_payload = await self.get_quran_chapter_ayahs(
            chapter_id,
            translation_id=translation_id,
            recitation_id=recitation_id,
            use_cache=use_cache,
        )
        ayahs = ayah_payload["ayahs"]
        blocks = []
        for start in range(0, len(ayahs), group_size):
            chunk = ayahs[start : start + group_size]
            if not chunk:
                continue
            ref = f"{chapter_id}:{chunk[0]['ayah_number']}-{chunk[-1]['ayah_number']}"
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

        result = {
            **self._default_metadata(),
            "screen": "quran_triplets",
            "chapter_id": chapter_id,
            "translation_id": translation_id,
            "recitation_id": recitation_id,
            "blocks": blocks,
        }
        self._write_cached(result, "quran", "triplets", cache_name)
        return result

    async def get_quran_ayah(
        self,
        verse_key: str,
        *,
        translation_id: int | None = None,
        recitation_id: int | None = None,
        use_cache: bool = True,
    ) -> dict[str, Any]:
        translation_id = translation_id or self.settings.quran_default_translation_id
        recitation_id = recitation_id or self.settings.quran_default_recitation_id
        cache_name = f"{verse_key.replace(':', '_')}_t{translation_id}_r{recitation_id}.json"
        if use_cache:
            cached = self._read_cached("quran", "ayah_detail", cache_name)
            if cached is not None:
                return cached

        payload = await self._quran_request_json(
            f"verses/by_key/{verse_key}",
            params={
                "language": "en",
                "words": "true",
                "translations": translation_id,
                "word_fields": self.settings.quran_word_fields,
                "fields": "text_uthmani",
                "audio": recitation_id,
            },
        )
        verse = payload.get("verse") or {}
        normalized = self._normalize_quran_ayah(
            verse,
            translation_id=translation_id,
            recitation_id=recitation_id,
        )
        result = {
            **self._default_metadata(),
            "screen": "quran_ayah_detail",
            "translation_id": translation_id,
            "recitation_id": recitation_id,
            "ayah": normalized,
        }
        self._write_cached(result, "quran", "ayah_detail", cache_name)
        return result

    def _normalize_quran_translation_item(
        self,
        ayah: dict[str, Any],
        *,
        translation_id: int,
    ) -> dict[str, Any]:
        return {
            "verse_key": ayah.get("verse_key"),
            "surah_number": ayah.get("surah_number"),
            "ayah_number": ayah.get("ayah_number"),
            "translation_id": translation_id,
            "translation_text": ayah.get("translation_text") or "",
        }

    async def get_quran_ayah_translation(
        self,
        verse_key: str,
        *,
        translation_id: int | None = None,
        use_cache: bool = True,
    ) -> dict[str, Any]:
        translation_id = translation_id or self.settings.quran_default_translation_id
        cache_name = f"{verse_key.replace(':', '_')}_t{translation_id}.json"
        if use_cache:
            cached = self._read_cached("quran", "translation_detail", cache_name)
            if cached is not None:
                return cached

        ayah_payload = await self.get_quran_ayah(
            verse_key,
            translation_id=translation_id,
            use_cache=use_cache,
        )
        result = {
            **self._default_metadata(),
            "source_api": QURAN_SOURCE_API,
            "translation_id": translation_id,
            "item": self._normalize_quran_translation_item(
                ayah_payload["ayah"],
                translation_id=translation_id,
            ),
        }
        self._write_cached(result, "quran", "translation_detail", cache_name)
        return result

    async def get_quran_chapter_translations(
        self,
        chapter_id: int,
        *,
        translation_id: int | None = None,
        use_cache: bool = True,
    ) -> dict[str, Any]:
        translation_id = translation_id or self.settings.quran_default_translation_id
        cache_name = f"chapter_{chapter_id}_t{translation_id}.json"
        if use_cache:
            cached = self._read_cached("quran", "translations", cache_name)
            if cached is not None:
                return cached

        ayah_payload = await self.get_quran_chapter_ayahs(
            chapter_id,
            translation_id=translation_id,
            use_cache=use_cache,
        )
        items = [
            self._normalize_quran_translation_item(ayah, translation_id=translation_id)
            for ayah in ayah_payload["ayahs"]
        ]
        result = {
            **self._default_metadata(),
            "source_api": QURAN_SOURCE_API,
            "chapter_id": chapter_id,
            "translation_id": translation_id,
            "items": items,
        }
        self._write_cached(result, "quran", "translations", cache_name)
        return result

    def _normalize_quran_ayah(
        self,
        verse: dict[str, Any],
        *,
        translation_id: int,
        recitation_id: int,
    ) -> dict[str, Any]:
        verse_key = str(verse.get("verse_key") or "")
        chapter_part, _, ayah_part = verse_key.partition(":")
        words = [self._normalize_quran_word(item) for item in verse.get("words") or []]
        translations = verse.get("translations") or []
        translation_text = "\n".join(
            self._clean_text(item.get("text")) for item in translations if item.get("text")
        ).strip()
        transliteration_text = " ".join(
            word["transliteration"] for word in words if word.get("transliteration")
        ).strip()
        audio = verse.get("audio") or {}
        return {
            "source_api": QURAN_SOURCE_API,
            "source_type": "quran",
            "verse_key": verse_key,
            "surah_number": int(chapter_part) if chapter_part.isdigit() else None,
            "ayah_number": int(ayah_part) if ayah_part.isdigit() else None,
            "juz_number": verse.get("juz_number"),
            "hizb_number": verse.get("hizb_number"),
            "rub_number": verse.get("rub_number"),
            "page_number": verse.get("page_number"),
            "arabic_text": (verse.get("text_uthmani") or verse.get("text_uthmani_simple") or "").strip(),
            "transliteration_text": transliteration_text,
            "translation_text": translation_text,
            "words": words,
            "audio": {
                "recitation_id": recitation_id,
                "translation_id": translation_id,
                "url": audio.get("url") or audio.get("audio_url"),
            },
        }

    def _normalize_quran_word(self, word: dict[str, Any]) -> dict[str, Any]:
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
            "audio_url": word.get("audio_url"),
        }

    # ------------------------------------------------------------------
    # Hadith provider routing: CDN (fawazahmed0) primary, sunnah.com fallback
    # ------------------------------------------------------------------

    @property
    def _use_hadith_cdn(self) -> bool:
        return bool(self.settings.hadith_cdn_enabled and self.settings.hadith_cdn_base_url)

    def _cdn_edition(self, collection_name: str, lang: str | None = None) -> str:
        lang = lang or self.settings.hadith_cdn_default_lang
        cdn_name = _CDN_COLLECTION_MAP.get(collection_name, collection_name)
        return f"{lang}-{cdn_name}"

    def _sunnah_headers(self) -> dict[str, str]:
        if not self.settings.sunnah_api_key:
            raise IslamicContentError("Sunnah API key is not configured")
        return {"X-API-Key": self.settings.sunnah_api_key}

    # --- get_hadith_collections ---

    async def get_hadith_collections(self, *, use_cache: bool = True) -> dict[str, Any]:
        if use_cache:
            cached = self._read_cached("hadith", "collections.json")
            if cached is not None:
                return cached
        if self._use_hadith_cdn:
            result = await self._cdn_get_hadith_collections()
        else:
            result = await self._sunnah_get_hadith_collections()
        self._write_cached(result, "hadith", "collections.json")
        return result

    async def _cdn_get_hadith_collections(self) -> dict[str, Any]:
        payload = await self._request_json(
            self.settings.hadith_cdn_base_url,
            "editions.json",
        )
        items = []
        for name, info in payload.items():
            if not isinstance(info, dict):
                continue
            display_name = info.get("name") or name.title()
            items.append({
                "name": name,
                "title": display_name,
                "short_intro": None,
                "has_books": True,
                "has_chapters": True,
                "total_books": None,
                "total_hadith": None,
            })
        return {
            **self._default_metadata(),
            "screen": "hadith_collections",
            "source_api": HADITH_CDN_SOURCE_API,
            "collections": items,
        }

    async def _sunnah_get_hadith_collections(self) -> dict[str, Any]:
        items: list[dict[str, Any]] = []
        page = 1
        while True:
            payload = await self._request_json(
                self.settings.sunnah_base_url, "collections",
                params={"limit": self.settings.sunnah_page_size, "page": page},
                headers=self._sunnah_headers(),
            )
            data = payload.get("data") or []
            if not data:
                break
            items.extend(self._normalize_sunnah_collection(item) for item in data)
            total_pages = self._pagination_total_pages(payload.get("pagination") or {})
            if total_pages is not None and page >= total_pages:
                break
            if total_pages is not None:
                page += 1
                continue
            if len(data) < self.settings.sunnah_page_size:
                break
            page += 1
        return {
            **self._default_metadata(),
            "screen": "hadith_collections",
            "source_api": SUNNAH_SOURCE_API,
            "collections": items,
        }

    # --- get_hadith_books ---

    async def get_hadith_books(
        self, collection_name: str, *, use_cache: bool = True,
    ) -> dict[str, Any]:
        cache_name = f"{collection_name}.json"
        if use_cache:
            cached = self._read_cached("hadith", "books", cache_name)
            if cached is not None:
                return cached
        if self._use_hadith_cdn:
            result = await self._cdn_get_hadith_books(collection_name)
        else:
            result = await self._sunnah_get_hadith_books(collection_name)
        self._write_cached(result, "hadith", "books", cache_name)
        return result

    async def _cdn_get_hadith_books(self, collection_name: str) -> dict[str, Any]:
        edition = self._cdn_edition(collection_name)
        payload = await self._request_json(
            self.settings.hadith_cdn_base_url,
            f"editions/{edition}.json",
        )
        meta = payload.get("metadata") or {}
        section_details = meta.get("section_details") or {}
        hadiths = payload.get("hadiths") or []

        # Build book list from section_details + count hadiths per section
        book_hadith_counts: dict[str, int] = {}
        for h in hadiths:
            ref = h.get("reference") or {}
            bk = str(ref.get("book") or "")
            if bk:
                book_hadith_counts[bk] = book_hadith_counts.get(bk, 0) + 1

        books = []
        for book_num in sorted(section_details.keys(), key=lambda x: int(x) if x.isdigit() else 0):
            raw_title = section_details[book_num]
            # CDN sometimes returns a dict instead of a string for section titles
            if isinstance(raw_title, dict):
                raw_title = raw_title.get("title") or raw_title.get("name") or f"Book {book_num}"
            books.append({
                "book_number": book_num,
                "title": str(raw_title) if raw_title else f"Book {book_num}",
                "hadith_start_number": None,
                "hadith_end_number": None,
                "number_of_hadith": book_hadith_counts.get(book_num),
            })

        return {
            **self._default_metadata(),
            "screen": "hadith_books",
            "source_api": HADITH_CDN_SOURCE_API,
            "collection": {
                "name": collection_name,
                "title": meta.get("name") or collection_name.title(),
                "short_intro": None,
                "has_books": True,
                "has_chapters": True,
                "total_books": len(books),
                "total_hadith": len(hadiths),
            },
            "books": books,
        }

    async def _sunnah_get_hadith_books(self, collection_name: str) -> dict[str, Any]:
        meta_payload = await self._request_json(
            self.settings.sunnah_base_url,
            f"collections/{collection_name}",
            headers=self._sunnah_headers(),
        )
        books: list[dict[str, Any]] = []
        page = 1
        while True:
            payload = await self._request_json(
                self.settings.sunnah_base_url,
                f"collections/{collection_name}/books",
                params={"limit": self.settings.sunnah_page_size, "page": page},
                headers=self._sunnah_headers(),
            )
            data = payload.get("data") or []
            if not data:
                break
            books.extend(self._normalize_sunnah_book(item) for item in data)
            total_pages = self._pagination_total_pages(payload.get("pagination") or {})
            if total_pages is not None and page >= total_pages:
                break
            if total_pages is not None:
                page += 1
                continue
            if len(data) < self.settings.sunnah_page_size:
                break
            page += 1
        return {
            **self._default_metadata(),
            "screen": "hadith_books",
            "source_api": SUNNAH_SOURCE_API,
            "collection": self._normalize_sunnah_collection(meta_payload.get("data") or meta_payload),
            "books": books,
        }

    # --- get_hadith_book_items ---

    async def get_hadith_book_items(
        self, collection_name: str, book_number: str, *,
        page: int = 1, limit: int | None = None, use_cache: bool = True,
    ) -> dict[str, Any]:
        limit = limit or self.settings.sunnah_page_size
        cache_name = f"book_{book_number}_page_{page}_limit_{limit}.json"
        if use_cache:
            cached = self._read_cached("hadith", "hadiths", collection_name, cache_name)
            if cached is not None:
                return cached
        if self._use_hadith_cdn:
            result = await self._cdn_get_hadith_book_items(collection_name, book_number, page=page, limit=limit)
        else:
            result = await self._sunnah_get_hadith_book_items(collection_name, book_number, page=page, limit=limit)
        self._write_cached(result, "hadith", "hadiths", collection_name, cache_name)
        return result

    async def _cdn_get_hadith_book_items(
        self, collection_name: str, book_number: str, *, page: int = 1, limit: int = 50,
    ) -> dict[str, Any]:
        edition = self._cdn_edition(collection_name)
        payload = await self._request_json(
            self.settings.hadith_cdn_base_url,
            f"editions/{edition}/sections/{book_number}.json",
        )
        hadiths = payload.get("hadiths") or []

        # Fetch Arabic text in parallel for summaries
        ara_texts = await self._cdn_fetch_arabic_for_hadiths(collection_name, hadiths)

        # Client-side pagination
        start = (page - 1) * limit
        page_hadiths = hadiths[start : start + limit]

        items = []
        for h in page_hadiths:
            ref = h.get("reference") or {}
            hnum = str(h.get("hadithnumber") or "")
            text = self._clean_text(h.get("text"))
            items.append({
                "collection": collection_name,
                "book_number": str(ref.get("book") or book_number),
                "chapter_id": str(ref.get("book") or book_number),
                "hadith_number": hnum,
                "title": None,
                "preview_text": text[:280] if text else "",
                "arabic_preview_text": self._clean_text(ara_texts.get(hnum))[:280] if ara_texts.get(hnum) else "",
            })
        return {
            **self._default_metadata(),
            "screen": "hadith_book_items",
            "source_api": HADITH_CDN_SOURCE_API,
            "collection_name": collection_name,
            "book_number": book_number,
            "items": items,
            "pagination": {"current_page": page, "total": len(hadiths), "per_page": limit},
        }

    async def _sunnah_get_hadith_book_items(
        self, collection_name: str, book_number: str, *, page: int = 1, limit: int = 50,
    ) -> dict[str, Any]:
        payload = await self._request_json(
            self.settings.sunnah_base_url,
            f"collections/{collection_name}/books/{book_number}/hadiths",
            params={"page": page, "limit": limit},
            headers=self._sunnah_headers(),
        )
        return {
            **self._default_metadata(),
            "screen": "hadith_book_items",
            "source_api": SUNNAH_SOURCE_API,
            "collection_name": collection_name,
            "book_number": book_number,
            "items": [self._normalize_sunnah_summary(item) for item in payload.get("data") or []],
            "pagination": payload.get("pagination") or {},
        }

    # --- get_hadith_detail ---

    async def get_hadith_detail(
        self, collection_name: str, hadith_number: str, *, use_cache: bool = True,
    ) -> dict[str, Any]:
        cache_name = f"{hadith_number}.json"
        if use_cache:
            cached = self._read_cached("hadith", "detail", collection_name, cache_name)
            if cached is not None:
                return cached
        if self._use_hadith_cdn:
            result = await self._cdn_get_hadith_detail(collection_name, hadith_number)
        else:
            result = await self._sunnah_get_hadith_detail(collection_name, hadith_number)
        self._write_cached(result, "hadith", "detail", collection_name, cache_name)
        return result

    async def _cdn_get_hadith_detail(self, collection_name: str, hadith_number: str) -> dict[str, Any]:
        edition = self._cdn_edition(collection_name)
        eng_payload = await self._request_json(
            self.settings.hadith_cdn_base_url,
            f"editions/{edition}/{hadith_number}.json",
        )
        eng_hadiths = eng_payload.get("hadiths") or []
        eng = eng_hadiths[0] if eng_hadiths else {}

        # Fetch Arabic
        ara_edition = self._cdn_edition(collection_name, lang="ara")
        try:
            ara_payload = await self._request_json(
                self.settings.hadith_cdn_base_url,
                f"editions/{ara_edition}/{hadith_number}.json",
            )
            ara = (ara_payload.get("hadiths") or [{}])[0]
        except Exception:
            ara = {}

        ref = eng.get("reference") or {}
        # Resolve book title from metadata
        meta = eng_payload.get("metadata") or {}
        section_details = meta.get("section_details") or {}
        book_num = str(ref.get("book") or "")
        chapter_title = section_details.get(book_num)

        return {
            **self._default_metadata(),
            "screen": "hadith_detail",
            "source_api": HADITH_CDN_SOURCE_API,
            "hadith": {
                "collection": collection_name,
                "book_number": book_num,
                "chapter_id": book_num,
                "hadith_number": str(eng.get("hadithnumber") or hadith_number),
                "chapter_title": chapter_title,
                "translation_text": self._clean_text(eng.get("text")),
                "arabic_text": self._clean_text(ara.get("text")),
                "grades": {
                    "en": eng.get("grades") or [],
                    "ar": ara.get("grades") or [],
                },
                "share_actions": ["bookmark", "share", "copy"],
            },
        }

    async def _sunnah_get_hadith_detail(self, collection_name: str, hadith_number: str) -> dict[str, Any]:
        payload = await self._request_json(
            self.settings.sunnah_base_url,
            f"collections/{collection_name}/hadiths/{hadith_number}",
            headers=self._sunnah_headers(),
        )
        return {
            **self._default_metadata(),
            "screen": "hadith_detail",
            "source_api": SUNNAH_SOURCE_API,
            "hadith": self._normalize_sunnah_detail(payload.get("data") or payload),
        }

    # --- Arabic text helper for CDN ---

    async def _cdn_fetch_arabic_for_hadiths(
        self, collection_name: str, hadiths: list[dict[str, Any]],
    ) -> dict[str, str]:
        """Fetch Arabic texts for a list of hadiths. Returns {hadith_number: arabic_text}."""
        if not hadiths:
            return {}
        ara_edition = self._cdn_edition(collection_name, lang="ara")
        # Use section endpoint to get all Arabic in one call
        ref = (hadiths[0].get("reference") or {})
        book = ref.get("book")
        if book is None:
            return {}
        try:
            payload = await self._request_json(
                self.settings.hadith_cdn_base_url,
                f"editions/{ara_edition}/sections/{book}.json",
            )
            return {
                str(h.get("hadithnumber") or ""): h.get("text") or ""
                for h in (payload.get("hadiths") or [])
            }
        except Exception:
            return {}

    # ------------------------------------------------------------------
    # Sunnah.com normalizers (kept for fallback)
    # ------------------------------------------------------------------

    def _normalize_sunnah_collection(self, item: dict[str, Any]) -> dict[str, Any]:
        collection_entries = item.get("collection") or []
        english_entry = next(
            (entry for entry in collection_entries if entry.get("lang") == "en"),
            collection_entries[0] if collection_entries else {},
        )
        return {
            "name": item.get("name"),
            "title": english_entry.get("title") or item.get("name"),
            "short_intro": english_entry.get("shortIntro"),
            "has_books": item.get("hasBooks"),
            "has_chapters": item.get("hasChapters"),
            "total_books": item.get("totalBooks") or english_entry.get("totalBooks"),
            "total_hadith": item.get("totalHadith") or english_entry.get("totalHadith"),
        }

    def _normalize_sunnah_book(self, item: dict[str, Any]) -> dict[str, Any]:
        book_entries = item.get("book") or []
        english_entry = next(
            (entry for entry in book_entries if entry.get("lang") == "en"),
            book_entries[0] if book_entries else {},
        )
        return {
            "book_number": str(item.get("bookNumber") or ""),
            "title": english_entry.get("name") or english_entry.get("title"),
            "hadith_start_number": item.get("hadithStartNumber"),
            "hadith_end_number": item.get("hadithEndNumber"),
            "number_of_hadith": item.get("numberOfHadith"),
        }

    def _normalize_sunnah_summary(self, item: dict[str, Any]) -> dict[str, Any]:
        entries = item.get("hadith") or []
        english = next((entry for entry in entries if entry.get("lang") == "en"), {})
        arabic = next((entry for entry in entries if entry.get("lang") == "ar"), {})
        return {
            "collection": item.get("collection"),
            "book_number": str(item.get("bookNumber") or ""),
            "chapter_id": str(item.get("chapterId") or ""),
            "hadith_number": str(item.get("hadithNumber") or ""),
            "title": english.get("chapterTitle") or arabic.get("chapterTitle"),
            "preview_text": self._clean_text(english.get("body"))[:280],
            "arabic_preview_text": self._clean_text(arabic.get("body"))[:280],
        }

    def _normalize_sunnah_detail(self, item: dict[str, Any]) -> dict[str, Any]:
        entries = item.get("hadith") or []
        english = next((entry for entry in entries if entry.get("lang") == "en"), {})
        arabic = next((entry for entry in entries if entry.get("lang") == "ar"), {})
        return {
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
            "share_actions": ["bookmark", "share", "copy"],
        }

    async def get_prayer_times(
        self,
        *,
        latitude: float,
        longitude: float,
        prayer_date: str | None = None,
        method: int | None = None,
    ) -> dict[str, Any]:
        prayer_date = prayer_date or date.today().isoformat()
        method = method or self.settings.aladhan_default_method
        payload = await self._request_json(
            self.settings.aladhan_base_url,
            f"timings/{prayer_date}",
            params={"latitude": latitude, "longitude": longitude, "method": method},
        )
        data = payload.get("data") or {}
        meta = data.get("meta") or {}
        timings = data.get("timings") or {}
        prayers = []
        for key in ("Fajr", "Sunrise", "Dhuhr", "Asr", "Maghrib", "Isha"):
            prayers.append({"name": key, "time": timings.get(key)})
        return {
            "screen": "prayer_times_home",
            "generated_at": datetime.now(timezone.utc).isoformat(),
            "source_api": ALADHAN_SOURCE_API,
            "location": {
                "latitude": latitude,
                "longitude": longitude,
                "timezone": meta.get("timezone"),
            },
            "date": data.get("date") or {"gregorian": {"date": prayer_date}},
            "prayers": prayers,
            "meta": meta,
        }

    async def get_qibla(self, *, latitude: float, longitude: float) -> dict[str, Any]:
        payload = await self._request_json(
            self.settings.aladhan_base_url,
            f"qibla/{latitude}/{longitude}",
        )
        data = payload.get("data") or {}
        return {
            "screen": "qiblah_home",
            "generated_at": datetime.now(timezone.utc).isoformat(),
            "source_api": ALADHAN_SOURCE_API,
            "location": {"latitude": latitude, "longitude": longitude},
            "qiblah_bearing": data.get("direction"),
            "meta": data,
        }

    async def get_dua_categories(self) -> dict[str, Any]:
        items = self._load_dua_items()
        counts: dict[str, int] = {}
        for item in items:
            category = item.get("category") or "Uncategorized"
            counts[category] = counts.get(category, 0) + 1
        categories = [
            {"category": category, "dua_count": count}
            for category, count in sorted(counts.items(), key=lambda value: value[0].lower())
        ]
        return {
            "screen": "dua_category_home",
            "generated_at": datetime.now(timezone.utc).isoformat(),
            "source_api": "local_dua_dataset",
            "categories": categories,
        }

    async def get_duas(
        self,
        *,
        category: str | None = None,
        search: str | None = None,
    ) -> dict[str, Any]:
        items = self._load_dua_items()
        normalized_search = (search or "").strip().lower()
        filtered = []
        for item in items:
            if category and str(item.get("category") or "").strip().lower() != category.lower():
                continue
            haystack = " ".join(
                str(item.get(key) or "")
                for key in ("title", "dua", "translation", "transliteration", "category")
            ).lower()
            if normalized_search and normalized_search not in haystack:
                continue
            filtered.append(item)
        return {
            "screen": "dua_list",
            "generated_at": datetime.now(timezone.utc).isoformat(),
            "source_api": "local_dua_dataset",
            "items": filtered,
        }

    def _load_dua_items(self) -> list[dict[str, Any]]:
        path = Path(self.settings.duas_file_path)
        if not path.exists():
            raise IslamicContentError(f"Dua dataset file not found: {path}")
        if path.suffix.lower() == ".json":
            raw = json.loads(path.read_text(encoding="utf-8"))
            rows = raw if isinstance(raw, list) else raw.get("items") or raw.get("data") or []
        elif path.suffix.lower() == ".csv":
            with path.open("r", encoding="utf-8-sig", newline="") as file_obj:
                rows = list(csv.DictReader(file_obj))
        else:
            raise IslamicContentError(f"Unsupported dua dataset format: {path.suffix}")
        return [self._normalize_dua_item(index, row) for index, row in enumerate(rows, start=1)]

    def _normalize_dua_item(self, position: int, row: dict[str, Any]) -> dict[str, Any]:
        def pick(*keys: str) -> str:
            for key in keys:
                value = row.get(key)
                if value:
                    return str(value).strip()
            return ""

        title = pick("title", "dua_name", "name")
        category = pick("category", "group", "type") or "Uncategorized"
        return {
            "dua_id": pick("id") or f"dua-{position}",
            "title": title or f"Dua {position}",
            "category": category,
            "arabic_text": pick("arabic", "dua_arabic", "text_arabic"),
            "transliteration_text": pick("transliteration", "romanized", "dua_transliteration"),
            "translation_text": pick("translation", "english", "dua_english"),
            "reference": pick("reference", "source"),
            "benefit": pick("benefit", "notes", "description"),
        }

    async def sync_static_content(
        self,
        *,
        include_quran: bool = True,
        include_hadith: bool = False,
        hadith_collections: Iterable[str] | None = None,
        include_duas: bool = True,
        persist_db: bool = False,
    ) -> dict[str, Any]:
        repository = self.repository if persist_db and self.repository and self.repository.enabled else None
        sync_scope = {
            "include_quran": include_quran,
            "include_hadith": include_hadith,
            "hadith_collections": list(hadith_collections or []),
            "include_duas": include_duas,
            "persist_db": bool(repository),
        }
        sync_metrics: dict[str, int] = {}
        sync_run_id = ""
        if repository:
            sync_run_id = await repository.start_sync_run("islamic_content", sync_scope)

        manifest: dict[str, Any] = {
            **self._default_metadata(),
            "cache_dir": str(self.cache_dir),
            "steps": [],
        }
        try:
            if include_quran:
                chapters = await self.get_quran_chapters(use_cache=False)
                translations = await self.get_quran_translations(use_cache=False)
                recitations = await self.get_quran_recitations(use_cache=False)
                if repository:
                    await repository.upsert_quran_chapters(chapters["chapters"])
                    await repository.upsert_quran_translations(translations["translations"])
                    await repository.upsert_quran_recitations(recitations["recitations"])
                quran_ayah_count = 0
                quran_triplet_count = 0
                for chapter in chapters["chapters"]:
                    chapter_id = int(chapter["chapter_id"])
                    ayah_payload = await self.get_quran_chapter_ayahs(chapter_id, use_cache=False)
                    triplet_payload = await self.get_quran_triplets(chapter_id, use_cache=False)
                    quran_ayah_count += len(ayah_payload["ayahs"])
                    quran_triplet_count += len(triplet_payload["blocks"])
                    if repository:
                        await repository.upsert_quran_ayahs(
                            chapter_id=chapter_id,
                            translation_id=int(ayah_payload["translation_id"]),
                            recitation_id=int(ayah_payload["recitation_id"]),
                            ayahs=ayah_payload["ayahs"],
                        )
                        await repository.upsert_quran_triplets(
                            chapter_id=chapter_id,
                            translation_id=int(triplet_payload["translation_id"]),
                            recitation_id=int(triplet_payload["recitation_id"]),
                            blocks=triplet_payload["blocks"],
                        )
                sync_metrics.update(
                    {
                        "quran_chapters": len(chapters["chapters"]),
                        "quran_ayahs": quran_ayah_count,
                        "quran_triplets": quran_triplet_count,
                    }
                )
                manifest["steps"].append(
                    {
                        "name": "quran",
                        "chapters": len(chapters["chapters"]),
                        "ayahs": quran_ayah_count,
                        "triplet_blocks": quran_triplet_count,
                        "persisted_to_db": bool(repository),
                        "status": "ok",
                    }
                )
            if include_hadith:
                collections_payload = await self.get_hadith_collections(use_cache=False)
                collections = collections_payload["collections"]
                if repository:
                    await repository.upsert_hadith_collections(collections)
                wanted = {item.lower() for item in hadith_collections or []}
                synced_collections = 0
                synced_books = 0
                synced_hadith_details = 0
                for collection in collections:
                    name = str(collection.get("name") or "")
                    if wanted and name.lower() not in wanted:
                        continue
                    synced_collections += 1
                    books_payload = await self.get_hadith_books(name, use_cache=False)
                    if repository:
                        await repository.upsert_hadith_books(name, books_payload["books"])
                    for book in books_payload["books"]:
                        synced_books += 1
                        page = 1
                        while True:
                            book_payload = await self.get_hadith_book_items(
                                name,
                                str(book["book_number"]),
                                page=page,
                                limit=self.settings.sunnah_page_size,
                                use_cache=False,
                            )
                            for item in book_payload["items"]:
                                hadith_number = str(item.get("hadith_number") or "").strip()
                                if not hadith_number:
                                    continue
                                detail_payload = await self.get_hadith_detail(
                                    name, hadith_number, use_cache=False
                                )
                                if repository:
                                    await repository.upsert_hadith_detail(detail_payload["hadith"])
                                synced_hadith_details += 1
                            total_pages = self._pagination_total_pages(
                                book_payload.get("pagination") or {}
                            )
                            if total_pages is not None and page >= total_pages:
                                break
                            if total_pages is not None:
                                page += 1
                                continue
                            if len(book_payload["items"]) < self.settings.sunnah_page_size:
                                break
                            page += 1
                sync_metrics.update(
                    {
                        "hadith_collections": synced_collections,
                        "hadith_books": synced_books,
                        "hadith_items": synced_hadith_details,
                    }
                )
                manifest["steps"].append(
                    {
                        "name": "hadith",
                        "collections": synced_collections,
                        "books": synced_books,
                        "detail_items": synced_hadith_details,
                        "persisted_to_db": bool(repository),
                        "status": "ok",
                    }
                )
            if include_duas:
                duas = self._load_dua_items()
                categories_payload = await self.get_dua_categories()
                payload = {
                    **self._default_metadata(),
                    "source_api": "local_dua_dataset",
                    "items": duas,
                }
                self._write_cached(payload, "duas", "items.json")
                self._write_cached(categories_payload, "duas", "categories.json")
                if repository:
                    await repository.upsert_dua_categories(categories_payload["categories"])
                    await repository.upsert_dua_items(duas)
                sync_metrics.update(
                    {
                        "dua_categories": len(categories_payload["categories"]),
                        "dua_items": len(duas),
                    }
                )
                manifest["steps"].append(
                    {
                        "name": "duas",
                        "items": len(duas),
                        "categories": len(categories_payload["categories"]),
                        "persisted_to_db": bool(repository),
                        "status": "ok",
                    }
                )
            self._write_cached(manifest, "manifest.json")
            if repository:
                await repository.save_snapshot(
                    source_name="gateway",
                    snapshot_kind="manifest",
                    snapshot_key="latest",
                    request_path="sync_static_content",
                    request_params=sync_scope,
                    response_payload=manifest,
                )
                await repository.finish_sync_run(
                    sync_run_id,
                    status="completed",
                    metrics=sync_metrics,
                )
            logger.info("Islamic content sync completed", extra={"manifest": manifest})
            return manifest
        except Exception as exc:
            if repository:
                await repository.finish_sync_run(
                    sync_run_id,
                    status="failed",
                    metrics=sync_metrics,
                    error_summary=str(exc),
                )
            raise
