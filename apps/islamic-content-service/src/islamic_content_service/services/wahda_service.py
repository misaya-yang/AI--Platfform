"""Sheikh Wahda business logic — recommendations, typeahead, trending, feedback, share, recommended questions."""
from __future__ import annotations

import json
import logging
from datetime import date, datetime, timedelta, timezone
from pathlib import Path
from typing import Any

from ..repositories.wahda_repository import WahdaRepository

logger = logging.getLogger(__name__)


class WahdaService:
    def __init__(self, repo: WahdaRepository, gemini_client=None) -> None:
        self._repo = repo
        self._gemini = gemini_client

    # ------------------------------------------------------------------
    # Recommendations (§3.1 + §3.2)
    # ------------------------------------------------------------------

    async def get_recommendations(self, today: date | None = None) -> dict[str, Any]:
        today = today or date.today()
        events = await self._repo.get_events_for_date(today)

        # Filter: skip daily/weekly/monthly if a higher-priority yearly event exists
        yearly = [e for e in events if e["recurrence"] == "yearly"]
        if yearly:
            best = max(yearly, key=lambda e: e["priority"])
            questions = json.loads(best["questions"]) if isinstance(best["questions"], str) else best["questions"]
            return {
                "type": "religious_event",
                "event_name": best["event_name"],
                "questions": questions[:5],
                "date": today.isoformat(),
            }

        # Weekly (Friday) or monthly (Ayyam al-Beed)
        weekly = [e for e in events if e["recurrence"] == "weekly"]
        if weekly and today.weekday() == 4:  # Friday
            best = weekly[0]
            questions = json.loads(best["questions"]) if isinstance(best["questions"], str) else best["questions"]
            return {
                "type": "religious_event",
                "event_name": best["event_name"],
                "questions": questions[:5],
                "date": today.isoformat(),
            }

        # Fallback: daily rotation from question pool
        questions = await self._repo.get_daily_questions(today, count=5)
        if not questions:
            questions = ["What are the 5 daily prayers?", "How to perform Salah properly?",
                         "How to make Wudu correctly?", "How to check prayer times?",
                         "How to make up missed prayers?"]
        return {
            "type": "daily",
            "event_name": None,
            "questions": questions,
            "date": today.isoformat(),
        }

    # ------------------------------------------------------------------
    # Typeahead (§3.3)
    # ------------------------------------------------------------------

    async def get_typeahead(self, query_prefix: str) -> list[str]:
        prefix = query_prefix.strip().lower()
        valid = {"how", "what", "when", "who", "why", "where", "is", "can", "does"}
        words = prefix.split()
        word = words[0] if words else ""
        if word not in valid:
            return []

        # Multi-word: fuzzy-match against question pool, prefix matches first
        if len(words) >= 2:
            results = await self._repo.search_questions(prefix, limit=3)
            if results:
                return results

        # Single word or no fuzzy matches: fallback to category random
        category = f"typeahead_{word}"
        return await self._repo.get_questions_by_category(category, limit=3, random=True)

    async def get_typeahead_pool(self) -> list[str]:
        """Return all typeahead questions for client-side filtering."""
        return await self._repo.get_all_typeahead_questions()

    # ------------------------------------------------------------------
    # Trending (§3.4)
    # ------------------------------------------------------------------

    async def get_trending(self, today: date | None = None) -> dict[str, Any]:
        today = today or date.today()
        queries = await self._repo.get_trending_queries(days=7, limit=30)

        if len(queries) >= 10:
            # Rotate: divide 30 into groups of 5, pick group by day
            group_idx = today.toordinal() % max(len(queries) // 5, 1)
            start = group_idx * 5
            selected = queries[start:start + 5]
            if len(selected) < 5:
                selected = queries[:5]
            return {
                "questions": selected,
                "period": f"{today.replace(day=max(today.day - 7, 1)).isoformat()} ~ {today.isoformat()}",
                "source": "trending",
            }

        # Fallback: mix typeahead + daily
        fallback = await self._repo.get_daily_questions(today, count=3)
        extra = await self._repo.get_questions_by_category("typeahead_how", limit=2, random=True)
        return {
            "questions": (fallback + extra)[:5],
            "period": f"{today.replace(day=max(today.day - 7, 1)).isoformat()} ~ {today.isoformat()}",
            "source": "fallback",
        }

    # ------------------------------------------------------------------
    # Feedback (§3.6)
    # ------------------------------------------------------------------

    async def submit_feedback(
        self, tenant_id: str, user_id: str, session_id: str,
        message_index: int | None, feedback_type: str,
        reason: str | None = None, comment: str | None = None,
    ) -> str:
        return await self._repo.insert_feedback(
            tenant_id, user_id, session_id, message_index,
            feedback_type, reason, comment,
        )

    # ------------------------------------------------------------------
    # Share — ChatGPT-style full conversation snapshot
    # ------------------------------------------------------------------

    async def create_share(
        self, tenant_id: str, user_id: str, session_id: str,
        title: str | None = None,
        pre_fetched_messages: list[dict] | None = None,
    ) -> dict[str, str]:
        """Store a full conversation snapshot.

        Messages can be:
        1. Pre-fetched by the frontend (passed in pre_fetched_messages)
        2. Pulled from Gateway session history API
        """
        import httpx, re

        if pre_fetched_messages:
            messages = self._clean_messages(pre_fetched_messages)
        else:
            # Pull from Gateway session history
            gw_url = self._gateway_url
            async with httpx.AsyncClient(timeout=30) as client:
                resp = await client.get(
                    f"{gw_url}/api/v1/assistant/sessions/{session_id}/history",
                    headers={"X-User-Id": user_id, "X-Tenant-Id": tenant_id},
                )
                resp.raise_for_status()
                raw = resp.json()

            raw_msgs = raw if isinstance(raw, list) else raw.get("messages", raw.get("data", []))
            messages = []
            for m in raw_msgs:
                role = m.get("role", "")
                content = m.get("content", "")
                if isinstance(content, list):
                    content = " ".join(
                        p.get("text", "") for p in content
                        if isinstance(p, dict) and p.get("text")
                    )
                if role in ("user", "assistant") and content and len(str(content)) > 2:
                    messages.append({"role": role, "content": str(content)})
            messages = self._clean_messages(messages)

        if not messages:
            raise ValueError("No messages found in thread")

        # Auto-generate title from first user message
        if not title:
            for m in messages:
                if m["role"] == "user":
                    title = m["content"][:80]
                    if len(m["content"]) > 80:
                        title += "..."
                    break
            title = title or "Shared Conversation"

        share_id = await self._repo.create_share(
            tenant_id=tenant_id,
            user_id=user_id,
            thread_id=session_id,
            title=title,
            messages=messages,
            message_count=len(messages),
        )
        return {
            "share_id": share_id,
            "share_url": f"/share/{share_id}",
        }

    async def get_share(self, share_id: str) -> dict[str, Any] | None:
        row = await self._repo.get_share(share_id)
        if not row:
            return None
        created = row.get("created_at")
        expires = row.get("expires_at")
        return {
            "title": row.get("title", "Shared Conversation"),
            "messages": row.get("messages", []),
            "message_count": row.get("message_count", 0),
            "agent_name": row.get("agent_name", "Sheikh Wahda"),
            "created_at": created.isoformat() if isinstance(created, datetime) else str(created),
            "expires_at": expires.isoformat() if isinstance(expires, datetime) else None,
        }

    @staticmethod
    def _clean_messages(raw: list[dict]) -> list[dict]:
        """Filter to only user + final assistant messages, strip tool/KB noise."""
        import re
        cleaned = []
        for m in raw:
            role = m.get("role", "")
            content = str(m.get("content", ""))
            if role not in ("user", "assistant"):
                continue
            if not content or len(content.strip()) < 3:
                continue
            # Skip raw tool results (KB search output with [REF-N] patterns)
            if role == "assistant" and re.match(r"^\s*(CONFIDENCE:|RELEVANT_SOURCES:|\[REF-\d)", content):
                continue
            cleaned.append({"role": role, "content": content})
        return cleaned

    @property
    def _gateway_url(self) -> str:
        import os
        return os.getenv("GATEWAY_URL", "http://gateway:8080")

    # ------------------------------------------------------------------
    # Recommended Questions — AI-generated personalized follow-ups
    # ------------------------------------------------------------------

    async def get_recommended_questions(
        self, tenant_id: str, user_id: str, target_date: date | None = None,
    ) -> dict[str, Any]:
        """Get today's personalized recommended questions for a user."""
        target_date = target_date or date.today()
        rows = await self._repo.get_recommended_questions(tenant_id, user_id, target_date)
        questions = [self._row_to_item(r) for r in rows]
        return {
            "questions": questions,
            "date": target_date.isoformat(),
            "total": len(questions),
        }

    _MAX_ACTIVE_QUESTIONS = 30  # H-3: rate limit cap per user

    async def generate_recommendations(
        self, tenant_id: str, user_id: str,
        session_ids: list[str] | None = None,
        count: int = 5, date_strategy: str = "spaced",
    ) -> dict[str, Any]:
        """Generate recommended questions from conversation history using Gemini."""
        if not self._gemini:
            raise RuntimeError("AI recommendation service not configured")

        # H-3: rate limit — reject if user already has too many active questions
        today = date.today()
        active_count = await self._repo.count_active_questions(tenant_id, user_id, today)
        if active_count >= self._MAX_ACTIVE_QUESTIONS:
            return {"generated": 0, "questions": []}

        # Fetch conversation history
        if session_ids:
            all_recent = await self._repo.get_recent_user_sessions(
                tenant_id, user_id, days=90, limit=20,
            )
            wanted = set(session_ids[:5])
            sessions = [s for s in all_recent if s["session_id"] in wanted]
        else:
            sessions = await self._repo.get_recent_user_sessions(
                tenant_id, user_id, days=7, limit=5,
            )

        if not sessions:
            return {"generated": 0, "questions": []}

        # Format conversation history for the LLM
        history_text = self._format_sessions_for_llm(sessions)
        if not history_text.strip():
            return {"generated": 0, "questions": []}

        # Call Gemini to generate questions
        raw_items = await self._gemini.generate_recommended_questions(history_text, count)
        if not raw_items:
            return {"generated": 0, "questions": []}

        # Assign date_trigger based on strategy
        today = date.today()
        self._assign_date_triggers(raw_items, date_strategy, today)

        # Build DB records — use first session_id as default source
        default_session = sessions[0]["session_id"] if sessions else None
        db_records = []
        for item in raw_items:
            db_records.append({
                "tenant_id": tenant_id,
                "user_id": user_id,
                "session_id": default_session,
                "date_trigger": item.get("date_trigger", today),
                "question_text": item.get("question_text", ""),
                "is_regen": False,
                "source_topic": item.get("source_topic"),
            })
        db_records = [r for r in db_records if r["question_text"]]

        if not db_records:
            return {"generated": 0, "questions": []}

        ids = await self._repo.insert_recommended_questions(db_records)

        # Build response
        questions = []
        for rec, qid in zip(db_records, ids):
            questions.append({
                "question_id": qid,
                "date_trigger": rec["date_trigger"].isoformat(),
                "question_text": rec["question_text"],
                "is_regen": False,
                "session_id": rec["session_id"],
                "source_topic": rec.get("source_topic"),
                "status": "active",
                "created_at": datetime.now(timezone.utc).isoformat(),
            })

        return {"generated": len(questions), "questions": questions}

    async def regenerate_question(
        self, tenant_id: str, user_id: str, question_id: str,
    ) -> dict[str, Any] | None:
        """Regenerate a single recommended question. Dismisses old, creates new with is_regen=True."""
        if not self._gemini:
            raise RuntimeError("AI recommendation service not configured")

        old = await self._repo.get_recommended_question(question_id)
        if not old or old["tenant_id"] != tenant_id or old["user_id"] != user_id:
            return None

        # M-6 fix: atomically dismiss only if still active (race-safe)
        dismissed = await self._repo.dismiss_if_active(question_id, tenant_id, user_id)
        if not dismissed:
            return None  # already dismissed by a concurrent request

        # Get history from the source session to regenerate
        sessions = []
        if old.get("session_id"):
            all_sessions = await self._repo.get_recent_user_sessions(
                tenant_id, user_id, days=90, limit=10,
            )
            sessions = [s for s in all_sessions if s["session_id"] == old["session_id"]]

        if not sessions:
            sessions = await self._repo.get_recent_user_sessions(
                tenant_id, user_id, days=7, limit=3,
            )

        if not sessions:
            return None

        history_text = self._format_sessions_for_llm(sessions)
        raw_items = await self._gemini.generate_recommended_questions(history_text, 1)
        if not raw_items:
            return None

        item = raw_items[0]
        db_record = {
            "tenant_id": tenant_id,
            "user_id": user_id,
            "session_id": old.get("session_id"),
            "date_trigger": old["date_trigger"],
            "question_text": item.get("question_text", ""),
            "is_regen": True,
            "source_topic": item.get("source_topic"),
        }
        if not db_record["question_text"]:
            return None

        ids = await self._repo.insert_recommended_questions([db_record])
        return {
            "question_id": ids[0],
            "date_trigger": db_record["date_trigger"].isoformat()
            if isinstance(db_record["date_trigger"], date)
            else str(db_record["date_trigger"]),
            "question_text": db_record["question_text"],
            "is_regen": True,
            "session_id": db_record["session_id"],
            "source_topic": db_record.get("source_topic"),
            "status": "active",
            "created_at": datetime.now(timezone.utc).isoformat(),
        }

    async def update_question_status(
        self, tenant_id: str, user_id: str, question_id: str, new_status: str,
    ) -> bool:
        return await self._repo.update_recommended_question_status(
            question_id, tenant_id, user_id, new_status,
        )

    async def get_recommendations_history(
        self, tenant_id: str, user_id: str, limit: int = 20, offset: int = 0,
    ) -> dict[str, Any]:
        rows, total = await self._repo.get_recommended_questions_history(
            tenant_id, user_id, limit, offset,
        )
        questions = [self._row_to_item(r) for r in rows]
        return {
            "questions": questions,
            "date": date.today().isoformat(),
            "total": total,
        }

    # --- helpers ---

    @staticmethod
    def _row_to_item(row: dict[str, Any]) -> dict[str, Any]:
        """Convert a DB row to a RecommendedQuestionItem-compatible dict."""
        dt = row.get("date_trigger")
        ca = row.get("created_at")
        return {
            "question_id": str(row["question_id"]),
            "date_trigger": dt.isoformat() if isinstance(dt, date) else str(dt),
            "question_text": row["question_text"],
            "is_regen": row.get("is_regen", False),
            "session_id": row.get("session_id"),
            "source_topic": row.get("source_topic"),
            "status": row.get("status", "active"),
            "created_at": ca.isoformat() if isinstance(ca, datetime) else str(ca) if ca else None,
        }

    _PROMPT_CHAR_BUDGET = 10_000  # M-5: cap total prompt chars to ~2500 tokens

    def _format_sessions_for_llm(self, sessions: list[dict[str, Any]]) -> str:
        """Format session histories into a compact text for the LLM prompt."""
        parts: list[str] = []
        total_chars = 0
        for s in sessions[:5]:
            if total_chars >= self._PROMPT_CHAR_BUDGET:
                break
            history = s.get("history", [])
            if isinstance(history, str):
                history = json.loads(history)
            messages = self._clean_messages(
                [{"role": m.get("role", ""), "content": m.get("content", "")} for m in history[-20:]]
            )
            if not messages:
                continue
            sid = s.get("session_id", "unknown")
            lines = [f"--- Session {sid} ---"]
            for m in messages:
                remaining = self._PROMPT_CHAR_BUDGET - total_chars
                if remaining <= 0:
                    break
                snippet = m["content"][:min(500, remaining)]
                line = f"{m['role'].upper()}: {snippet}"
                lines.append(line)
                total_chars += len(line)
            parts.append("\n".join(lines))
        return "\n\n".join(parts)

    @staticmethod
    def _assign_date_triggers(
        items: list[dict[str, Any]], strategy: str, base_date: date,
    ) -> None:
        """Assign date_trigger to each item in-place based on strategy."""
        urgency_offsets = {"high": 1, "medium": 3, "low": 7}
        for item in items:
            if strategy == "today":
                item["date_trigger"] = base_date
            else:
                urgency = item.get("urgency", "medium")
                offset_days = urgency_offsets.get(urgency, 3)
                item["date_trigger"] = base_date + timedelta(days=offset_days)

    # ------------------------------------------------------------------
    # Seed data import
    # ------------------------------------------------------------------

    async def seed_if_empty(self, data_dir: str | Path | None = None) -> None:
        """Import seed data from wahda_seed.json if tables are empty."""
        event_count = await self._repo.count_events()
        pool_count = await self._repo.count_pool()
        if event_count > 0 and pool_count > 0:
            logger.info("Wahda seed data already loaded (events=%d, pool=%d)", event_count, pool_count)
            return

        # Find seed file
        candidates = [
            Path(data_dir) / "wahda_seed.json" if data_dir else None,
            Path(__file__).resolve().parents[2] / "data" / "wahda_seed.json",
            Path("/app/data/wahda_seed.json"),
        ]
        seed_path = None
        for p in candidates:
            if p and p.exists():
                seed_path = p
                break
        if not seed_path:
            logger.warning("wahda_seed.json not found, skipping seed")
            return

        data = json.loads(seed_path.read_text(encoding="utf-8"))

        # Import events
        if event_count == 0:
            for event in data.get("religious_events", []):
                await self._repo.insert_event(event)
            logger.info("Seeded %d religious events", len(data.get("religious_events", [])))

        # Import question pool
        if pool_count == 0:
            pool = data.get("question_pool", {})
            total = 0
            for category, questions in pool.items():
                for q in questions:
                    await self._repo.insert_question(category, q)
                    total += 1
            logger.info("Seeded %d question pool entries", total)
