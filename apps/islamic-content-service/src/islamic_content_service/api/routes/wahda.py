"""Sheikh Wahda recommendation & interaction API routes."""
from __future__ import annotations

from fastapi import APIRouter, Depends, HTTPException, Request

from ..deps import get_wahda_service
from ..schemas.wahda import (
    FeedbackRequest, FeedbackResponse,
    RecommendationResponse,
    ShareContentResponse, ShareCreateResponse, ShareRequest,
    TrendingResponse,
    TypeaheadResponse,
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
