"""Sheikh Wahda business logic — recommendations, typeahead, trending, feedback, share."""
from __future__ import annotations

import json
import logging
from datetime import date, datetime, timezone
from pathlib import Path
from typing import Any

from ..repositories.wahda_repository import WahdaRepository

logger = logging.getLogger(__name__)


class WahdaService:
    def __init__(self, repo: WahdaRepository) -> None:
        self._repo = repo

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
        valid = {"how", "what", "when", "who", "why", "where"}
        word = prefix.split()[0] if prefix else ""
        if word not in valid:
            return []
        category = f"typeahead_{word}"
        return await self._repo.get_questions_by_category(category, limit=3, random=True)

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
        self, tenant_id: str, user_id: str, thread_id: str,
        title: str | None = None,
    ) -> dict[str, str]:
        """Pull full conversation from LangGraph thread and store as snapshot."""
        import httpx
        lg_url = self._langgraph_url

        # Fetch thread state from LangGraph Platform
        async with httpx.AsyncClient(timeout=30) as client:
            resp = await client.get(f"{lg_url}/threads/{thread_id}/state")
            resp.raise_for_status()
            state = resp.json()

        # Extract messages from state
        raw_msgs = state.get("values", {}).get("messages", [])
        messages = []
        for m in raw_msgs:
            role = "user" if m.get("type") == "human" else "assistant"
            content = m.get("content", "")
            # Gemini returns list-of-parts
            if isinstance(content, list):
                content = " ".join(
                    p.get("text", "") for p in content
                    if isinstance(p, dict) and p.get("text")
                )
            if not content or len(str(content)) < 2:
                continue
            messages.append({"role": role, "content": str(content)})

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
            thread_id=thread_id,
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

    @property
    def _langgraph_url(self) -> str:
        import os
        return os.getenv("LANGGRAPH_URL", "http://imam-agent:8000")

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
