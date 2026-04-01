"""
Quiz API — Generate, take, and grade quizzes from KB content.

Endpoints:
- POST /assistant/quiz/generate  — Generate a new quiz
- GET  /assistant/quiz/list      — List user's quizzes
- GET  /assistant/quiz/{quiz_id} — Get quiz details (no answers)
- POST /assistant/quiz/{quiz_id}/submit — Submit answers for grading
- DELETE /assistant/quiz/{quiz_id} — Delete a quiz
"""

from __future__ import annotations

import logging
from typing import Any

from fastapi import APIRouter, Depends, HTTPException, Query, Request
from pydantic import BaseModel, Field

from ...core.auth.user_resolver import UserContext
from ...services.assistant.quiz_generator import QuizGenerator
from ...services.assistant.quiz_grader import QuizGrader
from ...services.assistant.quiz_service import QuizService
from ..deps import get_user_context

router = APIRouter(prefix="/assistant/quiz", tags=["quiz"])
logger = logging.getLogger(__name__)


# ---------------------------------------------------------------------------
# Schemas
# ---------------------------------------------------------------------------


class QuizGenerateRequest(BaseModel):
    dataset_ids: list[str] = Field(..., min_length=1, description="KB dataset IDs to source questions from")
    topic: str | None = Field(None, description="Optional topic focus")
    question_count: int = Field(5, ge=1, le=10, description="Number of questions")
    difficulty: str = Field("medium", description="easy / medium / hard")
    language: str = Field("auto", description="Language code or 'auto'")
    model_id: str | None = Field(None, description="Override LLM model for generation")


class QuizSubmitRequest(BaseModel):
    answers: dict[str, str] = Field(..., description="question_id → selected option label")


class QuizGenerateResponse(BaseModel):
    quiz_id: str
    title: str
    description: str | None = None
    topic: str | None = None
    difficulty: str
    question_count: int
    questions: list[dict[str, Any]]


class QuizListResponse(BaseModel):
    quizzes: list[dict[str, Any]]
    total: int


class QuizAttemptResponse(BaseModel):
    attempt_id: str
    total_score: float
    correct_count: int
    total_count: int
    per_question: list[dict[str, Any]]


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


def _get_quiz_service(request: Request) -> QuizService:
    """Build a QuizService from app state."""
    db = getattr(request.app.state, "database", None)
    if db is None:
        raise HTTPException(503, "Database not available")

    registry = getattr(request.app.state, "model_registry", None)
    if registry is None:
        raise HTTPException(503, "Model registry not available")

    generator = QuizGenerator(registry)
    grader = QuizGrader()
    return QuizService(db=db, generator=generator, grader=grader)


# ---------------------------------------------------------------------------
# Endpoints
# ---------------------------------------------------------------------------


@router.post("/generate", response_model=QuizGenerateResponse)
async def generate_quiz(
    body: QuizGenerateRequest,
    request: Request,
    user: UserContext = Depends(get_user_context),
):
    """Generate a quiz from KB datasets."""
    svc = _get_quiz_service(request)

    # Retrieve KB chunks for quiz generation
    kb_service = getattr(request.app.state, "knowledge_service", None)
    if kb_service is None:
        raise HTTPException(503, "Knowledge service not available")

    # Retrieve chunks from each dataset
    all_chunks: list[dict[str, Any]] = []
    query = body.topic or "key concepts and important information"

    for dataset_id in body.dataset_ids:
        try:
            results, _meta = await kb_service.retrieve(
                user=user,
                dataset_id=dataset_id,
                query=query,
                top_k=20,
                mode="hybrid",
            )
            for r in results:
                all_chunks.append({
                    "content": r.text,
                    "score": r.score,
                    "metadata": r.metadata or {},
                    "segment_id": getattr(r, "segment_id", None),
                    "document_id": getattr(r, "document_id", None),
                })
        except Exception as e:
            logger.warning(f"Failed to retrieve from dataset {dataset_id}: {e}")

    if not all_chunks:
        raise HTTPException(400, "No content retrieved from the specified datasets. Ensure the datasets have indexed documents.")

    try:
        quiz = await svc.create_quiz(
            tenant_id=user.tenant_id,
            user_id=user.user_id,
            dataset_ids=body.dataset_ids,
            kb_chunks=all_chunks,
            topic=body.topic,
            question_count=body.question_count,
            difficulty=body.difficulty,
            language=body.language,
            model_id=body.model_id,
        )
    except ValueError as e:
        raise HTTPException(422, str(e))
    except Exception as e:
        logger.error(f"Quiz generation failed: {e}", exc_info=True)
        raise HTTPException(500, f"Quiz generation failed: {e}")

    return quiz


@router.get("/list", response_model=QuizListResponse)
async def list_quizzes(
    request: Request,
    user: UserContext = Depends(get_user_context),
    limit: int = Query(20, ge=1, le=100),
    offset: int = Query(0, ge=0),
):
    """List the current user's quizzes."""
    svc = _get_quiz_service(request)
    quizzes, total = await svc.list_quizzes(
        tenant_id=user.tenant_id,
        user_id=user.user_id,
        limit=limit,
        offset=offset,
    )
    return QuizListResponse(quizzes=quizzes, total=total)


@router.get("/{quiz_id}")
async def get_quiz(
    quiz_id: str,
    request: Request,
    user: UserContext = Depends(get_user_context),
):
    """Get quiz details (questions without answers)."""
    svc = _get_quiz_service(request)
    quiz = await svc.get_quiz(quiz_id, user.tenant_id, include_answers=False)
    if not quiz:
        raise HTTPException(404, "Quiz not found")
    return quiz


@router.post("/{quiz_id}/submit", response_model=QuizAttemptResponse)
async def submit_quiz(
    quiz_id: str,
    body: QuizSubmitRequest,
    request: Request,
    user: UserContext = Depends(get_user_context),
):
    """Submit quiz answers and receive grading results."""
    svc = _get_quiz_service(request)
    try:
        result = await svc.submit_attempt(
            quiz_id=quiz_id,
            tenant_id=user.tenant_id,
            user_id=user.user_id,
            answers=body.answers,
        )
    except ValueError as e:
        raise HTTPException(404, str(e))

    return result


@router.delete("/{quiz_id}")
async def delete_quiz(
    quiz_id: str,
    request: Request,
    user: UserContext = Depends(get_user_context),
):
    """Delete a quiz (only by creator)."""
    svc = _get_quiz_service(request)
    deleted = await svc.delete_quiz(quiz_id, user.tenant_id, user.user_id)
    if not deleted:
        raise HTTPException(404, "Quiz not found or not authorized to delete")
    return {"deleted": True}
