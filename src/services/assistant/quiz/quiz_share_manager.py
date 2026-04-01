"""
Quiz Share Manager — Create, validate, and revoke shareable quiz links.
"""

from __future__ import annotations

import html
import json
import logging
import random
import re
import secrets
import uuid
from datetime import datetime, timedelta, timezone

from ....persistence.database import DatabaseStorage

logger = logging.getLogger(__name__)

SHARE_CODE_LENGTH = 12
MAX_DISPLAY_NAME_LEN = 100
DISPLAY_NAME_RE = re.compile(r"^[\w\s\u4e00-\u9fff\u0600-\u06ff.\-@]+$", re.UNICODE)


def _sanitize_display_name(name: str | None) -> str | None:
    """Sanitize display_name: strip, length-limit, reject suspicious chars."""
    if not name:
        return None
    name = html.escape(name.strip())[:MAX_DISPLAY_NAME_LEN]
    if not name or not DISPLAY_NAME_RE.match(html.unescape(name)):
        return name  # keep escaped version even if regex fails
    return name


def _shuffle_options(questions: list[dict]) -> list[dict]:
    """Shuffle option display order per question. Labels stay attached to their text."""
    shuffled = []
    for q in questions:
        opts = list(q.get("options", []))
        random.shuffle(opts)
        shuffled.append({**q, "options": opts})
    return shuffled


