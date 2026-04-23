from __future__ import annotations

from typing import Any

from ..db import Database


class DuaRepository:
    def __init__(self, db: Database):
        self.db = db

    async def upsert_category(self, category: str, dua_count: int) -> None:
        await self.db.execute(
            """
            INSERT INTO dua_categories (category, dua_count, updated_at)
            VALUES ($1, $2, NOW())
            ON CONFLICT (category)
            DO UPDATE SET dua_count = EXCLUDED.dua_count, updated_at = NOW()
            """,
            category,
            dua_count,
        )

    async def upsert_item(self, item: dict[str, Any]) -> None:
        await self.db.execute(
            """
            INSERT INTO dua_items (
                dua_id, category, title, arabic_text, transliteration,
                english_meaning, urdu_meaning, source, reference,
                authenticity, occasion, data_source, verification_status, updated_at
            )
            VALUES ($1,$2,$3,$4,$5,$6,$7,$8,$9,$10,$11,$12,$13, NOW())
            ON CONFLICT (dua_id)
            DO UPDATE SET
                category = EXCLUDED.category,
                title = EXCLUDED.title,
                arabic_text = EXCLUDED.arabic_text,
                transliteration = EXCLUDED.transliteration,
                english_meaning = EXCLUDED.english_meaning,
                urdu_meaning = EXCLUDED.urdu_meaning,
                source = EXCLUDED.source,
                reference = EXCLUDED.reference,
                authenticity = EXCLUDED.authenticity,
                occasion = EXCLUDED.occasion,
                data_source = EXCLUDED.data_source,
                verification_status = EXCLUDED.verification_status,
                updated_at = NOW()
            """,
            item.get("dua_id", ""),
            item.get("category", ""),
            item.get("title", ""),
            item.get("arabic_text", ""),
            item.get("transliteration", ""),
            item.get("english_meaning", ""),
            item.get("urdu_meaning", ""),
            item.get("source", ""),
            item.get("reference", ""),
            item.get("authenticity", ""),
            item.get("occasion", ""),
            item.get("data_source", ""),
            item.get("verification_status", ""),
        )

    async def get_categories(self) -> list[dict[str, Any]]:
        rows = await self.db.fetch(
            "SELECT category, dua_count FROM dua_categories ORDER BY category"
        )
        return [dict(row) for row in rows]

    async def get_items_by_category(self, category: str) -> list[dict[str, Any]]:
        rows = await self.db.fetch(
            """
            SELECT dua_id, category, title, arabic_text, transliteration,
                   english_meaning, urdu_meaning, source, reference,
                   authenticity, occasion, data_source, verification_status
            FROM dua_items
            WHERE category = $1
            ORDER BY dua_id
            """,
            category,
        )
        return [dict(row) for row in rows]

    async def get_all_items(self) -> list[dict[str, Any]]:
        rows = await self.db.fetch(
            """
            SELECT dua_id, category, title, arabic_text, transliteration,
                   english_meaning, urdu_meaning, source, reference,
                   authenticity, occasion, data_source, verification_status
            FROM dua_items
            ORDER BY dua_id
            """
        )
        return [dict(row) for row in rows]

    async def get_detail(self, dua_id: str) -> dict[str, Any] | None:
        row = await self.db.fetchrow(
            """
            SELECT dua_id, category, title, arabic_text, transliteration,
                   english_meaning, urdu_meaning, source, reference,
                   authenticity, occasion, data_source, verification_status
            FROM dua_items
            WHERE dua_id = $1
            """,
            dua_id,
        )
        return dict(row) if row else None

    async def count(self) -> int:
        return int(await self.db.fetchval("SELECT COUNT(*) FROM dua_items") or 0)

    async def get_random(self) -> dict[str, Any] | None:
        row = await self.db.fetchrow(
            """
            SELECT dua_id, category, title, arabic_text, transliteration,
                   english_meaning, urdu_meaning, source, reference,
                   authenticity, occasion, data_source, verification_status
            FROM dua_items
            ORDER BY random() LIMIT 1
            """
        )
        return dict(row) if row else None

    async def search_items(
        self,
        query: str,
        *,
        limit: int,
        offset: int,
    ) -> tuple[list[dict[str, Any]], int]:
        """Case-insensitive search across title, arabic_text, transliteration,
        english_meaning, urdu_meaning. Returns (items, total)."""
        pattern = f"%{query}%"
        rows = await self.db.fetch(
            """
            SELECT dua_id, category, title, arabic_text, transliteration,
                   english_meaning, urdu_meaning, source, reference,
                   authenticity, occasion, data_source, verification_status
            FROM dua_items
            WHERE title ILIKE $1
               OR arabic_text ILIKE $1
               OR transliteration ILIKE $1
               OR english_meaning ILIKE $1
               OR urdu_meaning ILIKE $1
            ORDER BY dua_id
            LIMIT $2 OFFSET $3
            """,
            pattern,
            limit,
            offset,
        )
        total = await self.db.fetchval(
            """
            SELECT COUNT(*) FROM dua_items
            WHERE title ILIKE $1
               OR arabic_text ILIKE $1
               OR transliteration ILIKE $1
               OR english_meaning ILIKE $1
               OR urdu_meaning ILIKE $1
            """,
            pattern,
        )
        return [dict(row) for row in rows], int(total or 0)

    async def get_items_by_occasion(self, occasion: str) -> list[dict[str, Any]]:
        """Filter by occasion column (ILIKE — occasions can be phrases like
        'Morning', 'Before eating', 'Entering mosque')."""
        rows = await self.db.fetch(
            """
            SELECT dua_id, category, title, arabic_text, transliteration,
                   english_meaning, urdu_meaning, source, reference,
                   authenticity, occasion, data_source, verification_status
            FROM dua_items
            WHERE occasion ILIKE $1
            ORDER BY dua_id
            """,
            f"%{occasion}%",
        )
        return [dict(row) for row in rows]

    async def list_occasions(self) -> list[dict[str, Any]]:
        """Distinct occasions with counts (for building a filter chip UI)."""
        rows = await self.db.fetch(
            """
            SELECT occasion, COUNT(*) AS dua_count
            FROM dua_items
            WHERE occasion IS NOT NULL AND occasion <> ''
            GROUP BY occasion
            ORDER BY occasion
            """
        )
        return [{"occasion": row["occasion"], "dua_count": int(row["dua_count"])} for row in rows]
