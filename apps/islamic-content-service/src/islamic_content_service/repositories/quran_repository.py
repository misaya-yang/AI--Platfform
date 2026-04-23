from __future__ import annotations

import json
from collections import defaultdict
from typing import Any

from ..db import Database
from ..domain.constants import QURAN_SOURCE_API


def _decode_json(value: Any) -> Any:
    if isinstance(value, str):
        return json.loads(value)
    return value


def _normalize_segments(value: Any) -> list[dict[str, int]]:
    raw_segments = list(_decode_json(value) or [])
    normalized = []
    for segment in raw_segments:
        if isinstance(segment, dict):
            word_index = segment.get("word_index")
            start_ms = segment.get("start_ms")
            end_ms = segment.get("end_ms")
        elif isinstance(segment, (list, tuple)) and len(segment) >= 3:
            word_index, start_ms, end_ms = segment[0], segment[1], segment[2]
        else:
            continue
        try:
            normalized.append(
                {
                    "word_index": int(word_index),
                    "start_ms": int(float(start_ms)),
                    "end_ms": int(float(end_ms)),
                }
            )
        except (TypeError, ValueError):
            continue
    return normalized


class QuranRepository:
    def __init__(self, db: Database):
        self.db = db

    async def upsert_chapters(self, chapters: list[dict[str, Any]]) -> None:
        rows = [
            (
                chapter.get("chapter_id"),
                chapter.get("name_simple"),
                chapter.get("name_complex"),
                chapter.get("name_arabic"),
                chapter.get("translated_name"),
                chapter.get("revelation_place"),
                chapter.get("verses_count"),
            )
            for chapter in chapters
            if chapter.get("chapter_id") is not None
        ]
        await self.db.executemany(
            """
            INSERT INTO quran_chapters (
                chapter_id, name_simple, name_complex, name_arabic,
                translated_name, revelation_place, verses_count, updated_at
            )
            VALUES ($1, $2, $3, $4, $5, $6, $7, NOW())
            ON CONFLICT (chapter_id)
            DO UPDATE SET
                name_simple = EXCLUDED.name_simple,
                name_complex = EXCLUDED.name_complex,
                name_arabic = EXCLUDED.name_arabic,
                translated_name = EXCLUDED.translated_name,
                revelation_place = EXCLUDED.revelation_place,
                verses_count = EXCLUDED.verses_count,
                updated_at = NOW()
            """,
            rows,
        )

    async def upsert_translations(self, translations: list[dict[str, Any]]) -> None:
        rows = []
        for item in translations:
            translation_id = item.get("id") or item.get("resource_id")
            if translation_id is None:
                continue
            rows.append(
                (
                    int(translation_id),
                    item.get("name"),
                    item.get("author_name"),
                    item.get("slug"),
                    item.get("language_name") or item.get("language"),
                    json.dumps(item),
                )
            )
        await self.db.executemany(
            """
            INSERT INTO quran_translations (
                translation_id, name, author_name, slug, language_name, raw_payload, updated_at
            )
            VALUES ($1, $2, $3, $4, $5, $6::jsonb, NOW())
            ON CONFLICT (translation_id)
            DO UPDATE SET
                name = EXCLUDED.name,
                author_name = EXCLUDED.author_name,
                slug = EXCLUDED.slug,
                language_name = EXCLUDED.language_name,
                raw_payload = EXCLUDED.raw_payload,
                updated_at = NOW()
            """,
            rows,
        )

    async def upsert_recitations(self, recitations: list[dict[str, Any]]) -> None:
        rows = []
        for item in recitations:
            recitation_id = item.get("id") or item.get("reciter_id")
            if recitation_id is None:
                continue
            translated_name = item.get("translated_name")
            if isinstance(translated_name, dict):
                translated_name = translated_name.get("name")
            rows.append(
                (
                    int(recitation_id),
                    item.get("reciter_name") or item.get("name"),
                    item.get("style"),
                    translated_name,
                    json.dumps(item),
                )
            )
        await self.db.executemany(
            """
            INSERT INTO quran_recitations (
                recitation_id, reciter_name, style, translated_name, raw_payload, updated_at
            )
            VALUES ($1, $2, $3, $4, $5::jsonb, NOW())
            ON CONFLICT (recitation_id)
            DO UPDATE SET
                reciter_name = EXCLUDED.reciter_name,
                style = EXCLUDED.style,
                translated_name = EXCLUDED.translated_name,
                raw_payload = EXCLUDED.raw_payload,
                updated_at = NOW()
            """,
            rows,
        )

    async def upsert_ayah_bases(
        self,
        *,
        chapter_id: int,
        placeholder_translation_id: int,
        placeholder_recitation_id: int,
        ayahs: list[dict[str, Any]],
    ) -> None:
        async def _txn(connection) -> None:
            ayah_rows = []
            word_rows = []
            for ayah in ayahs:
                ayah_rows.append(
                    (
                        ayah.get("verse_key"),
                        chapter_id,
                        ayah.get("ayah_number"),
                        ayah.get("juz_number"),
                        ayah.get("hizb_number"),
                        ayah.get("rub_number"),
                        ayah.get("page_number"),
                        ayah.get("arabic_text") or "",
                        ayah.get("transliteration_text") or "",
                        placeholder_translation_id,
                        "",
                        placeholder_recitation_id,
                        None,
                    )
                )
                for word in ayah.get("words") or []:
                    word_rows.append(
                        (
                            ayah.get("verse_key"),
                            word.get("position"),
                            word.get("arabic"),
                            word.get("arabic_simple"),
                            word.get("transliteration"),
                            word.get("translation"),
                            word.get("audio_url"),
                            word.get("char_type"),
                        )
                    )

            await connection.executemany(
                """
                INSERT INTO quran_ayahs (
                    verse_key, chapter_id, ayah_number, juz_number, hizb_number, rub_number,
                    page_number, arabic_text, transliteration_text, translation_id,
                    translation_text, recitation_id, audio_url, updated_at
                )
                VALUES ($1, $2, $3, $4, $5, $6, $7, $8, $9, $10, $11, $12, $13, NOW())
                ON CONFLICT (verse_key)
                DO UPDATE SET
                    chapter_id = EXCLUDED.chapter_id,
                    ayah_number = EXCLUDED.ayah_number,
                    juz_number = EXCLUDED.juz_number,
                    hizb_number = EXCLUDED.hizb_number,
                    rub_number = EXCLUDED.rub_number,
                    page_number = EXCLUDED.page_number,
                    arabic_text = EXCLUDED.arabic_text,
                    transliteration_text = EXCLUDED.transliteration_text,
                    updated_at = NOW()
                """,
                ayah_rows,
            )

            verse_keys = [row[0] for row in ayah_rows if row[0]]
            if verse_keys:
                await connection.execute(
                    "DELETE FROM quran_words WHERE verse_key = ANY($1::varchar[])",
                    verse_keys,
                )
            if word_rows:
                await connection.executemany(
                    """
                    INSERT INTO quran_words (
                        verse_key, position, arabic, arabic_simple,
                        transliteration, translation, audio_url, char_type, updated_at
                    )
                    VALUES ($1, $2, $3, $4, $5, $6, $7, $8, NOW())
                    ON CONFLICT (verse_key, position)
                    DO UPDATE SET
                        arabic = EXCLUDED.arabic,
                        arabic_simple = EXCLUDED.arabic_simple,
                        transliteration = EXCLUDED.transliteration,
                        translation = EXCLUDED.translation,
                        audio_url = EXCLUDED.audio_url,
                        char_type = EXCLUDED.char_type,
                        updated_at = NOW()
                    """,
                    word_rows,
                )

        await self.db.transaction(_txn)

    async def upsert_ayah_translations(
        self,
        *,
        translation_id: int,
        items: list[dict[str, Any]],
    ) -> None:
        rows = [
            (item.get("verse_key"), translation_id, item.get("translation_text") or "")
            for item in items
            if item.get("verse_key")
        ]
        await self.db.executemany(
            """
            INSERT INTO quran_ayah_translations (
                verse_key, translation_id, translation_text, updated_at
            )
            VALUES ($1, $2, $3, NOW())
            ON CONFLICT (verse_key, translation_id)
            DO UPDATE SET
                translation_text = EXCLUDED.translation_text,
                updated_at = NOW()
            """,
            rows,
        )

    async def upsert_ayah_audio(
        self,
        *,
        recitation_id: int,
        items: list[dict[str, Any]],
    ) -> None:
        rows = [
            (item.get("verse_key"), recitation_id, item.get("audio_url"))
            for item in items
            if item.get("verse_key")
        ]
        await self.db.executemany(
            """
            INSERT INTO quran_ayah_audio (
                verse_key, recitation_id, audio_url, updated_at
            )
            VALUES ($1, $2, $3, NOW())
            ON CONFLICT (verse_key, recitation_id)
            DO UPDATE SET
                audio_url = EXCLUDED.audio_url,
                updated_at = NOW()
            """,
            rows,
        )

    async def upsert_chapter_audio_track(
        self,
        chapter_id: int,
        *,
        recitation_id: int,
        audio_url: str | None,
    ) -> None:
        await self.db.execute(
            """
            INSERT INTO quran_chapter_audio_tracks (
                chapter_id, recitation_id, audio_url, updated_at
            )
            VALUES ($1, $2, $3, NOW())
            ON CONFLICT (chapter_id, recitation_id)
            DO UPDATE SET
                audio_url = EXCLUDED.audio_url,
                updated_at = NOW()
            """,
            chapter_id,
            recitation_id,
            audio_url,
        )
        await self.db.execute(
            """
            UPDATE quran_chapters
            SET default_recitation_id = $2,
                default_audio_url = $3,
                updated_at = NOW()
            WHERE chapter_id = $1
            """,
            chapter_id,
            recitation_id,
            audio_url,
        )

    async def upsert_chapter_audio_timings(
        self,
        *,
        chapter_id: int,
        recitation_id: int,
        timings: list[dict[str, Any]],
    ) -> None:
        async def _txn(connection) -> None:
            await connection.execute(
                """
                DELETE FROM quran_chapter_audio_timings
                WHERE chapter_id = $1 AND recitation_id = $2
                """,
                chapter_id,
                recitation_id,
            )
            rows = [
                (
                    chapter_id,
                    recitation_id,
                    timing.get("verse_key"),
                    timing.get("timestamp_from_ms"),
                    timing.get("timestamp_to_ms"),
                    timing.get("duration_ms"),
                    json.dumps(timing.get("segments") or []),
                )
                for timing in timings
                if timing.get("verse_key")
            ]
            if rows:
                await connection.executemany(
                    """
                    INSERT INTO quran_chapter_audio_timings (
                        chapter_id, recitation_id, verse_key, timestamp_from_ms,
                        timestamp_to_ms, duration_ms, segments_json, updated_at
                    )
                    VALUES ($1, $2, $3, $4, $5, $6, $7::jsonb, NOW())
                    """,
                    rows,
                )

        await self.db.transaction(_txn)

    async def upsert_triplet_ranges(self, *, chapter_id: int, ranges: list[dict[str, Any]]) -> None:
        rows = [
            (
                item.get("block_id"),
                chapter_id,
                item.get("start_ayah_number"),
                item.get("end_ayah_number"),
                json.dumps(item.get("verse_keys") or []),
            )
            for item in ranges
            if item.get("block_id")
        ]
        await self.db.executemany(
            """
            INSERT INTO quran_triplet_ranges (
                block_id, chapter_id, start_ayah_number, end_ayah_number, verse_keys_json, updated_at
            )
            VALUES ($1, $2, $3, $4, $5::jsonb, NOW())
            ON CONFLICT (block_id)
            DO UPDATE SET
                chapter_id = EXCLUDED.chapter_id,
                start_ayah_number = EXCLUDED.start_ayah_number,
                end_ayah_number = EXCLUDED.end_ayah_number,
                verse_keys_json = EXCLUDED.verse_keys_json,
                updated_at = NOW()
            """,
            rows,
        )

    async def get_chapters(self) -> list[dict[str, Any]]:
        rows = await self.db.fetch(
            """
            SELECT chapter_id, name_simple, name_complex, name_arabic, translated_name,
                   revelation_place, verses_count
            FROM quran_chapters
            ORDER BY chapter_id
            """
        )
        return [dict(row) for row in rows]

    async def get_translations(self) -> list[dict[str, Any]]:
        rows = await self.db.fetch(
            """
            SELECT translation_id, name, author_name, slug, language_name
            FROM quran_translations
            ORDER BY translation_id
            """
        )
        return [
            {
                "id": row["translation_id"],
                "name": row["name"],
                "author_name": row["author_name"],
                "slug": row["slug"],
                "language_name": row["language_name"],
            }
            for row in rows
        ]

    async def get_recitations(self) -> list[dict[str, Any]]:
        rows = await self.db.fetch(
            """
            SELECT recitation_id, reciter_name, style, translated_name
            FROM quran_recitations
            ORDER BY recitation_id
            """
        )
        return [
            {
                "id": row["recitation_id"],
                "reciter_name": row["reciter_name"],
                "style": row["style"],
                "translated_name": row["translated_name"],
            }
            for row in rows
        ]

    async def get_synced_translation_ids(self) -> list[int]:
        rows = await self.db.fetch(
            """
            SELECT DISTINCT translation_id
            FROM quran_ayah_translations
            ORDER BY translation_id
            """
        )
        return [int(row["translation_id"]) for row in rows]

    async def get_synced_recitation_ids(self) -> list[int]:
        rows = await self.db.fetch(
            """
            SELECT DISTINCT recitation_id
            FROM quran_ayah_audio
            ORDER BY recitation_id
            """
        )
        return [int(row["recitation_id"]) for row in rows]

    async def has_translation(self, translation_id: int) -> bool:
        value = await self.db.fetchval(
            """
            SELECT 1
            FROM quran_ayah_translations
            WHERE translation_id = $1
            LIMIT 1
            """,
            translation_id,
        )
        return bool(value)

    async def has_recitation(self, recitation_id: int) -> bool:
        value = await self.db.fetchval(
            """
            SELECT 1
            FROM quran_ayah_audio
            WHERE recitation_id = $1
            LIMIT 1
            """,
            recitation_id,
        )
        return bool(value)

    async def _get_words_by_verse(self, verse_keys: list[str]) -> dict[str, list[dict[str, Any]]]:
        if not verse_keys:
            return {}
        rows = await self.db.fetch(
            """
            SELECT verse_key, position, arabic, arabic_simple, transliteration,
                   translation, audio_url, char_type
            FROM quran_words
            WHERE verse_key = ANY($1::varchar[])
            ORDER BY verse_key, position
            """,
            verse_keys,
        )
        grouped: dict[str, list[dict[str, Any]]] = defaultdict(list)
        for row in rows:
            grouped[row["verse_key"]].append(
                {
                    "position": row["position"],
                    "arabic": row["arabic"],
                    "arabic_simple": row["arabic_simple"],
                    "transliteration": row["transliteration"],
                    "translation": row["translation"],
                    "audio_url": row["audio_url"],
                    "char_type": row["char_type"],
                }
            )
        return grouped

    async def _get_timings_by_verse(
        self,
        verse_keys: list[str],
        *,
        recitation_id: int,
    ) -> dict[str, dict[str, Any]]:
        if not verse_keys:
            return {}
        rows = await self.db.fetch(
            """
            SELECT verse_key, timestamp_from_ms, timestamp_to_ms, duration_ms, segments_json
            FROM quran_chapter_audio_timings
            WHERE recitation_id = $2
              AND verse_key = ANY($1::varchar[])
            ORDER BY timestamp_from_ms, verse_key
            """,
            verse_keys,
            recitation_id,
        )
        return {
            row["verse_key"]: {
                "verse_key": row["verse_key"],
                "timestamp_from_ms": row["timestamp_from_ms"],
                "timestamp_to_ms": row["timestamp_to_ms"],
                "duration_ms": row["duration_ms"],
                "segments": _normalize_segments(row["segments_json"]),
            }
            for row in rows
        }

    async def _get_audio_by_verse(
        self,
        verse_keys: list[str],
        *,
        recitation_id: int,
    ) -> dict[str, str | None]:
        if not verse_keys:
            return {}
        rows = await self.db.fetch(
            """
            SELECT verse_key, audio_url
            FROM quran_ayah_audio
            WHERE recitation_id = $2
              AND verse_key = ANY($1::varchar[])
            """,
            verse_keys,
            recitation_id,
        )
        return {row["verse_key"]: row["audio_url"] for row in rows}

    async def _get_translations_by_verse(
        self,
        verse_keys: list[str],
        *,
        translation_id: int,
    ) -> dict[str, str]:
        if not verse_keys:
            return {}
        rows = await self.db.fetch(
            """
            SELECT verse_key, translation_text
            FROM quran_ayah_translations
            WHERE translation_id = $2
              AND verse_key = ANY($1::varchar[])
            """,
            verse_keys,
            translation_id,
        )
        return {row["verse_key"]: row["translation_text"] for row in rows}

    def _attach_segments_to_words(
        self,
        words: list[dict[str, Any]],
        timing: dict[str, Any] | None,
    ) -> list[dict[str, Any]]:
        if not timing:
            return words
        segment_map = {
            segment["word_index"]: segment
            for segment in timing.get("segments") or []
            if segment.get("word_index") is not None
        }
        enriched = []
        for word in words:
            word_copy = dict(word)
            position = word_copy.get("position")
            if position in segment_map:
                word_copy["segment"] = segment_map[position]
            enriched.append(word_copy)
        return enriched

    def _build_ayah_payload(
        self,
        row: Any,
        *,
        translation_id: int,
        recitation_id: int,
        translation_text: str,
        audio_url: str | None,
        words_by_verse: dict[str, list[dict[str, Any]]],
        timings_by_verse: dict[str, dict[str, Any]],
    ) -> dict[str, Any]:
        verse_key = row["verse_key"]
        timing = timings_by_verse.get(verse_key)
        words = self._attach_segments_to_words(words_by_verse.get(verse_key, []), timing)
        return {
            "source_api": QURAN_SOURCE_API,
            "source_type": "quran",
            "verse_key": verse_key,
            "surah_number": row["chapter_id"],
            "ayah_number": row["ayah_number"],
            "juz_number": row["juz_number"],
            "hizb_number": row["hizb_number"],
            "rub_number": row["rub_number"],
            "page_number": row["page_number"],
            "arabic_text": row["arabic_text"],
            "transliteration_text": row["transliteration_text"],
            "translation_text": translation_text,
            "words": words,
            "timing": timing,
            "audio": {
                "recitation_id": recitation_id,
                "translation_id": translation_id,
                "url": audio_url,
            },
        }

    async def get_chapter_ayahs(
        self,
        chapter_id: int,
        *,
        translation_id: int,
        recitation_id: int,
    ) -> list[dict[str, Any]]:
        rows = await self.db.fetch(
            """
            SELECT verse_key, chapter_id, ayah_number, juz_number, hizb_number,
                   rub_number, page_number, arabic_text, transliteration_text
            FROM quran_ayahs
            WHERE chapter_id = $1
            ORDER BY ayah_number
            """,
            chapter_id,
        )
        verse_keys = [row["verse_key"] for row in rows]
        words = await self._get_words_by_verse(verse_keys)
        timings = await self._get_timings_by_verse(verse_keys, recitation_id=recitation_id)
        translation_map = await self._get_translations_by_verse(
            verse_keys,
            translation_id=translation_id,
        )
        audio_map = await self._get_audio_by_verse(verse_keys, recitation_id=recitation_id)
        return [
            self._build_ayah_payload(
                row,
                translation_id=translation_id,
                recitation_id=recitation_id,
                translation_text=translation_map.get(row["verse_key"], ""),
                audio_url=audio_map.get(row["verse_key"]),
                words_by_verse=words,
                timings_by_verse=timings,
            )
            for row in rows
        ]

    async def get_juz_ayahs(
        self,
        juz_number: int,
        *,
        translation_id: int,
        recitation_id: int,
    ) -> list[dict[str, Any]]:
        rows = await self.db.fetch(
            """
            SELECT verse_key, chapter_id, ayah_number, juz_number, hizb_number,
                   rub_number, page_number, arabic_text, transliteration_text
            FROM quran_ayahs
            WHERE juz_number = $1
            ORDER BY chapter_id, ayah_number
            """,
            juz_number,
        )
        verse_keys = [row["verse_key"] for row in rows]
        words = await self._get_words_by_verse(verse_keys)
        timings = await self._get_timings_by_verse(verse_keys, recitation_id=recitation_id)
        translation_map = await self._get_translations_by_verse(
            verse_keys,
            translation_id=translation_id,
        )
        audio_map = await self._get_audio_by_verse(verse_keys, recitation_id=recitation_id)
        return [
            self._build_ayah_payload(
                row,
                translation_id=translation_id,
                recitation_id=recitation_id,
                translation_text=translation_map.get(row["verse_key"], ""),
                audio_url=audio_map.get(row["verse_key"]),
                words_by_verse=words,
                timings_by_verse=timings,
            )
            for row in rows
        ]

    async def get_ayah(
        self,
        verse_key: str,
        *,
        translation_id: int,
        recitation_id: int,
    ) -> dict[str, Any] | None:
        row = await self.db.fetchrow(
            """
            SELECT verse_key, chapter_id, ayah_number, juz_number, hizb_number,
                   rub_number, page_number, arabic_text, transliteration_text
            FROM quran_ayahs
            WHERE verse_key = $1
            """,
            verse_key,
        )
        if row is None:
            return None
        words = await self._get_words_by_verse([verse_key])
        timings = await self._get_timings_by_verse([verse_key], recitation_id=recitation_id)
        translation_map = await self._get_translations_by_verse(
            [verse_key],
            translation_id=translation_id,
        )
        audio_map = await self._get_audio_by_verse([verse_key], recitation_id=recitation_id)
        return self._build_ayah_payload(
            row,
            translation_id=translation_id,
            recitation_id=recitation_id,
            translation_text=translation_map.get(verse_key, ""),
            audio_url=audio_map.get(verse_key),
            words_by_verse=words,
            timings_by_verse=timings,
        )

    async def get_triplet_ranges(self, chapter_id: int) -> list[dict[str, Any]]:
        rows = await self.db.fetch(
            """
            SELECT block_id, chapter_id, start_ayah_number, end_ayah_number, verse_keys_json
            FROM quran_triplet_ranges
            WHERE chapter_id = $1
            ORDER BY start_ayah_number
            """,
            chapter_id,
        )
        return [
            {
                "block_id": row["block_id"],
                "chapter_id": row["chapter_id"],
                "start_ayah_number": row["start_ayah_number"],
                "end_ayah_number": row["end_ayah_number"],
                "verse_keys": list(_decode_json(row["verse_keys_json"]) or []),
            }
            for row in rows
        ]

    async def list_juz_summaries(self) -> list[dict[str, Any]]:
        """Aggregate one row per Juz from ``quran_ayahs`` + ``quran_chapters``.

        Returns a list sorted by juz_number, with start/end verse keys, per-chapter
        ayah ranges, and the first ayah's chapter name joined in. No persistent
        ``quran_juzs`` table — the 30-row aggregation runs in-process and is
        cached by the service layer.
        """
        rows = await self.db.fetch(
            """
            WITH per_juz_chapter AS (
                SELECT a.juz_number,
                       a.chapter_id,
                       MIN(a.ayah_number) AS first_ayah,
                       MAX(a.ayah_number) AS last_ayah,
                       COUNT(*) AS ayah_count
                FROM quran_ayahs a
                WHERE a.juz_number IS NOT NULL
                GROUP BY a.juz_number, a.chapter_id
            ),
            juz_totals AS (
                SELECT juz_number,
                       SUM(ayah_count)::INTEGER AS verses_count
                FROM per_juz_chapter
                GROUP BY juz_number
            ),
            juz_bounds AS (
                SELECT pc.juz_number,
                       (SELECT pc2.chapter_id || ':' || pc2.first_ayah
                          FROM per_juz_chapter pc2
                         WHERE pc2.juz_number = pc.juz_number
                         ORDER BY pc2.chapter_id ASC
                         LIMIT 1) AS first_verse_key,
                       (SELECT pc2.chapter_id || ':' || pc2.last_ayah
                          FROM per_juz_chapter pc2
                         WHERE pc2.juz_number = pc.juz_number
                         ORDER BY pc2.chapter_id DESC
                         LIMIT 1) AS last_verse_key,
                       (SELECT pc2.chapter_id
                          FROM per_juz_chapter pc2
                         WHERE pc2.juz_number = pc.juz_number
                         ORDER BY pc2.chapter_id ASC
                         LIMIT 1) AS start_chapter_id,
                       (SELECT pc2.first_ayah
                          FROM per_juz_chapter pc2
                         WHERE pc2.juz_number = pc.juz_number
                         ORDER BY pc2.chapter_id ASC
                         LIMIT 1) AS start_ayah_number
                FROM per_juz_chapter pc
                GROUP BY pc.juz_number
            )
            SELECT jt.juz_number,
                   jt.verses_count,
                   jb.first_verse_key,
                   jb.last_verse_key,
                   jb.start_chapter_id,
                   jb.start_ayah_number,
                   c.name_simple AS start_chapter_name_simple,
                   c.name_arabic AS start_chapter_name_arabic
            FROM juz_totals jt
            JOIN juz_bounds jb USING (juz_number)
            LEFT JOIN quran_chapters c ON c.chapter_id = jb.start_chapter_id
            ORDER BY jt.juz_number
            """
        )

        mapping_rows = await self.db.fetch(
            """
            SELECT juz_number,
                   chapter_id,
                   MIN(ayah_number) AS first_ayah,
                   MAX(ayah_number) AS last_ayah
            FROM quran_ayahs
            WHERE juz_number IS NOT NULL
            GROUP BY juz_number, chapter_id
            ORDER BY juz_number, chapter_id
            """
        )
        verse_mapping: dict[int, dict[str, str]] = defaultdict(dict)
        for m in mapping_rows:
            verse_mapping[m["juz_number"]][str(m["chapter_id"])] = (
                f"{m['first_ayah']}-{m['last_ayah']}"
                if m["first_ayah"] != m["last_ayah"]
                else f"{m['first_ayah']}"
            )

        return [
            {
                "juz_number": row["juz_number"],
                "verses_count": row["verses_count"],
                "first_verse_key": row["first_verse_key"],
                "last_verse_key": row["last_verse_key"],
                "start_chapter_id": row["start_chapter_id"],
                "start_ayah_number": row["start_ayah_number"],
                "start_chapter_name_simple": row["start_chapter_name_simple"],
                "start_chapter_name_arabic": row["start_chapter_name_arabic"],
                "verse_mapping": dict(verse_mapping.get(row["juz_number"], {})),
            }
            for row in rows
        ]

    async def get_chapter(self, chapter_id: int) -> dict[str, Any] | None:
        row = await self.db.fetchrow(
            """
            SELECT chapter_id, name_simple, name_complex, name_arabic, translated_name,
                   revelation_place, verses_count
            FROM quran_chapters
            WHERE chapter_id = $1
            """,
            chapter_id,
        )
        return dict(row) if row else None

    async def get_random_ayah(
        self,
        *,
        translation_id: int,
        recitation_id: int,
    ) -> dict[str, Any] | None:
        row = await self.db.fetchrow(
            """
            SELECT verse_key, chapter_id, ayah_number, juz_number, hizb_number,
                   rub_number, page_number, arabic_text, transliteration_text
            FROM quran_ayahs
            ORDER BY random()
            LIMIT 1
            """
        )
        if row is None:
            return None
        verse_key = row["verse_key"]
        words = await self._get_words_by_verse([verse_key])
        timings = await self._get_timings_by_verse([verse_key], recitation_id=recitation_id)
        translation_map = await self._get_translations_by_verse(
            [verse_key], translation_id=translation_id,
        )
        audio_map = await self._get_audio_by_verse([verse_key], recitation_id=recitation_id)
        return self._build_ayah_payload(
            row,
            translation_id=translation_id,
            recitation_id=recitation_id,
            translation_text=translation_map.get(verse_key, ""),
            audio_url=audio_map.get(verse_key),
            words_by_verse=words,
            timings_by_verse=timings,
        )

    async def get_page_ayahs(
        self,
        page_number: int,
        *,
        translation_id: int,
        recitation_id: int,
    ) -> list[dict[str, Any]]:
        rows = await self.db.fetch(
            """
            SELECT verse_key, chapter_id, ayah_number, juz_number, hizb_number,
                   rub_number, page_number, arabic_text, transliteration_text
            FROM quran_ayahs
            WHERE page_number = $1
            ORDER BY chapter_id, ayah_number
            """,
            page_number,
        )
        verse_keys = [row["verse_key"] for row in rows]
        words = await self._get_words_by_verse(verse_keys)
        timings = await self._get_timings_by_verse(verse_keys, recitation_id=recitation_id)
        translation_map = await self._get_translations_by_verse(
            verse_keys, translation_id=translation_id,
        )
        audio_map = await self._get_audio_by_verse(verse_keys, recitation_id=recitation_id)
        return [
            self._build_ayah_payload(
                row,
                translation_id=translation_id,
                recitation_id=recitation_id,
                translation_text=translation_map.get(row["verse_key"], ""),
                audio_url=audio_map.get(row["verse_key"]),
                words_by_verse=words,
                timings_by_verse=timings,
            )
            for row in rows
        ]

    async def get_ayahs_range(
        self,
        *,
        from_chapter: int,
        from_ayah: int,
        to_chapter: int,
        to_ayah: int,
        translation_id: int,
        recitation_id: int,
    ) -> list[dict[str, Any]]:
        """Range across possibly multiple surahs using (chapter, ayah) tuple comparison."""
        rows = await self.db.fetch(
            """
            SELECT verse_key, chapter_id, ayah_number, juz_number, hizb_number,
                   rub_number, page_number, arabic_text, transliteration_text
            FROM quran_ayahs
            WHERE (chapter_id, ayah_number) >= ($1, $2)
              AND (chapter_id, ayah_number) <= ($3, $4)
            ORDER BY chapter_id, ayah_number
            """,
            from_chapter, from_ayah, to_chapter, to_ayah,
        )
        verse_keys = [row["verse_key"] for row in rows]
        words = await self._get_words_by_verse(verse_keys)
        timings = await self._get_timings_by_verse(verse_keys, recitation_id=recitation_id)
        translation_map = await self._get_translations_by_verse(
            verse_keys, translation_id=translation_id,
        )
        audio_map = await self._get_audio_by_verse(verse_keys, recitation_id=recitation_id)
        return [
            self._build_ayah_payload(
                row,
                translation_id=translation_id,
                recitation_id=recitation_id,
                translation_text=translation_map.get(row["verse_key"], ""),
                audio_url=audio_map.get(row["verse_key"]),
                words_by_verse=words,
                timings_by_verse=timings,
            )
            for row in rows
        ]

    async def list_hizb_summaries(self) -> list[dict[str, Any]]:
        """60 hizbs aggregated from ``quran_ayahs.hizb_number``."""
        rows = await self.db.fetch(
            """
            WITH per_hizb_chapter AS (
                SELECT hizb_number, chapter_id,
                       MIN(ayah_number) AS first_ayah,
                       MAX(ayah_number) AS last_ayah,
                       COUNT(*) AS ayah_count
                FROM quran_ayahs
                WHERE hizb_number IS NOT NULL
                GROUP BY hizb_number, chapter_id
            ),
            totals AS (
                SELECT hizb_number, SUM(ayah_count)::INTEGER AS verses_count
                FROM per_hizb_chapter GROUP BY hizb_number
            ),
            bounds AS (
                SELECT h.hizb_number,
                       (SELECT p.chapter_id || ':' || p.first_ayah
                          FROM per_hizb_chapter p
                         WHERE p.hizb_number = h.hizb_number
                         ORDER BY p.chapter_id ASC LIMIT 1) AS first_verse_key,
                       (SELECT p.chapter_id || ':' || p.last_ayah
                          FROM per_hizb_chapter p
                         WHERE p.hizb_number = h.hizb_number
                         ORDER BY p.chapter_id DESC LIMIT 1) AS last_verse_key,
                       (SELECT p.chapter_id
                          FROM per_hizb_chapter p
                         WHERE p.hizb_number = h.hizb_number
                         ORDER BY p.chapter_id ASC LIMIT 1) AS start_chapter_id,
                       (SELECT p.first_ayah
                          FROM per_hizb_chapter p
                         WHERE p.hizb_number = h.hizb_number
                         ORDER BY p.chapter_id ASC LIMIT 1) AS start_ayah_number
                FROM per_hizb_chapter h GROUP BY h.hizb_number
            )
            SELECT t.hizb_number, t.verses_count,
                   b.first_verse_key, b.last_verse_key,
                   b.start_chapter_id, b.start_ayah_number,
                   c.name_simple AS start_chapter_name_simple,
                   c.name_arabic AS start_chapter_name_arabic,
                   ((t.hizb_number - 1) / 2 + 1) AS juz_number
            FROM totals t
            JOIN bounds b USING (hizb_number)
            LEFT JOIN quran_chapters c ON c.chapter_id = b.start_chapter_id
            ORDER BY t.hizb_number
            """
        )
        return [dict(row) for row in rows]

    async def get_ayahs_by_verse_keys(
        self,
        verse_keys: list[str],
        *,
        translation_id: int,
        recitation_id: int,
    ) -> list[dict[str, Any]]:
        if not verse_keys:
            return []
        rows = await self.db.fetch(
            """
            SELECT verse_key, chapter_id, ayah_number, juz_number, hizb_number,
                   rub_number, page_number, arabic_text, transliteration_text
            FROM quran_ayahs
            WHERE verse_key = ANY($1::varchar[])
            ORDER BY chapter_id, ayah_number
            """,
            verse_keys,
        )
        words = await self._get_words_by_verse(verse_keys)
        timings = await self._get_timings_by_verse(verse_keys, recitation_id=recitation_id)
        translation_map = await self._get_translations_by_verse(
            verse_keys, translation_id=translation_id,
        )
        audio_map = await self._get_audio_by_verse(verse_keys, recitation_id=recitation_id)
        return [
            self._build_ayah_payload(
                row,
                translation_id=translation_id,
                recitation_id=recitation_id,
                translation_text=translation_map.get(row["verse_key"], ""),
                audio_url=audio_map.get(row["verse_key"]),
                words_by_verse=words,
                timings_by_verse=timings,
            )
            for row in rows
        ]

    async def search_ayahs(
        self,
        query: str,
        *,
        translation_id: int,
        limit: int,
        offset: int,
    ) -> tuple[list[dict[str, Any]], int]:
        """Case-insensitive search across arabic_text + given translation.

        Returns (items, total). Each item is a minimal search hit with verse_key,
        surah/ayah, the matched snippet, and the chapter name for UI display.
        Ranking: arabic matches first, then translation matches.
        """
        pattern = f"%{query}%"
        rows = await self.db.fetch(
            """
            WITH hits AS (
                SELECT a.verse_key, a.chapter_id, a.ayah_number,
                       a.arabic_text,
                       COALESCE(t.translation_text, '') AS translation_text,
                       CASE
                           WHEN a.arabic_text ILIKE $1 THEN 1
                           WHEN t.translation_text ILIKE $1 THEN 2
                           ELSE 3
                       END AS rank
                FROM quran_ayahs a
                LEFT JOIN quran_ayah_translations t
                    ON t.verse_key = a.verse_key AND t.translation_id = $2
                WHERE a.arabic_text ILIKE $1
                   OR t.translation_text ILIKE $1
            )
            SELECT h.verse_key, h.chapter_id, h.ayah_number,
                   h.arabic_text, h.translation_text, h.rank,
                   c.name_simple, c.name_arabic
            FROM hits h
            LEFT JOIN quran_chapters c ON c.chapter_id = h.chapter_id
            ORDER BY h.rank, h.chapter_id, h.ayah_number
            LIMIT $3 OFFSET $4
            """,
            pattern,
            translation_id,
            limit,
            offset,
        )
        total = await self.db.fetchval(
            """
            SELECT COUNT(*)
            FROM quran_ayahs a
            LEFT JOIN quran_ayah_translations t
                ON t.verse_key = a.verse_key AND t.translation_id = $2
            WHERE a.arabic_text ILIKE $1
               OR t.translation_text ILIKE $1
            """,
            pattern,
            translation_id,
        )
        items = [
            {
                "verse_key": row["verse_key"],
                "surah_number": row["chapter_id"],
                "ayah_number": row["ayah_number"],
                "chapter_name_simple": row["name_simple"],
                "chapter_name_arabic": row["name_arabic"],
                "arabic_text": row["arabic_text"],
                "translation_text": row["translation_text"],
                "match_field": "arabic" if row["rank"] == 1 else "translation",
            }
            for row in rows
        ]
        return items, int(total or 0)

    async def get_chapter_audio(
        self,
        chapter_id: int,
        *,
        recitation_id: int,
    ) -> dict[str, Any] | None:
        row = await self.db.fetchrow(
            """
            SELECT chapter_id, recitation_id, audio_url
            FROM quran_chapter_audio_tracks
            WHERE chapter_id = $1
              AND recitation_id = $2
            """,
            chapter_id,
            recitation_id,
        )
        if row is None:
            fallback = await self.db.fetchrow(
                """
                SELECT chapter_id, default_recitation_id, default_audio_url
                FROM quran_chapters
                WHERE chapter_id = $1
                  AND default_recitation_id = $2
                """,
                chapter_id,
                recitation_id,
            )
            if fallback is None:
                return None
            row = {
                "chapter_id": fallback["chapter_id"],
                "recitation_id": fallback["default_recitation_id"],
                "audio_url": fallback["default_audio_url"],
            }

        timing_rows = await self.db.fetch(
            """
            SELECT verse_key, timestamp_from_ms, timestamp_to_ms, duration_ms, segments_json
            FROM quran_chapter_audio_timings
            WHERE chapter_id = $1
              AND recitation_id = $2
            ORDER BY timestamp_from_ms, verse_key
            """,
            chapter_id,
            recitation_id,
        )
        return {
            "chapter_id": int(row["chapter_id"]),
            "recitation_id": int(row["recitation_id"]) if row["recitation_id"] is not None else None,
            "audio_url": row["audio_url"],
            "source_api": QURAN_SOURCE_API,
            "timings": [
                {
                    "verse_key": timing["verse_key"],
                    "timestamp_from_ms": timing["timestamp_from_ms"],
                    "timestamp_to_ms": timing["timestamp_to_ms"],
                    "duration_ms": timing["duration_ms"],
                    "segments": _normalize_segments(timing["segments_json"]),
                }
                for timing in timing_rows
            ],
        }
