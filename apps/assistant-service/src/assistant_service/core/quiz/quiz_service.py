"""Quiz generation service layered on shared quiz persistence operations."""

from __future__ import annotations

import json
import logging
import uuid
from datetime import datetime, timezone
from typing import TYPE_CHECKING, Any

if TYPE_CHECKING:
    from ai_gateway_core.persistence import DatabaseStorageLike

from ai_gateway_core.quiz import QuizAccessService, QuizGrader

from .quiz_generator import QuizGenerator

logger = logging.getLogger(__name__)


class QuizService(QuizAccessService):
    """Assistant-owned generation plus inherited shared persistence operations."""

    def __init__(
        self,
        db: DatabaseStorageLike,
        generator: QuizGenerator | None = None,
        grader: QuizGrader | None = None,
    ) -> None:
        super().__init__(db=db, grader=grader)
        self.generator = generator

    async def create_quiz(
        self,
        tenant_id: str,
        user_id: str,
        dataset_ids: list[str],
        kb_chunks: list[dict[str, Any]],
        topic: str | None = None,
        question_count: int = 5,
        question_types: list[str] | None = None,
        difficulty: str = "medium",
        language: str = "auto",
        model_id: str | None = None,
    ) -> dict:
        """Generate a quiz from KB chunks and persist it."""
        if self.generator is None:
            raise RuntimeError("QuizService has no generator; creation is not supported")
        quiz_data = await self.generator.generate(
            kb_chunks=kb_chunks,
            topic=topic,
            question_count=question_count,
            question_types=question_types,
            difficulty=difficulty,
            language=language,
            model_id=model_id,
        )

        quiz_id = uuid.uuid4()
        now = datetime.now(timezone.utc)
        await self.db.execute(
            """
            INSERT INTO quizzes (id, tenant_id, created_by, title, description,
                                 dataset_ids, topic, question_count, difficulty,
                                 config, status, created_at, updated_at)
            VALUES ($1, $2, $3, $4, $5, $6, $7, $8, $9, $10, $11, $12, $13)
            """,
            quiz_id,
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

        questions = quiz_data["questions"]
        question_rows = []
        for question in questions:
            question_id = uuid.uuid4()
            question["id"] = str(question_id)
            question_rows.append(
                (
                    question_id,
                    quiz_id,
                    question["question_num"],
                    question.get("question_type", "mc_single"),
                    question["question_text"],
                    json.dumps(question.get("options", [])),
                    json.dumps(question.get("correct_answer", [])),
                    question.get("explanation", ""),
                    json.dumps(question.get("source_chunk_ids", [])),
                    now,
                )
            )

        await self.db.executemany(
            """
            INSERT INTO quiz_questions (id, quiz_id, question_num, question_type,
                                        question_text, options, correct_answer,
                                        explanation, source_chunks, created_at)
            VALUES ($1, $2, $3, $4, $5, $6, $7, $8, $9, $10)
            """,
            question_rows,
        )

        logger.info("Created quiz %s with %s questions", quiz_id, len(questions))
        return {
            "quiz_id": str(quiz_id),
            "title": quiz_data["title"],
            "description": quiz_data.get("description", ""),
            "topic": topic,
            "difficulty": difficulty,
            "question_count": len(questions),
            "questions": [
                {
                    "id": question["id"],
                    "question_num": question["question_num"],
                    "question_type": question.get("question_type", "mc_single"),
                    "question_text": question["question_text"],
                    "options": question.get("options", []),
                }
                for question in questions
            ],
        }
