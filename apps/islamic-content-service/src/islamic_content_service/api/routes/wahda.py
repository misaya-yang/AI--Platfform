"""Sheikh Wahda recommendation & interaction API routes."""
from __future__ import annotations

from fastapi import APIRouter, Depends, HTTPException, Request

from ..deps import get_wahda_service
from ..schemas.wahda import (
    FeedbackRequest, FeedbackResponse,
    GenerateRecommendationsRequest, GenerateRecommendationsResponse,
    RecommendationResponse,
    RecommendedQuestionItem, RecommendedQuestionsResponse,
    ShareContentResponse, ShareCreateResponse, ShareRequest,
    TrendingResponse,
    TypeaheadResponse,
    UpdateRecommendedQuestionRequest,
)

router = APIRouter(prefix="/wahda", tags=["Sheikh Wahda"])


@router.get("/recommendations", response_model=RecommendationResponse)
async def get_recommendations(
    svc=Depends(get_wahda_service),
):
    """Get today's recommended questions (religious event or daily rotation)."""
    return await svc.get_recommendations()


@router.get("/typeahead", response_model=TypeaheadResponse)
async def get_typeahead(
    q: str = "",
    svc=Depends(get_wahda_service),
):
    """Get input suggestions based on English question word prefix."""
    suggestions = await svc.get_typeahead(q)
    return TypeaheadResponse(suggestions=suggestions)


@router.get("/trending", response_model=TrendingResponse)
async def get_trending(
    svc=Depends(get_wahda_service),
):
    """Get trending questions from the past 7 days."""
    return await svc.get_trending()


@router.post("/feedback", response_model=FeedbackResponse)
async def submit_feedback(
    body: FeedbackRequest,
    request: Request,
    svc=Depends(get_wahda_service),
):
    """Submit thumbs up/down feedback on a message."""
    tenant_id = request.headers.get("X-Tenant-Id", "default")
    user_id = request.headers.get("X-User-Id", "anonymous")
    feedback_id = await svc.submit_feedback(
        tenant_id=tenant_id,
        user_id=user_id,
        session_id=body.session_id,
        message_index=body.message_index,
        feedback_type=body.feedback_type,
        reason=body.reason,
        comment=body.comment,
    )
    return FeedbackResponse(feedback_id=feedback_id)


@router.post("/share", response_model=ShareCreateResponse)
async def create_share(
    body: ShareRequest,
    request: Request,
    svc=Depends(get_wahda_service),
):
    """Create a shareable snapshot of a full conversation (ChatGPT-style).

    Pulls all messages from the LangGraph thread and stores as a read-only snapshot.
    """
    tenant_id = request.headers.get("X-Tenant-Id", "default")
    user_id = request.headers.get("X-User-Id", "anonymous")
    try:
        result = await svc.create_share(
            tenant_id=tenant_id,
            user_id=user_id,
            session_id=body.session_id,
            title=body.title,
            pre_fetched_messages=[{"role": m.role, "content": m.content} for m in body.messages] if body.messages else None,
        )
    except ValueError as e:
        raise HTTPException(400, str(e))
    except Exception as e:
        raise HTTPException(502, f"Failed to fetch conversation from agent: {e}")
    return ShareCreateResponse(**result)


@router.get("/share/{share_id}", response_model=ShareContentResponse)
async def get_share(
    share_id: str,
    svc=Depends(get_wahda_service),
):
    """Get shared conversation snapshot — full read-only chat history."""
    content = await svc.get_share(share_id)
    if not content:
        raise HTTPException(404, "Share not found or expired")
    return ShareContentResponse(**content)


# ------------------------------------------------------------------
# Recommended Questions (personalized, AI-generated)
# ------------------------------------------------------------------

@router.get("/recommended-questions", response_model=RecommendedQuestionsResponse)
async def get_recommended_questions(
    request: Request,
    date: str | None = None,
    svc=Depends(get_wahda_service),
):
    """Get today's personalized recommended questions for the current user."""
    from datetime import date as Date
    tenant_id = request.headers.get("X-Tenant-Id", "default")
    user_id = request.headers.get("X-User-Id", "anonymous")
    target_date = None
    if date:
        try:
            target_date = Date.fromisoformat(date)
        except ValueError:
            raise HTTPException(400, f"Invalid date format: {date}. Expected YYYY-MM-DD.")
    result = await svc.get_recommended_questions(tenant_id, user_id, target_date)
    return RecommendedQuestionsResponse(**result)


@router.post("/recommended-questions/generate", response_model=GenerateRecommendationsResponse)
async def generate_recommendations(
    body: GenerateRecommendationsRequest,
    request: Request,
    svc=Depends(get_wahda_service),
):
    """Generate recommended questions from the user's conversation history."""
    tenant_id = request.headers.get("X-Tenant-Id", "default")
    user_id = request.headers.get("X-User-Id", "anonymous")
    try:
        result = await svc.generate_recommendations(
            tenant_id=tenant_id,
            user_id=user_id,
            session_ids=body.session_ids,
            count=body.count,
            date_strategy=body.date_strategy,
        )
    except RuntimeError as e:
        raise HTTPException(503, str(e))
    return GenerateRecommendationsResponse(**result)


@router.post(
    "/recommended-questions/{question_id}/regenerate",
    response_model=RecommendedQuestionItem,
)
async def regenerate_question(
    question_id: str,
    request: Request,
    svc=Depends(get_wahda_service),
):
    """Regenerate a single recommended question (dismisses old, creates new)."""
    import uuid as _uuid
    try:
        _uuid.UUID(question_id)
    except ValueError:
        raise HTTPException(400, "Invalid question_id format (expected UUID)")
    tenant_id = request.headers.get("X-Tenant-Id", "default")
    user_id = request.headers.get("X-User-Id", "anonymous")
    try:
        result = await svc.regenerate_question(tenant_id, user_id, question_id)
    except RuntimeError as e:
        raise HTTPException(503, str(e))
    if not result:
        raise HTTPException(404, "Question not found or could not regenerate")
    return RecommendedQuestionItem(**result)


@router.patch("/recommended-questions/{question_id}")
async def update_question_status(
    question_id: str,
    body: UpdateRecommendedQuestionRequest,
    request: Request,
    svc=Depends(get_wahda_service),
):
    """Update the status of a recommended question (dismiss, mark used)."""
    import uuid as _uuid
    try:
        _uuid.UUID(question_id)
    except ValueError:
        raise HTTPException(400, "Invalid question_id format (expected UUID)")
    tenant_id = request.headers.get("X-Tenant-Id", "default")
    user_id = request.headers.get("X-User-Id", "anonymous")
    updated = await svc.update_question_status(tenant_id, user_id, question_id, body.status)
    if not updated:
        raise HTTPException(404, "Question not found or not owned by user")
    return {"status": "ok", "question_id": question_id}


@router.get("/recommended-questions/history", response_model=RecommendedQuestionsResponse)
async def get_recommendations_history(
    request: Request,
    limit: int = 20,
    offset: int = 0,
    svc=Depends(get_wahda_service),
):
    """Get paginated history of all recommended questions for the current user."""
    tenant_id = request.headers.get("X-Tenant-Id", "default")
    user_id = request.headers.get("X-User-Id", "anonymous")
    result = await svc.get_recommendations_history(tenant_id, user_id, limit, offset)
    return RecommendedQuestionsResponse(**result)
