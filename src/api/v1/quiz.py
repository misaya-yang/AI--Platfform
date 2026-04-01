"""
Quiz API — Generate, take, and grade quizzes from KB content.

Endpoints:
- POST /assistant/quiz/generate         — Generate a new quiz
- POST /assistant/quiz/generate/stream   — Generate with SSE progress
- GET  /assistant/quiz/list              — List user's quizzes
- GET  /assistant/quiz/{quiz_id}         — Get quiz details (no answers)
- POST /assistant/quiz/{quiz_id}/submit  — Submit answers for grading
- GET  /assistant/quiz/{quiz_id}/attempts — List attempts for a quiz
- DELETE /assistant/quiz/{quiz_id}       — Delete a quiz
- POST /assistant/quiz/{quiz_id}/share   — Generate share link
"""

from __future__ import annotations

import json
import logging
import time
from typing import Any

from fastapi import APIRouter, Depends, HTTPException, Query, Request
from fastapi.responses import StreamingResponse
from pydantic import BaseModel, Field

from ...core.auth.user_resolver import UserContext
from ...services.assistant.quiz_generator import QuizGenerator
from ...services.assistant.quiz_grader import QuizGrader
from ...services.assistant.quiz_service import QuizService
from ...services.assistant.quiz_share_manager import QuizShareManager
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
    question_types: list[str] = Field(default=["mc_single"], description="mc_single, mc_multi, true_false, short_answer")
    difficulty: str = Field("medium", description="easy / medium / hard")
    language: str = Field("auto", description="Language code or 'auto'")
    model_id: str | None = Field(None, description="Override LLM model for generation")


class QuizSubmitRequest(BaseModel):
    answers: dict[str, str] = Field(..., description="question_id → selected option label")


class QuizShareRequest(BaseModel):
    expires_hours: int | None = Field(None, description="Hours until expiry (None = never)")
    max_attempts: int | None = Field(None, description="Max attempts (None = unlimited)")
    require_name: bool = Field(True, description="Require name before taking")


class PublicQuizSubmitRequest(BaseModel):
    answers: dict[str, str] = Field(..., description="question_id → selected option label")
    display_name: str | None = Field(None, description="Anonymous user's name")


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
    grader = QuizGrader(model_registry=registry)
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
            question_types=body.question_types,
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


@router.post("/generate/stream")
async def generate_quiz_stream(
    body: QuizGenerateRequest,
    request: Request,
    user: UserContext = Depends(get_user_context),
):
    """Generate a quiz with SSE progress events."""
    svc = _get_quiz_service(request)
    kb_service = getattr(request.app.state, "knowledge_service", None)
    if kb_service is None:
        raise HTTPException(503, "Knowledge service not available")

    def _sse(event_type: str, data: Any) -> str:
        payload = {"event_type": event_type, "data": data, "timestamp": time.time()}
        return f"data: {json.dumps(payload, ensure_ascii=False)}\n\n"

    async def event_generator():
        try:
            yield _sse("quiz:status", {"message": "Retrieving knowledge base content..."})

            all_chunks: list[dict[str, Any]] = []
            query = body.topic or "key concepts and important information"

            for dataset_id in body.dataset_ids:
                try:
                    results, _meta = await kb_service.retrieve(
                        user=user, dataset_id=dataset_id, query=query, top_k=20, mode="hybrid",
                    )
                    for r in results:
                        all_chunks.append({
                            "content": r.text, "score": r.score,
                            "metadata": r.metadata or {},
                            "segment_id": getattr(r, "segment_id", None),
                            "document_id": getattr(r, "document_id", None),
                        })
                except Exception as e:
                    logger.warning(f"Failed to retrieve from dataset {dataset_id}: {e}")

            yield _sse("quiz:status", {"message": f"Retrieved {len(all_chunks)} relevant passages"})

            if not all_chunks:
                yield _sse("quiz:error", {"message": "No content retrieved from datasets"})
                return

            yield _sse("quiz:status", {"message": "Generating questions..."})

            quiz = await svc.create_quiz(
                tenant_id=user.tenant_id, user_id=user.user_id,
                dataset_ids=body.dataset_ids, kb_chunks=all_chunks,
                topic=body.topic, question_count=body.question_count,
                question_types=body.question_types, difficulty=body.difficulty,
                language=body.language, model_id=body.model_id,
            )

            yield _sse("quiz:status", {"message": "Quiz ready!"})
            yield _sse("quiz:ready", quiz)

        except Exception as e:
            logger.error(f"Quiz stream error: {e}", exc_info=True)
            yield _sse("quiz:error", {"message": str(e)})

    return StreamingResponse(
        event_generator(),
        media_type="text/event-stream",
        headers={"Cache-Control": "no-cache", "Connection": "keep-alive", "X-Accel-Buffering": "no"},
    )


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


@router.get("/{quiz_id}/attempts")
async def list_attempts(
    quiz_id: str,
    request: Request,
    user: UserContext = Depends(get_user_context),
):
    """List all attempts for a quiz (creator sees all, others see own)."""
    svc = _get_quiz_service(request)
    attempts = await svc.list_attempts(quiz_id, user.tenant_id, user.user_id)
    return {"attempts": attempts}


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


# ---------------------------------------------------------------------------
# Share endpoints (authenticated)
# ---------------------------------------------------------------------------


def _get_share_manager(request: Request) -> QuizShareManager:
    db = getattr(request.app.state, "database", None)
    if db is None:
        raise HTTPException(503, "Database not available")
    return QuizShareManager(db=db)


@router.post("/{quiz_id}/share")
async def create_share_link(
    quiz_id: str,
    body: QuizShareRequest,
    request: Request,
    user: UserContext = Depends(get_user_context),
):
    """Generate a shareable link for a quiz."""
    mgr = _get_share_manager(request)
    try:
        share = await mgr.create_share(
            quiz_id=quiz_id,
            user_id=user.user_id,
            tenant_id=user.tenant_id,
            expires_hours=body.expires_hours,
            max_attempts=body.max_attempts,
            require_name=body.require_name,
        )
    except ValueError as e:
        raise HTTPException(404, str(e))

    return share


@router.delete("/{quiz_id}/share/{share_id}")
async def revoke_share_link(
    quiz_id: str,
    share_id: str,
    request: Request,
    user: UserContext = Depends(get_user_context),
):
    """Revoke a share link."""
    mgr = _get_share_manager(request)
    revoked = await mgr.revoke_share(share_id, user.user_id)
    if not revoked:
        raise HTTPException(404, "Share link not found or not authorized")
    return {"revoked": True}


# ---------------------------------------------------------------------------
# Public endpoints (no auth required)
# ---------------------------------------------------------------------------

public_router = APIRouter(prefix="/quiz/shared", tags=["quiz-public"])


@public_router.get("/{share_code}")
async def get_shared_quiz(share_code: str, request: Request):
    """Get a quiz for public taking (no auth required). Returns questions without answers."""
    mgr = _get_share_manager(request)
    quiz = await mgr.get_public_quiz(share_code)
    if not quiz:
        raise HTTPException(404, "Quiz not found, expired, or max attempts reached")
    return quiz


@public_router.post("/{share_code}/submit")
async def submit_shared_quiz(
    share_code: str,
    body: PublicQuizSubmitRequest,
    request: Request,
):
    """Submit answers for a shared quiz (no auth required)."""
    mgr = _get_share_manager(request)
    try:
        result = await mgr.submit_public_attempt(
            share_code=share_code,
            answers=body.answers,
            display_name=body.display_name,
        )
    except ValueError as e:
        raise HTTPException(404, str(e))
    return result
