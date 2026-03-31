"""Database operations for Sheikh Wahda features."""
from __future__ import annotations

import logging
import secrets
from datetime import date, datetime, timezone
from typing import Any

logger = logging.getLogger(__name__)


class WahdaRepository:
    """CRUD for religious_events, question_pool, message_feedback, shared_messages."""

    def __init__(self, db) -> None:
        self._db = db

    # ------------------------------------------------------------------
    # Religious events
    # ------------------------------------------------------------------

    async def get_events_for_date(self, today: date) -> list[dict[str, Any]]:
        """Get active religious events matching today (yearly, weekly, monthly)."""
        sql = """
        SELECT event_id, event_name, islamic_date, questions, description, priority, recurrence,
               weekday, lunar_days, gregorian_start, gregorian_end
        FROM islamic_content.religious_events
        WHERE is_active = true
          AND (
            -- Yearly: date falls within start-end range
            (recurrence = 'yearly' AND gregorian_start <= $1
             AND (gregorian_end >= $1 OR gregorian_end IS NULL AND gregorian_start = $1))
            -- Weekly: matching weekday (0=Mon..6=Sun)
            OR (recurrence = 'weekly' AND weekday = EXTRACT(DOW FROM $1::date)::int)
            -- Monthly: handled in app layer (lunar_days need Hijri conversion)
            OR recurrence = 'monthly'
            -- Daily: always matches
            OR recurrence = 'daily'
          )
        ORDER BY priority DESC, event_id
        """
        rows = await self._db.fetch(sql, today)
        return [dict(r) for r in rows]

    async def count_events(self) -> int:
        return await self._db.fetchval(
            "SELECT COUNT(*) FROM islamic_content.religious_events"
        )

    async def insert_event(self, event: dict[str, Any]) -> None:
        from datetime import date as Date
        def _parse_date(v):
            if v is None: return None
            if isinstance(v, Date): return v
            return Date.fromisoformat(str(v))

        await self._db.execute("""
            INSERT INTO islamic_content.religious_events
            (event_name, islamic_date, gregorian_start, gregorian_end, recurrence,
             weekday, lunar_days, questions, description, priority)
            VALUES ($1, $2, $3, $4, $5, $6, $7, $8::jsonb, $9, $10)
        """,
            event["event_name"], event.get("islamic_date"),
            _parse_date(event["gregorian_start"]), _parse_date(event.get("gregorian_end")),
            event.get("recurrence", "yearly"),
            event.get("weekday"), event.get("lunar_days"),
            __import__("json").dumps(event["questions"]),
            event.get("description"), event.get("priority", 0),
        )

    # ------------------------------------------------------------------
    # Question pool
    # ------------------------------------------------------------------

    async def get_questions_by_category(
        self, category: str, limit: int = 5, random: bool = False,
    ) -> list[str]:
        if random:
            sql = """
            SELECT question FROM islamic_content.question_pool
            WHERE category = $1 AND is_active = true
            ORDER BY RANDOM() LIMIT $2
            """
        else:
            sql = """
            SELECT question FROM islamic_content.question_pool
            WHERE category = $1 AND is_active = true
            ORDER BY pool_id LIMIT $2
            """
        rows = await self._db.fetch(sql, category, limit)
        return [r["question"] for r in rows]

    async def get_daily_questions(self, today: date, count: int = 5) -> list[str]:
        """Get deterministic daily rotation of questions (same set per day)."""
        seed = today.toordinal()
        sql = """
        SELECT question FROM islamic_content.question_pool
        WHERE category = 'daily' AND is_active = true
        ORDER BY hashtext(question || $1::text) LIMIT $2
        """
        rows = await self._db.fetch(sql, str(seed), count)
        return [r["question"] for r in rows]

    async def count_pool(self) -> int:
        return await self._db.fetchval(
            "SELECT COUNT(*) FROM islamic_content.question_pool"
        )

    async def insert_question(self, category: str, question: str, language: str = "en") -> None:
        await self._db.execute("""
            INSERT INTO islamic_content.question_pool (category, question, language)
            VALUES ($1, $2, $3) ON CONFLICT DO NOTHING
        """, category, question, language)

    # ------------------------------------------------------------------
    # Feedback
    # ------------------------------------------------------------------

    async def insert_feedback(
        self, tenant_id: str, user_id: str, session_id: str,
        message_index: int | None, feedback_type: str,
        reason: str | None = None, comment: str | None = None,
    ) -> str:
        row = await self._db.fetchrow("""
            INSERT INTO islamic_content.message_feedback
            (tenant_id, user_id, session_id, message_index, feedback_type, reason, comment)
            VALUES ($1, $2, $3, $4, $5, $6, $7)
            RETURNING feedback_id::text
        """, tenant_id, user_id, session_id, message_index, feedback_type, reason, comment)
        return row["feedback_id"]

    # ------------------------------------------------------------------
    # Shared messages
    # ------------------------------------------------------------------

    async def create_share(
        self, tenant_id: str, user_id: str, session_id: str,
        message_index: int, question: str, answer: str,
    ) -> str:
        share_id = secrets.token_urlsafe(8)[:12]
        expires = datetime.now(timezone.utc).replace(
            month=datetime.now(timezone.utc).month + 1 if datetime.now(timezone.utc).month < 12 else 1,
        )
        await self._db.execute("""
            INSERT INTO islamic_content.shared_messages
            (share_id, tenant_id, user_id, session_id, message_index, question, answer, expires_at)
            VALUES ($1, $2, $3, $4, $5, $6, $7, $8)
        """, share_id, tenant_id, user_id, session_id, message_index, question, answer, expires)
        return share_id

    async def get_share(self, share_id: str) -> dict[str, Any] | None:
        row = await self._db.fetchrow("""
            SELECT question, answer, agent_name, created_at
            FROM islamic_content.shared_messages
            WHERE share_id = $1 AND (expires_at IS NULL OR expires_at > NOW())
        """, share_id)
        return dict(row) if row else None

    # ------------------------------------------------------------------
    # Trending (cross-schema query on public.usage_records)
    # ------------------------------------------------------------------

    async def get_trending_queries(self, days: int = 7, limit: int = 30) -> list[str]:
        """Get most frequent user queries from Gateway usage_records (past N days)."""
        try:
            sql = """
            SELECT metadata->>'user_message' AS msg, COUNT(*) AS cnt
            FROM public.usage_records
            WHERE created_at > NOW() - ($1 || ' days')::interval
              AND service_id = 'local-2024-agent'
              AND metadata->>'user_message' IS NOT NULL
              AND LENGTH(metadata->>'user_message') > 10
            GROUP BY metadata->>'user_message'
            ORDER BY cnt DESC
            LIMIT $2
            """
            rows = await self._db.fetch(sql, str(days), limit)
            return [r["msg"] for r in rows if r["msg"]]
        except Exception as e:
            logger.warning("Trending query failed (usage_records may not exist): %s", e)
            return []
