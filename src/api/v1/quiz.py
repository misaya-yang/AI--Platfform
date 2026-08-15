"""
Quiz API — DEPRECATED shim. The supported path is the in-chat ``generate_quiz``
assistant tool (apps/assistant-service/core/tools/quiz_tool.py), documented by
the ai-quiz agent plugin (agent-plugins/ai-quiz).

Endpoints (load-bearing only):
- GET  /assistant/quiz/{quiz_id}         — Get quiz details (no answers)
- POST /assistant/quiz/{quiz_id}/submit  — Submit answers for grading
- GET  /assistant/quiz/{quiz_id}/attempts — List attempts for a quiz
- DELETE /assistant/quiz/{quiz_id}       — Delete a quiz
- GET  /quiz/shared/{share_code}         — Public share (alias over artifact_shares)
- POST /quiz/shared/{share_code}/submit  — Anonymous submit (alias over artifact_shares)

Share creation/revocation moved to POST/DELETE /api/v1/artifact-shares
(src/api/v1/artifact_shares.py); quiz generation moved to the in-chat tool.
"""

from __future__ import annotations

import logging
import random
from typing import Any

from ai_gateway_core.quiz import QuizAccessService, QuizGrader
from ai_gateway_core.sharing import ArtifactShareManager
from fastapi import APIRouter, Depends, HTTPException, Query, Request
from pydantic import BaseModel, Field

from ...core.auth.user_resolver import UserContext
from ...core.client_ip import get_client_ip_from_request
from ..deps import enforce_rate_limit, get_user_context

router = APIRouter(prefix="/assistant/quiz", tags=["quiz"])
logger = logging.getLogger(__name__)


# ---------------------------------------------------------------------------
# Schemas
# ---------------------------------------------------------------------------


class QuizSubmitRequest(BaseModel):
    answers: dict[str, str] = Field(..., description="question_id → selected option label")


class PublicQuizSubmitRequest(BaseModel):
    answers: dict[str, str] = Field(..., description="question_id → selected option label")
    display_name: str | None = Field(None, description="Anonymous user's name")


class QuizAttemptResponse(BaseModel):
    attempt_id: str
    total_score: float
    correct_count: int
    total_count: int
    per_question: list[dict[str, Any]]


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


def _get_quiz_service(request: Request) -> QuizAccessService:
    """Build the shared read/grade/delete service from app state."""
    db = getattr(request.app.state, "database", None)
    if db is None:
        raise HTTPException(503, "Database not available")
    return QuizAccessService(db=db, grader=QuizGrader())


def _get_share_manager(request: Request) -> ArtifactShareManager:
    db = getattr(request.app.state, "database", None)
    if db is None:
        raise HTTPException(503, "Database not available")
    return ArtifactShareManager(db=db)


def _shuffle_options(questions: list[dict]) -> list[dict]:
    """Shuffle option display order per question. Labels stay attached to their text."""
    shuffled = []
    for q in questions:
        opts = list(q.get("options", []))
        random.shuffle(opts)
        shuffled.append({**q, "options": opts})
    return shuffled


# ---------------------------------------------------------------------------
# Authenticated endpoints
# ---------------------------------------------------------------------------


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
    limit: int = Query(100, ge=1, le=1000),
    offset: int = Query(0, ge=0),
    user: UserContext = Depends(get_user_context),
):
    """List all attempts for a quiz (creator sees all, others see own). Paginated."""
    svc = _get_quiz_service(request)
    return await svc.list_attempts(
        quiz_id, user.tenant_id, user.user_id, limit=limit, offset=offset,
    )


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
# Public endpoints (no auth required) — aliases over artifact_shares kind='quiz'
# ---------------------------------------------------------------------------

public_router = APIRouter(prefix="/quiz/shared", tags=["quiz-public"])


@public_router.get("/{share_code}")
async def get_shared_quiz(share_code: str, request: Request):
    """Get a quiz for public taking (no auth required). Returns questions without answers."""
    mgr = _get_share_manager(request)
    artifact = await mgr.get_public_artifact(share_code)
    if not artifact:
        raise HTTPException(404, "Quiz not found, expired, or max attempts reached")
    # Option display order is shuffled per viewer; answer keys stay server-side.
    questions = artifact.get("questions", [])
    artifact["questions"] = _shuffle_options(questions)
    return artifact


@public_router.post("/{share_code}/submit")
async def submit_shared_quiz(
    share_code: str,
    body: PublicQuizSubmitRequest,
    request: Request,
):
    """Submit answers for a shared quiz (no auth required)."""
    # Anonymous endpoint: IP-only rate limit to prevent submission spam
    await enforce_rate_limit(request, user=None, operation="quiz_submit_public")

    mgr = _get_share_manager(request)
    client_ip = get_client_ip_from_request(request)
    try:
        result = await mgr.submit_attempt(
            share_code=share_code,
            answers=body.answers,
            display_name=body.display_name,
            client_ip=client_ip,
        )
    except ValueError as e:
        msg = str(e)
        status = 409 if "already submitted" in msg else 404
        raise HTTPException(status, msg)
    return result