class QuizShareManager:
    """Manages shareable quiz links."""

    def __init__(self, db: DatabaseStorage) -> None:
        self.db = db

    async def create_share(
        self,
        quiz_id: str,
        user_id: str,
        tenant_id: str,
        expires_hours: int | None = None,
        max_attempts: int | None = None,
        require_name: bool = True,
        time_limit_minutes: int | None = None,
    ) -> dict:
        """Create a shareable link for a quiz."""
        # Verify quiz exists and belongs to user
        quiz_row = await self.db.fetchrow(
            "SELECT id, title FROM quizzes WHERE id = $1 AND tenant_id = $2 AND created_by = $3",
            uuid.UUID(quiz_id),
            tenant_id,
            user_id,
        )
        if not quiz_row:
            raise ValueError("Quiz not found or not authorized")

        share_code = secrets.token_urlsafe(SHARE_CODE_LENGTH)[:SHARE_CODE_LENGTH]
        share_id = str(uuid.uuid4())
        now = datetime.now(timezone.utc)
        expires_at = now + timedelta(hours=expires_hours) if expires_hours else None

        await self.db.execute(
            """
            INSERT INTO quiz_shares (id, quiz_id, share_code, created_by,
                                     is_active, max_attempts, expires_at,
                                     require_name, time_limit_minutes, created_at)
            VALUES ($1, $2, $3, $4, $5, $6, $7, $8, $9, $10)
            """,
            uuid.UUID(share_id),
            uuid.UUID(quiz_id),
            share_code,
            user_id,
            True,
            max_attempts,
            expires_at,
            require_name,
            time_limit_minutes,
            now,
        )

        logger.info(f"Created share {share_code} for quiz {quiz_id}")

        return {
            "share_id": share_id,
            "share_code": share_code,
            "quiz_id": quiz_id,
            "quiz_title": quiz_row["title"],
            "expires_at": expires_at.isoformat() if expires_at else None,
            "require_name": require_name,
            "max_attempts": max_attempts,
            "time_limit_minutes": time_limit_minutes,
        }

    async def get_share_by_code(self, share_code: str) -> dict | None:
        """Look up a share by its public code. Returns None if not found/expired/inactive."""
        row = await self.db.fetchrow(
            """
            SELECT s.*, q.title AS quiz_title, q.description AS quiz_description,
                   q.question_count, q.difficulty
            FROM quiz_shares s
            JOIN quizzes q ON q.id = s.quiz_id
            WHERE s.share_code = $1 AND s.is_active = TRUE
            """,
            share_code,
        )
        if not row:
            return None

        # Check expiry
        if row["expires_at"] and row["expires_at"] < datetime.now(timezone.utc):
            return None

        # Check max_attempts
        if row["max_attempts"] is not None:
            attempt_count = await self.db.fetchrow(
                "SELECT count(*) AS cnt FROM quiz_attempts WHERE share_id = $1",
                row["id"],
            )
            if attempt_count and attempt_count["cnt"] >= row["max_attempts"]:
                return None

        return {
            "share_id": str(row["id"]),
            "quiz_id": str(row["quiz_id"]),
            "share_code": row["share_code"],
            "quiz_title": row["quiz_title"],
            "quiz_description": row["quiz_description"],
            "question_count": row["question_count"],
            "difficulty": row["difficulty"],
            "require_name": row["require_name"],
            "expires_at": row["expires_at"].isoformat() if row["expires_at"] else None,
            "time_limit_minutes": row.get("time_limit_minutes"),
            "is_exam": bool(row.get("is_exam")),
        }

    async def get_public_quiz(self, share_code: str) -> dict | None:
        """Get quiz questions for public taking (no correct answers)."""
        share = await self.get_share_by_code(share_code)
        if not share:
            return None

        q_rows = await self.db.fetch(
            "SELECT id, question_num, question_type, question_text, options FROM quiz_questions WHERE quiz_id = $1 ORDER BY question_num",
            uuid.UUID(share["quiz_id"]),
        )

        questions = []
        for qr in q_rows:
            options = qr["options"]
            if isinstance(options, str):
                options = json.loads(options)
            questions.append({
                "id": str(qr["id"]),
                "question_num": qr["question_num"],
                "question_type": qr["question_type"],
                "question_text": qr["question_text"],
                "options": options,
            })

        # P0: Shuffle option display order so each viewer sees different arrangement
        shuffled_questions = _shuffle_options(questions)

        return {
            "quiz_id": share["quiz_id"],
            "share_code": share["share_code"],
            "title": share["quiz_title"],
            "description": share["quiz_description"],
            "question_count": share["question_count"],
            "difficulty": share["difficulty"],
            "require_name": share["require_name"],
            "time_limit_minutes": share.get("time_limit_minutes"),
            "questions": shuffled_questions,
        }

    async def submit_public_attempt(
        self,
        share_code: str,
        answers: dict[str, str],
        display_name: str | None = None,
        client_ip: str | None = None,
    ) -> dict:
        """Grade an anonymous attempt via shared link."""
        display_name = _sanitize_display_name(display_name)

        share = await self.get_share_by_code(share_code)
        if not share:
            raise ValueError("Quiz share not found, expired, or max attempts reached")

        # P0: Prevent duplicate submissions by same display_name on same share
        if display_name:
            dup = await self.db.fetchrow(
                "SELECT id FROM quiz_attempts WHERE share_id = $1 AND display_name = $2 LIMIT 1",
                uuid.UUID(share["share_id"]),
                display_name,
            )
            if dup:
                raise ValueError(f"You have already submitted this quiz as '{html.unescape(display_name)}'.")

        quiz_id = share["quiz_id"]

        # Load questions with answers
        q_rows = await self.db.fetch(
            "SELECT id, question_num, correct_answer, explanation FROM quiz_questions WHERE quiz_id = $1 ORDER BY question_num",
            uuid.UUID(quiz_id),
        )

        from .quiz_grader import QuizGrader

        questions = []
        for qr in q_rows:
            correct_raw = qr["correct_answer"]
            if isinstance(correct_raw, str):
                correct_raw = json.loads(correct_raw)
            questions.append({
                "id": str(qr["id"]),
                "question_num": qr["question_num"],
                "correct_answer": correct_raw,
                "explanation": qr["explanation"],
            })

        grader = QuizGrader()
        result = grader.grade(questions, answers)

        # Persist attempt — link to exam if this is an exam share
        attempt_id = str(uuid.uuid4())
        now = datetime.now(timezone.utc)

        exam_id_val = None
        if share.get("is_exam"):
            exam_row = await self.db.fetchrow(
                "SELECT id FROM exams WHERE share_id = $1 LIMIT 1",
                uuid.UUID(share["share_id"]),
            )
            if exam_row:
                exam_id_val = exam_row["id"]

        await self.db.execute(
            """
            INSERT INTO quiz_attempts (id, quiz_id, user_id, share_id, display_name,
                                       answers, total_score, correct_count, total_count,
                                       started_at, completed_at, status, client_ip, exam_id)
            VALUES ($1, $2, $3, $4, $5, $6, $7, $8, $9, $10, $11, $12, $13, $14)
            """,
            uuid.UUID(attempt_id),
            uuid.UUID(quiz_id),
            None,  # anonymous
            uuid.UUID(share["share_id"]),
            display_name,
            json.dumps(answers),
            result["total_score"],
            result["correct_count"],
            result["total_count"],
            now,
            now,
            "completed",
            client_ip,
            exam_id_val,
        )

        logger.info(
            f"Public attempt {attempt_id} on share {share_code}: "
            f"{result['correct_count']}/{result['total_count']}"
        )

        return {"attempt_id": attempt_id, **result}

    async def revoke_share(self, share_id: str, user_id: str) -> bool:
        """Deactivate a share link."""
        result = await self.db.execute(
            "UPDATE quiz_shares SET is_active = FALSE WHERE id = $1 AND created_by = $2",
            uuid.UUID(share_id),
            user_id,
        )
        return "UPDATE 1" in result
