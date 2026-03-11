from __future__ import annotations

import json
from typing import Any

from ..db import Database
from ..domain.constants import SUNNAH_SOURCE_API


def _hadith_sort_key(value: str) -> tuple[int, str]:
    digits = "".join(ch for ch in value if ch.isdigit())
    if digits:
        return (int(digits), value)
    return (10**9, value)


class HadithRepository:
    def __init__(self, db: Database):
        self.db = db

    async def upsert_collections(self, collections: list[dict[str, Any]]) -> None:
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
        await self.db.executemany(
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

    async def upsert_books(self, collection_name: str, books: list[dict[str, Any]]) -> None:
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
        await self.db.executemany(
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

    async def upsert_detail(self, hadith: dict[str, Any]) -> None:
        async def _txn(connection) -> None:
            row = await connection.fetchrow(
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
                hadith.get("collection"),
                str(hadith.get("book_number") or ""),
                str(hadith.get("chapter_id") or ""),
                str(hadith.get("hadith_number") or ""),
                hadith.get("chapter_title"),
            )
            hadith_item_id = row["id"]
            await connection.execute(
                "DELETE FROM hadith_localizations WHERE hadith_item_id = $1",
                hadith_item_id,
            )
            await connection.execute(
                "DELETE FROM hadith_grades WHERE hadith_item_id = $1",
                hadith_item_id,
            )
            localization_rows = []
            if hadith.get("translation_text"):
                localization_rows.append(
                    (hadith_item_id, "en", hadith.get("chapter_title"), hadith["translation_text"])
                )
            if hadith.get("arabic_text"):
                localization_rows.append(
                    (hadith_item_id, "ar", hadith.get("chapter_title"), hadith["arabic_text"])
                )
            if localization_rows:
                await connection.executemany(
                    """
                    INSERT INTO hadith_localizations (
                        hadith_item_id, language, chapter_title, body_text, updated_at
                    )
                    VALUES ($1, $2, $3, $4, NOW())
                    """,
                    localization_rows,
                )
            grade_rows = []
            for language, items in (hadith.get("grades") or {}).items():
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
                await connection.executemany(
                    """
                    INSERT INTO hadith_grades (
                        hadith_item_id, language, grade, graded_by, raw_payload, updated_at
                    )
                    VALUES ($1, $2, $3, $4, $5::jsonb, NOW())
                    """,
                    grade_rows,
                )

        await self.db.transaction(_txn)

    async def get_collections(self) -> list[dict[str, Any]]:
        rows = await self.db.fetch(
            """
            SELECT name, title, short_intro, has_books, has_chapters, total_books, total_hadith
            FROM hadith_collections
            ORDER BY name
            """
        )
        return [dict(row) for row in rows]

    async def get_books(self, collection_name: str) -> tuple[dict[str, Any] | None, list[dict[str, Any]]]:
        collection = await self.db.fetchrow(
            """
            SELECT name, title, short_intro, has_books, has_chapters, total_books, total_hadith
            FROM hadith_collections
            WHERE name = $1
            """,
            collection_name,
        )
        books = await self.db.fetch(
            """
            SELECT book_number, title, hadith_start_number, hadith_end_number, number_of_hadith
            FROM hadith_books
            WHERE collection_name = $1
            ORDER BY book_number
            """,
            collection_name,
        )
        return (dict(collection) if collection else None, [dict(row) for row in books])

    async def get_book_items(
        self,
        collection_name: str,
        book_number: str,
        *,
        page: int,
        limit: int,
    ) -> dict[str, Any]:
        rows = await self.db.fetch(
            """
            SELECT hi.id, hi.collection_name, hi.book_number, hi.chapter_id, hi.hadith_number,
                   hi.chapter_title,
                   en.body_text AS en_body,
                   ar.body_text AS ar_body
            FROM hadith_items hi
            LEFT JOIN hadith_localizations en
                ON en.hadith_item_id = hi.id AND en.language = 'en'
            LEFT JOIN hadith_localizations ar
                ON ar.hadith_item_id = hi.id AND ar.language = 'ar'
            WHERE hi.collection_name = $1 AND hi.book_number = $2
            """,
            collection_name,
            book_number,
        )
        items = sorted(rows, key=lambda row: _hadith_sort_key(row["hadith_number"]))
        total = len(items)
        start = max((page - 1) * limit, 0)
        page_items = items[start : start + limit]
        return {
            "items": [
                {
                    "collection": row["collection_name"],
                    "book_number": row["book_number"],
                    "chapter_id": row["chapter_id"],
                    "hadith_number": row["hadith_number"],
                    "title": row["chapter_title"],
                    "preview_text": (row["en_body"] or "")[:280],
                    "arabic_preview_text": (row["ar_body"] or "")[:280],
                }
                for row in page_items
            ],
            "pagination": {
                "page": page,
                "limit": limit,
                "total_items": total,
                "total_pages": max((total + limit - 1) // limit, 1),
            },
        }

    async def get_detail(self, collection_name: str, hadith_number: str) -> dict[str, Any] | None:
        row = await self.db.fetchrow(
            """
            SELECT *
            FROM hadith_items
            WHERE collection_name = $1 AND hadith_number = $2
            """,
            collection_name,
            hadith_number,
        )
        if row is None:
            return None
        localizations = await self.db.fetch(
            """
            SELECT language, chapter_title, body_text
            FROM hadith_localizations
            WHERE hadith_item_id = $1
            """,
            row["id"],
        )
        grades = await self.db.fetch(
            """
            SELECT language, grade, graded_by, raw_payload
            FROM hadith_grades
            WHERE hadith_item_id = $1
            """,
            row["id"],
        )
        grades_by_language: dict[str, list[dict[str, Any]]] = {}
        for item in grades:
            grades_by_language.setdefault(item["language"], []).append(
                {
                    "grade": item["grade"],
                    "graded_by": item["graded_by"],
                    **(json.loads(item["raw_payload"]) if isinstance(item["raw_payload"], str) else (item["raw_payload"] or {})),
                }
            )
        text_by_language = {
            item["language"]: {
                "chapter_title": item["chapter_title"],
                "body_text": item["body_text"],
            }
            for item in localizations
        }
        return {
            "collection": row["collection_name"],
            "book_number": row["book_number"],
            "chapter_id": row["chapter_id"],
            "hadith_number": row["hadith_number"],
            "chapter_title": row["chapter_title"],
            "translation_text": (text_by_language.get("en") or {}).get("body_text", ""),
            "arabic_text": (text_by_language.get("ar") or {}).get("body_text", ""),
            "grades": grades_by_language,
            "source_api": SUNNAH_SOURCE_API,
        }
