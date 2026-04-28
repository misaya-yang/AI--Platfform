from __future__ import annotations

import json
from typing import Any

from ..db import Database
from ..domain.constants import HADITH_SOURCE_API


# Arabic body text from BOTH sources (sunnah.com + fawazahmed0 CDN) carries
# embedded Unicode bidi marks — RLM (U+200F) before ~every period and LRM
# (U+200E) — that the original publishers used as typesetting hints. They
# render as benign zero-width characters in proper Arabic-aware UIs but
# show up as visible ``[U+200F]`` markers in JSON inspectors (Apifox /
# Postman) and add ~300KB of meaningless bytes to our payloads. Modern
# Unicode bidi algorithms handle direction without these explicit hints.
#
# We strip them at both seams (sync-in + read-out) so:
#   * future inserts never carry them (sync_service / replace_collection)
#   * legacy rows are scrubbed at response build time (defense in depth)
#   * ZWJ (U+200D) / ZWNJ (U+200C) are LEFT ALONE — those are legitimate
#     Arabic joiner controls that change letter shaping (e.g. Persian).
# Strip:
#   * U+200F RLM, U+200E LRM (publisher-supplied bidi typesetting hints)
#   * U+202A..U+202E (legacy directional embeddings: LRE/RLE/PDF/LRO/RLO)
#   * U+2066..U+2069 (modern directional isolates: LRI/RLI/FSI/PDI)
#   * U+FFFD REPLACEMENT CHARACTER (upstream encoding-conversion losses
#     in fawazahmed0 — 59 hadith bodies have these)
# Preserve ZWJ (U+200D), ZWNJ (U+200C), and BOM (U+FEFF) — those are
# legitimate Arabic/Persian letter shapers (and BOM at non-start is
# rare but not corrupting).
_BIDI_NOISE_CHARS = (
    "‏", "‎",                                        # RLM, LRM
    "‪", "‫", "‬", "‭", "‮",          # legacy embeddings
    "⁦", "⁧", "⁨", "⁩",                    # modern isolates
    "�",                                                  # replacement char
)


def _normalize_arabic(text: str | None) -> str | None:
    """Strip publisher-supplied bidi typesetting hints. Safe for English too —
    English never legitimately needs RLM/LRM and at least one English
    chapter intro from sunnah.com had a stray U+200E. Name kept for
    backwards compat with existing call sites."""
    if not text:
        return text
    if not any(ch in text for ch in _BIDI_NOISE_CHARS):
        return text
    out = text
    for ch in _BIDI_NOISE_CHARS:
        out = out.replace(ch, "")
    return out


# Alias — clearer at the EN call sites so reviewers don't wonder why an
# Arabic-named helper is being applied to a translation_text.
_normalize_text = _normalize_arabic


def _hadith_sort_key(value: str) -> tuple[int, str]:
    digits = "".join(ch for ch in value if ch.isdigit())
    if digits:
        return (int(digits), value)
    return (10**9, value)


