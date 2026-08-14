"""
Artifact share API — kind-generic public sharing of agent artifacts.

Replaces the quiz-specific share endpoints (product-convergence PC-03).
kind='quiz' freezes a snapshot of the quiz payload + answer keys; the public
routes in src/api/v1/quiz.py read the same artifact_shares rows, so legacy
/quiz/shared/{code} links stay valid.
"""

from __future__ import annotations

import json
import logging
import uuid
from typing import Any

from ai_gateway_core.sharing import ArtifactShareManager
from fastapi import APIRouter, Depends, HTTPException, Request
from pydantic import BaseModel, Field

from ...core.auth.user_resolver import UserContext
from ..deps import get_user_context

router = APIRouter(prefix="/artifact-shares", tags=["artifact-shares"])
logger = logging.getLogger(__name__)


class ArtifactShareCreateRequest(BaseModel):
    kind: str = Field("quiz", description="Artifact kind; only 'quiz' is supported today")
    quiz_id: str | None = Field(None, description="Source quiz id (kind='quiz')")
    expires_hours: int | None = Field(None, description="Hours until expiry (None = never)")
    max_attempts: int | None = Field(None, description="Max attempts (None = unlimited)")
    require_name: bool = Field(True, description="Require name before taking")
    time_limit_minutes: int | None = Field(None, description="Time limit per attempt in minutes (None = unlimited)")


@router.post("")
async def create_artifact_share(
    body: ArtifactShareCreateRequest,
    request: Request,
    user: UserContext = Depends(get_user_context),
):
    """Create a public share with a frozen artifact snapshot."""
    db = getattr(request.app.state, "database", None)
    if db is None:
        raise HTTPException(503, "Database not available")
    mgr = ArtifactShareManager(db=db)

    if body.kind != "quiz":
        raise HTTPException(400, f"Unsupported artifact kind: {body.kind}")
    if not body.quiz_id:
        raise HTTPException(400, "quiz_id is required for kind='quiz'")

    # Verify quiz exists and belongs to the caller.
    quiz_row = await db.fetchrow(
        "SELECT id, tenant_id, title, description, question_count, difficulty "
        "FROM quizzes WHERE id = $1 AND tenant_id = $2 AND created_by = $3",
        uuid.UUID(body.quiz_id),
        user.tenant_id,
        user.user_id,
    )
    if not quiz_row:
        raise HTTPException(404, "Quiz not found or not authorized")

    # Freeze a snapshot: public questions + grading answer keys.
    q_rows = await db.fetch(
        "SELECT id, question_num, question_type, question_text, options, "
        "correct_answer, explanation FROM quiz_questions "
        "WHERE quiz_id = $1 ORDER BY question_num",
        uuid.UUID(body.quiz_id),
    )
    questions: list[dict[str, Any]] = []
    answer_keys: list[dict[str, Any]] = []
    for qr in q_rows:
        options = qr["options"]
        if isinstance(options, str):
            options = json.loads(options)
        correct = qr["correct_answer"]
        if isinstance(correct, str):
            correct = json.loads(correct)
        questions.append({
            "id": str(qr["id"]),
            "question_num": qr["question_num"],
            "question_type": qr["question_type"],
            "question_text": qr["question_text"],
            "options": options,
        })
        answer_keys.append({
            "id": str(qr["id"]),
            "question_num": qr["question_num"],
            "correct_answer": correct,
            "explanation": qr["explanation"],
        })

    payload = {
        "quiz_id": body.quiz_id,
        "description": quiz_row["description"],
        "question_count": quiz_row["question_count"],
        "difficulty": quiz_row["difficulty"],
        "questions": questions,
    }

    share = await mgr.create_share(
        kind="quiz",
        title=quiz_row["title"],
        payload=payload,
        answer_keys=answer_keys,
        tenant_id=user.tenant_id,
        user_id=user.user_id,
        expires_hours=body.expires_hours,
        max_attempts=body.max_attempts,
        require_name=body.require_name,
        time_limit_minutes=body.time_limit_minutes,
    )
    return {**share, "quiz_id": body.quiz_id, "quiz_title": quiz_row["title"]}


@router.delete("/{share_id}")
async def revoke_artifact_share(
    share_id: str,
    request: Request,
    user: UserContext = Depends(get_user_context),
):
    """Revoke a share link (creator only)."""
    mgr = ArtifactShareManager(db=getattr(request.app.state, "database", None))
    revoked = await mgr.revoke_share(share_id, user.user_id)
    if not revoked:
        raise HTTPException(404, "Share link not found or not authorized")
    return {"revoked": True}
