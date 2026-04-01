"""
Quiz Service — Orchestrates quiz generation, persistence, and grading.
"""

from __future__ import annotations

import json
import logging
import uuid
from datetime import datetime, timezone
from typing import Any

from ...persistence.database import DatabaseStorage
from .quiz_generator import QuizGenerator
from .quiz_grader import QuizGrader

logger = logging.getLogger(__name__)


class QuizService:
    """Core quiz orchestration: create, fetch, grade, persist."""

    def __init__(
        self,
        db: DatabaseStorage,
        generator: QuizGenerator,
        grader: QuizGrader | None = None,
    ) -> None:
        self.db = db
        self.generator = generator
        self.grader = grader or QuizGrader()

    # -------------------------------------------------------------------------
    # Create
    # -------------------------------------------------------------------------

    async def create_quiz(
        self,
        tenant_id: str,
        user_id: str,
        dataset_ids: list[str],
        kb_chunks: list[dict[str, Any]],
        topic: str | None = None,
        question_count: int = 5,
        difficulty: str = "medium",
        language: str = "auto",
        model_id: str | None = None,
    ) -> dict:
        """Generate a quiz from KB chunks and persist it."""
        # 1. Generate via LLM
        quiz_data = await self.generator.generate(
            kb_chunks=kb_chunks,
            topic=topic,
            question_count=question_count,
            difficulty=difficulty,
            language=language,
            model_id=model_id,
        )

        # 2. Persist quiz
        quiz_id = str(uuid.uuid4())
        now = datetime.now(timezone.utc)

        await self.db.execute(
            """
            INSERT INTO quizzes (id, tenant_id, created_by, title, description,
                                 dataset_ids, topic, question_count, difficulty,
                                 config, status, created_at, updated_at)
            VALUES ($1, $2, $3, $4, $5, $6, $7, $8, $9, $10, $11, $12, $13)
            """,
            uuid.UUID(quiz_id),
            tenant_id,
            user_id,
            quiz_data.get("title", "Knowledge Quiz"),
            quiz_data.get("description", ""),
            json.dumps(dataset_ids),
            topic or "",
            len(quiz_data["questions"]),
            difficulty,
            json.dumps({"language": language, "model_id": model_id}),
            "ready",
            now,
            now,
        )

        # 3. Persist questions
        questions = quiz_data["questions"]
        q_rows = []
        for q in questions:
            q_id = str(uuid.uuid4())
            q["id"] = q_id
            q_rows.append((
                uuid.UUID(q_id),
                uuid.UUID(quiz_id),
                q["question_num"],
                q.get("question_type", "mc_single"),
                q["question_text"],
                json.dumps(q.get("options", [])),
                json.dumps(q.get("correct_answer", [])),
                q.get("explanation", ""),
                json.dumps(q.get("source_chunk_ids", [])),
                now,
            ))

        await self.db.executemany(
            """
            INSERT INTO quiz_questions (id, quiz_id, question_num, question_type,
                                        question_text, options, correct_answer,
                                        explanation, source_chunks, created_at)
            VALUES ($1, $2, $3, $4, $5, $6, $7, $8, $9, $10)
            """,
            q_rows,
        )

        logger.info(f"Created quiz {quiz_id} with {len(questions)} questions")

        # 4. Return quiz without answers
        return {
            "quiz_id": quiz_id,
            "title": quiz_data["title"],
            "description": quiz_data.get("description", ""),
            "topic": topic,
            "difficulty": difficulty,
            "question_count": len(questions),
            "questions": [
                {
                    "id": q["id"],
                    "question_num": q["question_num"],
                    "question_type": q.get("question_type", "mc_single"),
                    "question_text": q["question_text"],
                    "options": q.get("options", []),
                }
                for q in questions
            ],
        }

    # -------------------------------------------------------------------------
    # Read
    # -------------------------------------------------------------------------

    async def get_quiz(
        self,
        quiz_id: str,
        tenant_id: str,
        include_answers: bool = False,
    ) -> dict | None:
        """Fetch a quiz with its questions."""
        row = await self.db.fetchrow(
            "SELECT * FROM quizzes WHERE id = $1 AND tenant_id = $2",
            uuid.UUID(quiz_id),
            tenant_id,
        )
        if not row:
            return None

        q_rows = await self.db.fetch(
            "SELECT * FROM quiz_questions WHERE quiz_id = $1 ORDER BY question_num",
            uuid.UUID(quiz_id),
        )

        questions = []
        for qr in q_rows:
            q: dict[str, Any] = {
                "id": str(qr["id"]),
                "question_num": qr["question_num"],
                "question_type": qr["question_type"],
                "question_text": qr["question_text"],
                "options": qr["options"] if isinstance(qr["options"], list) else json.loads(qr["options"] or "[]"),
            }
            if include_answers:
                raw_answer = qr["correct_answer"]
                q["correct_answer"] = raw_answer if isinstance(raw_answer, list) else json.loads(raw_answer or "[]")
                q["explanation"] = qr["explanation"]
            questions.append(q)

        return {
            "quiz_id": str(row["id"]),
            "title": row["title"],
            "description": row["description"],
            "topic": row["topic"],
            "difficulty": row["difficulty"],
            "question_count": row["question_count"],
            "status": row["status"],
            "created_at": row["created_at"].isoformat() if row["created_at"] else None,
            "questions": questions,
        }

    async def list_quizzes(
        self,
        tenant_id: str,
        user_id: str,
        limit: int = 20,
        offset: int = 0,
    ) -> tuple[list[dict], int]:
        """List quizzes for a user (paginated)."""
        total_row = await self.db.fetchrow(
            "SELECT count(*) AS cnt FROM quizzes WHERE tenant_id = $1 AND created_by = $2",
            tenant_id,
            user_id,
        )
        total = total_row["cnt"] if total_row else 0

        rows = await self.db.fetch(
            """
            SELECT id, title, description, topic, difficulty, question_count,
                   status, created_at
            FROM quizzes
            WHERE tenant_id = $1 AND created_by = $2
            ORDER BY created_at DESC
            LIMIT $3 OFFSET $4
            """,
            tenant_id,
            user_id,
            limit,
            offset,
        )

        quizzes = [
            {
                "quiz_id": str(r["id"]),
                "title": r["title"],
                "description": r["description"],
                "topic": r["topic"],
                "difficulty": r["difficulty"],
                "question_count": r["question_count"],
                "status": r["status"],
                "created_at": r["created_at"].isoformat() if r["created_at"] else None,
            }
            for r in rows
        ]

        return quizzes, total

    # -------------------------------------------------------------------------
    # Submit / Grade
    # -------------------------------------------------------------------------

    async def submit_attempt(
        self,
        quiz_id: str,
        tenant_id: str,
        user_id: str,
        answers: dict[str, str],
    ) -> dict:
        """Grade a submission and persist the attempt."""
        # Load quiz with answers
        quiz = await self.get_quiz(quiz_id, tenant_id, include_answers=True)
        if not quiz:
            raise ValueError(f"Quiz {quiz_id} not found")

        # Grade
        result = self.grader.grade(quiz["questions"], answers)

        # Persist attempt
        attempt_id = str(uuid.uuid4())
        now = datetime.now(timezone.utc)

        await self.db.execute(
            """
            INSERT INTO quiz_attempts (id, quiz_id, user_id, answers,
                                       total_score, correct_count, total_count,
                                       started_at, completed_at, status)
            VALUES ($1, $2, $3, $4, $5, $6, $7, $8, $9, $10)
            """,
            uuid.UUID(attempt_id),
            uuid.UUID(quiz_id),
            user_id,
            json.dumps(answers),
            result["total_score"],
            result["correct_count"],
            result["total_count"],
            now,
            now,
            "completed",
        )

        logger.info(
            f"Quiz attempt {attempt_id}: {result['correct_count']}/{result['total_count']} "
            f"({result['total_score']:.0%})"
        )

        return {
            "attempt_id": attempt_id,
            **result,
        }

    # -------------------------------------------------------------------------
    # Delete
    # -------------------------------------------------------------------------

    async def delete_quiz(
        self,
        quiz_id: str,
        tenant_id: str,
        user_id: str,
    ) -> bool:
        """Delete a quiz (only by creator)."""
        result = await self.db.execute(
            "DELETE FROM quizzes WHERE id = $1 AND tenant_id = $2 AND created_by = $3",
            uuid.UUID(quiz_id),
            tenant_id,
            user_id,
        )
        deleted = "DELETE 1" in result
        if deleted:
            logger.info(f"Deleted quiz {quiz_id}")
        return deleted