class HadithRepository:
    def __init__(self, db: Database):
        self.db = db

    async def replace_collection(
        self,
        collection: dict[str, Any],
        books: list[dict[str, Any]],
        hadiths: list[dict[str, Any]],
    ) -> None:
        collection_name = str(collection.get("name") or "")
        if not collection_name:
            raise ValueError("collection.name is required")

        async def _txn(connection) -> None:
            # Serialize concurrent syncs for the same collection
            await connection.execute(
                "SELECT pg_advisory_xact_lock(hashtext($1))", collection_name
            )
            await connection.execute(
                """
                INSERT INTO hadith_collections (
                    name, title, short_intro, has_books, has_chapters,
                    total_books, total_hadith, source_api, updated_at
                )
                VALUES ($1, $2, $3, $4, $5, $6, $7, $8, NOW())
                ON CONFLICT (name)
                DO UPDATE SET
                    title = EXCLUDED.title,
                    short_intro = EXCLUDED.short_intro,
                    has_books = EXCLUDED.has_books,
                    has_chapters = EXCLUDED.has_chapters,
                    total_books = EXCLUDED.total_books,
                    total_hadith = EXCLUDED.total_hadith,
                    source_api = EXCLUDED.source_api,
                    updated_at = NOW()
                """,
                collection_name,
                collection.get("title"),
                collection.get("short_intro"),
                collection.get("has_books"),
                collection.get("has_chapters"),
                collection.get("total_books"),
                collection.get("total_hadith"),
                collection.get("source_api") or HADITH_SOURCE_API,
            )
            await connection.execute(
                "DELETE FROM hadith_items WHERE collection_name = $1",
                collection_name,
            )
            await connection.execute(
                "DELETE FROM hadith_books WHERE collection_name = $1",
                collection_name,
            )

            book_rows = [
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
            if book_rows:
                await connection.executemany(
                    """
                    INSERT INTO hadith_books (
                        collection_name, book_number, title,
                        hadith_start_number, hadith_end_number, number_of_hadith, updated_at
                    )
                    VALUES ($1, $2, $3, $4, $5, $6, NOW())
                    """,
                    book_rows,
                )

            hadith_rows = [
                (
                    collection_name,
                    str(item.get("book_number") or ""),
                    str(item.get("chapter_id") or ""),
                    str(item.get("hadith_number") or ""),
                    item.get("chapter_title"),
                    item.get("source_api") or HADITH_SOURCE_API,
                )
                for item in hadiths
                if item.get("hadith_number")
            ]
            if hadith_rows:
                await connection.executemany(
                    """
                    INSERT INTO hadith_items (
                        collection_name, book_number, chapter_id, hadith_number,
                        chapter_title, source_api, updated_at
                    )
                    VALUES ($1, $2, $3, $4, $5, $6, NOW())
                    """,
                    hadith_rows,
                )

            if not hadith_rows:
                return

            item_rows = await connection.fetch(
                """
                SELECT id, hadith_number
                FROM hadith_items
                WHERE collection_name = $1
                """,
                collection_name,
            )
            item_ids = {str(row["hadith_number"]): row["id"] for row in item_rows}

            localization_rows: list[tuple[Any, ...]] = []
            grade_rows: list[tuple[Any, ...]] = []
            for item in hadiths:
                hadith_item_id = item_ids.get(str(item.get("hadith_number") or ""))
                if hadith_item_id is None:
                    continue
                chapter_title = item.get("chapter_title")
                if item.get("translation_text"):
                    localization_rows.append(
                        (hadith_item_id, "en", chapter_title, _normalize_text(item["translation_text"]))
                    )
                if item.get("arabic_text"):
                    localization_rows.append(
                        (hadith_item_id, "ar", chapter_title, _normalize_arabic(item["arabic_text"]))
                    )
                for language, grades in (item.get("grades") or {}).items():
                    for grade in grades or []:
                        grade_rows.append(
                            (
                                hadith_item_id,
                                language,
                                grade.get("grade"),
                                grade.get("graded_by"),
                                json.dumps(grade),
                            )
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
                    (hadith_item_id, "en", hadith.get("chapter_title"),
                     _normalize_text(hadith["translation_text"]))
                )
            if hadith.get("arabic_text"):
                localization_rows.append(
                    (hadith_item_id, "ar", hadith.get("chapter_title"),
                     _normalize_arabic(hadith["arabic_text"]))
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
        rows = await self.db.fetch(
            """
            SELECT book_number, title, hadith_start_number, hadith_end_number, number_of_hadith
            FROM hadith_books
            WHERE collection_name = $1
            """,
            collection_name,
        )
        books = sorted(
            (dict(row) for row in rows),
            key=lambda row: _hadith_sort_key(str(row["book_number"])),
        )
        return (dict(collection) if collection else None, books)

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
            SELECT hi.id, hi.collection_name, hi.book_number, hi.hadith_number,
                   hb.title AS book_title,
                   hc.chapter_order, hc.title_en AS ch_title_en,
                   hc.title_ar AS ch_title_ar,
                   en.body_text AS en_body,
                   ar.body_text AS ar_body
            FROM hadith_items hi
            LEFT JOIN hadith_books hb
                ON hb.collection_name = hi.collection_name AND hb.book_number = hi.book_number
            LEFT JOIN hadith_chapters hc
                ON hc.id = hi.chapter_ref_id
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
                    "section_number": row["book_number"],
                    "section_title": row["book_title"],
                    "chapter_id": (
                        str(row["chapter_order"])
                        if row["chapter_order"] is not None
                        else None
                    ),
                    "chapter_title": row["ch_title_en"] or row["ch_title_ar"],
                    "hadith_number": row["hadith_number"],
                    "title": row["ch_title_en"] or row["book_title"],
                    "preview_text": (row["en_body"] or "")[:280],
                    "arabic_preview_text": (_normalize_arabic(row["ar_body"]) or "")[:280],
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

    async def get_chapters(
        self, collection_name: str, book_number: str
    ) -> list[dict[str, Any]]:
        rows = await self.db.fetch(
            """
            SELECT chapter_order, chapter_id_raw, title_en, title_ar,
                   intro_en, intro_ar, hadith_count
            FROM hadith_chapters
            WHERE collection_name = $1 AND book_number = $2
            ORDER BY chapter_order
            """,
            collection_name,
            book_number,
        )
        return [
            {
                "chapter_id": str(row["chapter_order"]),
                "chapter_number": row["chapter_order"],
                "chapter_id_raw": row["chapter_id_raw"],
                "chapter_title": (
                    _normalize_text(row["title_en"])
                    or _normalize_text(row["title_ar"])
                    or ""
                ),
                "title_en": _normalize_text(row["title_en"]),
                "title_ar": _normalize_text(row["title_ar"]),
                "intro_en": _normalize_text(row["intro_en"]),
                "intro_ar": _normalize_text(row["intro_ar"]),
                "hadith_count": row["hadith_count"] or 0,
            }
            for row in rows
        ]

    async def get_collection(self, collection_name: str) -> dict[str, Any] | None:
        row = await self.db.fetchrow(
            """
            SELECT name, title, short_intro, has_books, has_chapters,
                   total_books, total_hadith
            FROM hadith_collections
            WHERE name = $1
            """,
            collection_name,
        )
        return dict(row) if row else None

    async def get_random_hadith(
        self, *, collection_name: str | None = None
    ) -> tuple[str, str] | None:
        """Pick a random (collection, hadith_number) tuple. Caller then fetches
        the full detail via get_detail() to reuse its localization/grade joins."""
        if collection_name:
            row = await self.db.fetchrow(
                """
                SELECT collection_name, hadith_number
                FROM hadith_items
                WHERE collection_name = $1
                ORDER BY random() LIMIT 1
                """,
                collection_name,
            )
        else:
            row = await self.db.fetchrow(
                """
                SELECT collection_name, hadith_number
                FROM hadith_items
                ORDER BY random() LIMIT 1
                """
            )
        if row is None:
            return None
        return row["collection_name"], row["hadith_number"]

    async def get_neighbors(
        self,
        collection_name: str,
        hadith_number: str,
    ) -> dict[str, str | None]:
        """Return previous/next hadith_number using natural numeric sort key."""
        rows = await self.db.fetch(
            """
            SELECT hadith_number
            FROM hadith_items
            WHERE collection_name = $1
            """,
            collection_name,
        )
        ordered = sorted((r["hadith_number"] for r in rows), key=_hadith_sort_key)
        if hadith_number not in ordered:
            return {"previous": None, "next": None}
        idx = ordered.index(hadith_number)
        return {
            "previous": ordered[idx - 1] if idx > 0 else None,
            "next": ordered[idx + 1] if idx < len(ordered) - 1 else None,
        }

    async def search_hadiths(
        self,
        query: str,
        *,
        language: str,
        collection_name: str | None,
        limit: int,
        offset: int,
    ) -> tuple[list[dict[str, Any]], int]:
        """Case-insensitive full-text search across ``hadith_localizations.body_text``.

        language filters which column to search (en/ar). collection_name optionally
        scopes to one collection. Returns (items, total).
        """
        pattern = f"%{query}%"
        args: list[Any] = [pattern, language]
        scope_sql = ""
        if collection_name:
            args.append(collection_name)
            scope_sql = f"AND hi.collection_name = ${len(args)}"
        args.extend([limit, offset])

        rows = await self.db.fetch(
            f"""
            SELECT hi.id, hi.collection_name, hi.book_number, hi.hadith_number,
                   hi.chapter_title AS fallback_chapter_title,
                   hb.title AS book_title,
                   hc.title_en AS ch_title_en,
                   hc.title_ar AS ch_title_ar,
                   hl.body_text AS matched_body
            FROM hadith_localizations hl
            JOIN hadith_items hi ON hi.id = hl.hadith_item_id
            LEFT JOIN hadith_books hb
                ON hb.collection_name = hi.collection_name AND hb.book_number = hi.book_number
            LEFT JOIN hadith_chapters hc ON hc.id = hi.chapter_ref_id
            WHERE hl.body_text ILIKE $1
              AND hl.language = $2
              {scope_sql}
            ORDER BY hi.collection_name, hi.id
            LIMIT ${len(args) - 1} OFFSET ${len(args)}
            """,
            *args,
        )

        # Count query reuses the same filters (drop limit/offset at end)
        count_args: list[Any] = [pattern, language]
        count_scope_sql = ""
        if collection_name:
            count_args.append(collection_name)
            count_scope_sql = f"AND hi.collection_name = ${len(count_args)}"
        total = await self.db.fetchval(
            f"""
            SELECT COUNT(*)
            FROM hadith_localizations hl
            JOIN hadith_items hi ON hi.id = hl.hadith_item_id
            WHERE hl.body_text ILIKE $1
              AND hl.language = $2
              {count_scope_sql}
            """,
            *count_args,
        )

        items = [
            {
                "collection": row["collection_name"],
                "book_number": row["book_number"],
                "book_title": row["book_title"],
                "chapter_title": (
                    row["ch_title_en"] if language == "en" else row["ch_title_ar"]
                ) or row["fallback_chapter_title"],
                "hadith_number": row["hadith_number"],
                "language": language,
                "preview_text": (
                    (_normalize_arabic(row["matched_body"]) if language == "ar"
                     else row["matched_body"]) or ""
                )[:400],
            }
            for row in rows
        ]
        return items, int(total or 0)

    async def get_detail(self, collection_name: str, hadith_number: str) -> dict[str, Any] | None:
        row = await self.db.fetchrow(
            """
            SELECT hi.*, hb.title AS book_title,
                   hc.chapter_order, hc.title_en AS ch_title_en,
                   hc.title_ar AS ch_title_ar
            FROM hadith_items hi
            LEFT JOIN hadith_books hb
                ON hb.collection_name = hi.collection_name AND hb.book_number = hi.book_number
            LEFT JOIN hadith_chapters hc
                ON hc.id = hi.chapter_ref_id
            WHERE hi.collection_name = $1 AND hi.hadith_number = $2
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
            "section_number": row["book_number"],
            "section_title": row["book_title"],
            "chapter_id": (
                str(row["chapter_order"])
                if row["chapter_order"] is not None
                else None
            ),
            "hadith_number": row["hadith_number"],
            "chapter_title": row["ch_title_en"] or row["ch_title_ar"] or row["book_title"],
            # Defense-in-depth: scrub bidi noise on the way out even though
            # we now normalize on insert. Legacy rows pre-backfill, plus any
            # future re-sync from a not-yet-normalized seam, get cleaned.
            # English body had 0 cases in prod survey but a single chapter
            # intro_en carried U+200E — scrub both languages defensively.
            "translation_text": _normalize_text(
                (text_by_language.get("en") or {}).get("body_text", "")
            ) or "",
            "arabic_text": _normalize_text(
                (text_by_language.get("ar") or {}).get("body_text", "")
            ) or "",
            "grades": grades_by_language,
            "source_api": HADITH_SOURCE_API,
        }
