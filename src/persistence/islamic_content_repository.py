from __future__ import annotations

import json
import uuid
from datetime import datetime, timezone
from typing import Any

from .database import DatabaseStorage


class IslamicContentRepository:
    """Canonical PostgreSQL persistence for Islamic content."""

    def __init__(self, database: DatabaseStorage):
        self.database = database

    @property
    def enabled(self) -> bool:
        return bool(self.database and getattr(self.database, "_pool", None))

    async def start_sync_run(self, source_name: str, scope: dict[str, Any]) -> str:
        if not self.enabled:
            return ""
        run_id = uuid.uuid4().hex
        await self.database.execute(
            """
            INSERT INTO source_sync_runs (run_id, source_name, scope, status, started_at)
            VALUES ($1, $2, $3::jsonb, 'running', NOW())
            """,
            run_id,
            source_name,
            json.dumps(scope),
        )
        return run_id

    async def finish_sync_run(
        self,
        run_id: str,
        *,
        status: str,
        metrics: dict[str, Any],
        error_summary: str | None = None,
    ) -> None:
        if not self.enabled or not run_id:
            return
        await self.database.execute(
            """
            UPDATE source_sync_runs
            SET status = $2,
                metrics_json = $3::jsonb,
                error_summary = $4,
                completed_at = NOW()
            WHERE run_id = $1
            """,
            run_id,
            status,
            json.dumps(metrics),
            error_summary,
        )

    async def save_snapshot(
        self,
        *,
        source_name: str,
        snapshot_kind: str,
        snapshot_key: str,
        request_path: str,
        request_params: dict[str, Any] | None,
        response_payload: dict[str, Any],
    ) -> None:
        if not self.enabled:
            return
        await self.database.execute(
            """
            INSERT INTO source_snapshots (
                source_name, snapshot_kind, snapshot_key, request_path,
                request_params_json, response_json, fetched_at
            )
            VALUES ($1, $2, $3, $4, $5::jsonb, $6::jsonb, NOW())
            ON CONFLICT (source_name, snapshot_kind, snapshot_key)
            DO UPDATE SET
                request_path = EXCLUDED.request_path,
                request_params_json = EXCLUDED.request_params_json,
                response_json = EXCLUDED.response_json,
                fetched_at = NOW()
            """,
            source_name,
            snapshot_kind,
            snapshot_key,
            request_path,
            json.dumps(request_params or {}),
            json.dumps(response_payload),
        )

    async def upsert_quran_chapters(self, chapters: list[dict[str, Any]]) -> None:
        if not self.enabled or not chapters:
            return
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
        ]
        await self.database.executemany(
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

    async def upsert_quran_translations(self, translations: list[dict[str, Any]]) -> None:
        if not self.enabled or not translations:
            return
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
        if not rows:
            return
        await self.database.executemany(
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

    async def upsert_quran_recitations(self, recitations: list[dict[str, Any]]) -> None:
        if not self.enabled or not recitations:
            return
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
        if not rows:
            return
        await self.database.executemany(
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

    async def upsert_quran_ayahs(
        self,
        *,
        chapter_id: int,
        translation_id: int,
        recitation_id: int,
        ayahs: list[dict[str, Any]],
    ) -> None:
        if not self.enabled or not ayahs:
            return
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
                    translation_id,
                    ayah.get("translation_text") or "",
                    recitation_id,
                    ((ayah.get("audio") or {}).get("url")),
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

        async with self.database._pool.acquire() as conn:
            async with conn.transaction():
                await conn.executemany(
                    """
                    INSERT INTO quran_ayahs (
                        verse_key, chapter_id, ayah_number, juz_number, hizb_number, rub_number,
                        page_number, arabic_text, transliteration_text, default_translation_id,
                        translation_text, default_recitation_id, audio_url, updated_at
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
                        default_translation_id = EXCLUDED.default_translation_id,
                        translation_text = EXCLUDED.translation_text,
                        default_recitation_id = EXCLUDED.default_recitation_id,
                        audio_url = EXCLUDED.audio_url,
                        updated_at = NOW()
                    """,
                    ayah_rows,
                )
                verse_keys = [row[0] for row in ayah_rows if row[0]]
                if verse_keys:
                    await conn.execute(
                        "DELETE FROM quran_words WHERE verse_key = ANY($1::varchar[])",
                        verse_keys,
                    )
                if word_rows:
                    await conn.executemany(
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

    async def upsert_quran_triplets(
        self,
        *,
        chapter_id: int,
        translation_id: int,
        recitation_id: int,
        blocks: list[dict[str, Any]],
    ) -> None:
        if not self.enabled or not blocks:
            return
        rows = []
        for block in blocks:
            verse_keys = block.get("verse_keys") or []
            if not verse_keys:
                continue
            first = str(verse_keys[0]).split(":")[-1]
            last = str(verse_keys[-1]).split(":")[-1]
            rows.append(
                (
                    block.get("block_id"),
                    chapter_id,
                    int(first),
                    int(last),
                    translation_id,
                    recitation_id,
                    json.dumps(verse_keys),
                    block.get("arabic_text") or "",
                    block.get("transliteration_text") or "",
                    block.get("translation_text") or "",
                    json.dumps(block.get("audio_urls") or []),
                )
            )
        if not rows:
            return
        await self.database.executemany(
            """
            INSERT INTO quran_triplet_blocks (
                block_id, chapter_id, start_ayah_number, end_ayah_number,
                translation_id, recitation_id, verse_keys_json, arabic_text,
                transliteration_text, translation_text, audio_urls_json, updated_at
            )
            VALUES ($1, $2, $3, $4, $5, $6, $7::jsonb, $8, $9, $10, $11::jsonb, NOW())
            ON CONFLICT (block_id)
            DO UPDATE SET
                chapter_id = EXCLUDED.chapter_id,
                start_ayah_number = EXCLUDED.start_ayah_number,
                end_ayah_number = EXCLUDED.end_ayah_number,
                translation_id = EXCLUDED.translation_id,
                recitation_id = EXCLUDED.recitation_id,
                verse_keys_json = EXCLUDED.verse_keys_json,
                arabic_text = EXCLUDED.arabic_text,
                transliteration_text = EXCLUDED.transliteration_text,
                translation_text = EXCLUDED.translation_text,
                audio_urls_json = EXCLUDED.audio_urls_json,
                updated_at = NOW()
            """,
            rows,
        )

    async def upsert_hadith_collections(self, collections: list[dict[str, Any]]) -> None:
        if not self.enabled or not collections:
            return
        rows = [
            (
                item.get("name"),
                item.get("title"),
                item.get("short_intro"),
                item.get("has_books"),
                item.get("has_chapters"),
                item.get("total_books"),
                item.get("total_hadith"),
            )
            for item in collections
            if item.get("name")
        ]
        if not rows:
            return
        await self.database.executemany(
            """
            INSERT INTO hadith_collections (
                name, title, short_intro, has_books, has_chapters,
                total_books, total_hadith, updated_at
            )
            VALUES ($1, $2, $3, $4, $5, $6, $7, NOW())
            ON CONFLICT (name)
            DO UPDATE SET
                title = EXCLUDED.title,
                short_intro = EXCLUDED.short_intro,
                has_books = EXCLUDED.has_books,
                has_chapters = EXCLUDED.has_chapters,
                total_books = EXCLUDED.total_books,
                total_hadith = EXCLUDED.total_hadith,
                updated_at = NOW()
            """,
            rows,
        )

    async def upsert_hadith_books(
        self,
        collection_name: str,
        books: list[dict[str, Any]],
    ) -> None:
        if not self.enabled or not books:
            return
        rows = [
            (
                collection_name,
                item.get("book_number"),
                item.get("title"),
                item.get("hadith_start_number"),
                item.get("hadith_end_number"),
                item.get("number_of_hadith"),
            )
            for item in books
            if item.get("book_number")
        ]
        if not rows:
            return
        await self.database.executemany(
            """
            INSERT INTO hadith_books (
                collection_name, book_number, title,
                hadith_start_number, hadith_end_number, number_of_hadith, updated_at
            )
            VALUES ($1, $2, $3, $4, $5, $6, NOW())
            ON CONFLICT (collection_name, book_number)
            DO UPDATE SET
                title = EXCLUDED.title,
                hadith_start_number = EXCLUDED.hadith_start_number,
                hadith_end_number = EXCLUDED.hadith_end_number,
                number_of_hadith = EXCLUDED.number_of_hadith,
                updated_at = NOW()
            """,
            rows,
        )

    async def upsert_hadith_detail(self, hadith: dict[str, Any]) -> None:
        if not self.enabled:
            return
        collection_name = str(hadith.get("collection") or "").strip()
        hadith_number = str(hadith.get("hadith_number") or "").strip()
        if not collection_name or not hadith_number:
            return

        async with self.database._pool.acquire() as conn:
            async with conn.transaction():
                row = await conn.fetchrow(
                    """
                    INSERT INTO hadith_items (
                        collection_name, book_number, chapter_id, hadith_number,
                        chapter_title, updated_at
                    )
                    VALUES ($1, $2, $3, $4, $5, NOW())
                    ON CONFLICT (collection_name, hadith_number)
                    DO UPDATE SET
                        book_number = EXCLUDED.book_number,
                        chapter_id = EXCLUDED.chapter_id,
                        chapter_title = EXCLUDED.chapter_title,
                        updated_at = NOW()
                    RETURNING id
                    """,
                    collection_name,
                    str(hadith.get("book_number") or ""),
                    str(hadith.get("chapter_id") or ""),
                    hadith_number,
                    hadith.get("chapter_title"),
                )
                hadith_item_id = row["id"]

                await conn.execute(
                    "DELETE FROM hadith_localizations WHERE hadith_item_id = $1",
                    hadith_item_id,
                )
                await conn.execute(
                    "DELETE FROM hadith_grades WHERE hadith_item_id = $1",
                    hadith_item_id,
                )

                localization_rows = []
                translation_text = str(hadith.get("translation_text") or "").strip()
                arabic_text = str(hadith.get("arabic_text") or "").strip()
                if translation_text:
                    localization_rows.append(
                        (hadith_item_id, "en", hadith.get("chapter_title"), translation_text)
                    )
                if arabic_text:
                    localization_rows.append(
                        (hadith_item_id, "ar", hadith.get("chapter_title"), arabic_text)
                    )
                if localization_rows:
                    await conn.executemany(
                        """
                        INSERT INTO hadith_localizations (
                            hadith_item_id, language, chapter_title, body_text, updated_at
                        )
                        VALUES ($1, $2, $3, $4, NOW())
                        """,
                        localization_rows,
                    )

                grade_rows = []
                grades = hadith.get("grades") or {}
                for language, items in grades.items():
                    for item in items or []:
                        grade_rows.append(
                            (
                                hadith_item_id,
                                language,
                                item.get("grade"),
                                item.get("graded_by"),
                                json.dumps(item),
                            )
                        )
                if grade_rows:
                    await conn.executemany(
                        """
                        INSERT INTO hadith_grades (
                            hadith_item_id, language, grade, graded_by, raw_payload, updated_at
                        )
                        VALUES ($1, $2, $3, $4, $5::jsonb, NOW())
                        """,
                        grade_rows,
                    )

    async def upsert_dua_categories(self, categories: list[dict[str, Any]]) -> None:
        if not self.enabled or not categories:
            return
        rows = [
            (item.get("category"), item.get("dua_count") or 0)
            for item in categories
            if item.get("category")
        ]
        await self.database.executemany(
            """
            INSERT INTO dua_categories (category, dua_count, updated_at)
            VALUES ($1, $2, NOW())
            ON CONFLICT (category)
            DO UPDATE SET
                dua_count = EXCLUDED.dua_count,
                updated_at = NOW()
            """,
            rows,
        )

    async def upsert_dua_items(self, items: list[dict[str, Any]]) -> None:
        if not self.enabled or not items:
            return
        rows = [
            (
                item.get("dua_id"),
                item.get("category"),
                item.get("title"),
                item.get("arabic_text") or "",
                item.get("transliteration_text") or "",
                item.get("translation_text") or "",
                item.get("reference") or "",
                item.get("benefit") or "",
            )
            for item in items
            if item.get("dua_id") and item.get("category")
        ]
        await self.database.executemany(
            """
            INSERT INTO dua_items (
                dua_id, category, title, arabic_text, transliteration_text,
                translation_text, reference, benefit, updated_at
            )
            VALUES ($1, $2, $3, $4, $5, $6, $7, $8, NOW())
            ON CONFLICT (dua_id)
            DO UPDATE SET
                category = EXCLUDED.category,
                title = EXCLUDED.title,
                arabic_text = EXCLUDED.arabic_text,
                transliteration_text = EXCLUDED.transliteration_text,
                translation_text = EXCLUDED.translation_text,
                reference = EXCLUDED.reference,
                benefit = EXCLUDED.benefit,
                updated_at = NOW()
            """,
            rows,
        )

    async def get_canonical_summary(self) -> dict[str, Any]:
        if not self.enabled:
            return {
                "database_enabled": False,
                "counts": {},
                "generated_at": datetime.now(timezone.utc).isoformat(),
            }

        queries = {
            "quran_chapters": "SELECT COUNT(*) FROM quran_chapters",
            "quran_ayahs": "SELECT COUNT(*) FROM quran_ayahs",
            "quran_words": "SELECT COUNT(*) FROM quran_words",
            "quran_triplet_blocks": "SELECT COUNT(*) FROM quran_triplet_blocks",
            "hadith_collections": "SELECT COUNT(*) FROM hadith_collections",
            "hadith_books": "SELECT COUNT(*) FROM hadith_books",
            "hadith_items": "SELECT COUNT(*) FROM hadith_items",
            "dua_categories": "SELECT COUNT(*) FROM dua_categories",
            "dua_items": "SELECT COUNT(*) FROM dua_items",
            "source_sync_runs": "SELECT COUNT(*) FROM source_sync_runs",
        }
        counts: dict[str, int] = {}
        for name, query in queries.items():
            value = await self.database.fetchrow(query)
            count = value[0] if value else 0
            counts[name] = int(count or 0)
        return {
            "database_enabled": True,
            "counts": counts,
            "generated_at": datetime.now(timezone.utc).isoformat(),
        }
