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

    async def search_questions(
        self, full_input: str, category_prefix: str = "typeahead_", limit: int = 3,
    ) -> list[str]:
        """Fuzzy-match questions by substring, prefix matches ranked first.

        Used by /typeahead API (Flutter app). Web uses client-side pool filtering.
        """
        # Escape LIKE wildcards to prevent injection
        escaped = full_input.lower().replace("%", r"\%").replace("_", r"\_")
        sql = """
        SELECT question,
               CASE WHEN LOWER(question) LIKE $2 || '%' ESCAPE '\\' THEN 0 ELSE 1 END AS rank
        FROM islamic_content.question_pool
        WHERE category LIKE $1 AND is_active = true
          AND LOWER(question) LIKE '%' || $3 || '%' ESCAPE '\\'
        ORDER BY rank, RANDOM() LIMIT $4
        """
        rows = await self._db.fetch(sql, f"{category_prefix}%", escaped, escaped, limit)
        return [r["question"] for r in rows]

    async def get_all_typeahead_questions(self) -> list[str]:
        """Get all typeahead questions for client-side pool (PRD-locked to 6 categories × 5)."""
        sql = """
        SELECT DISTINCT question FROM islamic_content.question_pool
        WHERE is_active = true
          AND category LIKE 'typeahead_%'
        ORDER BY question
        """
        rows = await self._db.fetch(sql)
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
        self, tenant_id: str, user_id: str, thread_id: str,
        title: str, messages: list[dict], message_count: int,
    ) -> str:
        import json
        from datetime import timedelta
        share_id = secrets.token_urlsafe(8)[:12]
        expires = datetime.now(timezone.utc) + timedelta(days=30)
        await self._db.execute("""
            INSERT INTO islamic_content.shared_messages
            (share_id, tenant_id, user_id, session_id, title, messages, message_count, expires_at)
            VALUES ($1, $2, $3, $4, $5, $6::jsonb, $7, $8)
        """, share_id, tenant_id, user_id, thread_id, title,
            json.dumps(messages), message_count, expires)
        return share_id

    async def get_share(self, share_id: str) -> dict[str, Any] | None:
        row = await self._db.fetchrow("""
            SELECT title, messages, message_count, agent_name, created_at, expires_at
            FROM islamic_content.shared_messages
            WHERE share_id = $1 AND (expires_at IS NULL OR expires_at > NOW())
        """, share_id)
        if not row:
            return None
        d = dict(row)
        # messages is JSONB, asyncpg returns it as a Python object
        if isinstance(d.get("messages"), str):
            import json
            d["messages"] = json.loads(d["messages"])
        return d

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

    # ------------------------------------------------------------------
    # Recommended Questions (AI-generated, personalized)
    # ------------------------------------------------------------------

    async def get_recommended_questions(
        self, tenant_id: str, user_id: str, target_date: date,
        status: str = "active",
    ) -> list[dict[str, Any]]:
        """Get recommended questions for a user on a specific date."""
        sql = """
        SELECT question_id::text, tenant_id, user_id, session_id,
               date_trigger, question_text, is_regen,
               source_message_index, source_topic, status, created_at
        FROM islamic_content.recommended_questions
        WHERE tenant_id = $1 AND user_id = $2
          AND date_trigger = $3 AND status = $4
        ORDER BY created_at DESC
        """
        rows = await self._db.fetch(sql, tenant_id, user_id, target_date, status)
        return [dict(r) for r in rows]

    async def get_recommended_questions_history(
        self, tenant_id: str, user_id: str, limit: int = 20, offset: int = 0,
    ) -> tuple[list[dict[str, Any]], int]:
        """Get paginated history of all recommended questions for a user."""
        count_sql = """
        SELECT COUNT(*) FROM islamic_content.recommended_questions
        WHERE tenant_id = $1 AND user_id = $2
        """
        total = await self._db.fetchval(count_sql, tenant_id, user_id)

        sql = """
        SELECT question_id::text, tenant_id, user_id, session_id,
               date_trigger, question_text, is_regen,
               source_message_index, source_topic, status, created_at
        FROM islamic_content.recommended_questions
        WHERE tenant_id = $1 AND user_id = $2
        ORDER BY created_at DESC
        LIMIT $3 OFFSET $4
        """
        rows = await self._db.fetch(sql, tenant_id, user_id, limit, offset)
        return [dict(r) for r in rows], total or 0

    async def get_recommended_question(
        self, question_id: str,
    ) -> dict[str, Any] | None:
        """Get a single recommended question by ID."""
        sql = """
        SELECT question_id::text, tenant_id, user_id, session_id,
               date_trigger, question_text, is_regen,
               source_message_index, source_topic, status, created_at
        FROM islamic_content.recommended_questions
        WHERE question_id = $1::uuid
        """
        row = await self._db.fetchrow(sql, question_id)
        return dict(row) if row else None

    async def insert_recommended_questions(
        self, questions: list[dict[str, Any]],
    ) -> list[str]:
        """Bulk-insert recommended questions in a transaction. Returns UUIDs."""
        # H-1 fix: wrap in transaction to avoid partial writes
        pool = self._db._pool
        ids: list[str] = []
        async with pool.acquire() as conn:
            async with conn.transaction():
                for q in questions:
                    row = await conn.fetchrow("""
                        INSERT INTO islamic_content.recommended_questions
                        (tenant_id, user_id, session_id, date_trigger, question_text,
                         is_regen, source_message_index, source_topic)
                        VALUES ($1, $2, $3, $4, $5, $6, $7, $8)
                        RETURNING question_id::text
                    """,
                        q["tenant_id"], q["user_id"], q.get("session_id"),
                        q["date_trigger"], q["question_text"],
                        q.get("is_regen", False), q.get("source_message_index"),
                        q.get("source_topic"),
                    )
                    ids.append(row["question_id"])
        return ids

    async def update_recommended_question_status(
        self, question_id: str, tenant_id: str, user_id: str, new_status: str,
    ) -> bool:
        """Update status with ownership check. Returns True if updated."""
        result = await self._db.execute("""
            UPDATE islamic_content.recommended_questions
            SET status = $1, updated_at = NOW()
            WHERE question_id = $2::uuid
              AND tenant_id = $3 AND user_id = $4
        """, new_status, question_id, tenant_id, user_id)
        # M-1 fix: exact match instead of fragile endswith
        return result == "UPDATE 1"

    async def dismiss_if_active(
        self, question_id: str, tenant_id: str, user_id: str,
    ) -> bool:
        """Atomically dismiss only if still active (M-6: race-safe)."""
        result = await self._db.execute("""
            UPDATE islamic_content.recommended_questions
            SET status = 'dismissed', updated_at = NOW()
            WHERE question_id = $1::uuid
              AND tenant_id = $2 AND user_id = $3
              AND status = 'active'
        """, question_id, tenant_id, user_id)
        return result == "UPDATE 1"

    async def count_active_questions(
        self, tenant_id: str, user_id: str, target_date: date,
    ) -> int:
        """Count active questions for a user on a date (H-3: rate limiting)."""
        return await self._db.fetchval("""
            SELECT COUNT(*) FROM islamic_content.recommended_questions
            WHERE tenant_id = $1 AND user_id = $2
              AND date_trigger >= $3 AND status = 'active'
        """, tenant_id, user_id, target_date) or 0

    async def get_recent_user_sessions(
        self, tenant_id: str, user_id: str, days: int = 7, limit: int = 10,
    ) -> list[dict[str, Any]]:
        """Get recent sessions from public.sessions (cross-schema).

        H-4 fix: only fetch last 20 messages per session via jsonb_agg slice
        to avoid fetching megabytes of full history.
        """
        try:
            sql = """
            SELECT session_id,
                   history #> '{}' AS history,
                   metadata, updated_at
            FROM (
                SELECT session_id,
                       CASE
                         WHEN jsonb_array_length(history) > 20
                         THEN (
                           SELECT jsonb_agg(elem)
                           FROM jsonb_array_elements(history) WITH ORDINALITY AS t(elem, idx)
                           WHERE t.idx > jsonb_array_length(history) - 20
                         )
                         ELSE history
                       END AS history,
                       metadata, updated_at
                FROM public.sessions
                WHERE tenant_id = $1 AND user_id = $2
                  AND status = 'active'
                  AND updated_at > NOW() - ($3 || ' days')::interval
                  AND jsonb_array_length(history) > 0
                ORDER BY updated_at DESC
                LIMIT $4
            ) sub
            """
            rows = await self._db.fetch(sql, tenant_id, user_id, str(days), limit)
            return [dict(r) for r in rows]
        except Exception as e:
            logger.error("Cross-schema session query failed: %s", e)
            return []
