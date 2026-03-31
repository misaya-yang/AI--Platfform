"""Pydantic schemas for Sheikh Wahda recommendation APIs."""
from __future__ import annotations

from pydantic import BaseModel, Field


# --- Recommendations ---

class RecommendationResponse(BaseModel):
    type: str = Field(description="'religious_event' or 'daily'")
    event_name: str | None = None
    questions: list[str]
    date: str


# --- Typeahead ---

class TypeaheadResponse(BaseModel):
    suggestions: list[str]


# --- Trending ---

class TrendingResponse(BaseModel):
    questions: list[str]
    period: str
    source: str = Field(description="'trending' or 'fallback'")


# --- Feedback ---

class FeedbackRequest(BaseModel):
    session_id: str
    message_index: int | None = None
    feedback_type: str = Field(pattern="^(thumbs_up|thumbs_down)$")
    reason: str | None = Field(
        default=None,
        pattern="^(incorrect|not_asked|slow|style|safety|other)$",
    )
    comment: str | None = None


class FeedbackResponse(BaseModel):
    status: str = "ok"
    feedback_id: str


# --- Share ---

class ShareRequest(BaseModel):
    session_id: str
    message_index: int
    question: str
    answer: str


class ShareCreateResponse(BaseModel):
    share_id: str
    share_url: str
    expires_at: str | None = None


class ShareContentResponse(BaseModel):
    question: str
    answer: str
    created_at: str
    agent_name: str = "Sheikh Wahda"
