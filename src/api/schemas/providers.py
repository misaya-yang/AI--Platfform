"""
Provider and Model API Schemas.

Pydantic models for LLM provider and model management endpoints.
"""

from datetime import datetime
from decimal import Decimal
from typing import Literal

from pydantic import BaseModel, Field

# ============================================================================
# Provider Schemas
# ============================================================================


class ProviderBase(BaseModel):
    """Base provider fields."""

    provider_id: str = Field(
        ..., min_length=1, max_length=50, description="Unique provider identifier"
    )
    display_name: str = Field(..., min_length=1, max_length=100, description="Display name")
    api_type: str = Field(
        default="openai",
        description="API type: openai, anthropic, google, or google-vertex",
    )
    base_url: str | None = Field(None, max_length=500, description="Custom API base URL")
    is_enabled: bool = Field(default=True, description="Whether the provider is enabled")


class ProviderCreate(ProviderBase):
    """Request to create a provider."""

    api_key: str | None = Field(None, description="API key (will be encrypted)")


class ProviderUpdate(BaseModel):
    """Request to update a provider."""

    display_name: str | None = Field(None, min_length=1, max_length=100)
    api_type: str | None = None
    base_url: str | None = Field(None, max_length=500)
    api_key: str | None = Field(None, description="New API key (will be encrypted)")
    is_enabled: bool | None = None


class ProviderResponse(ProviderBase):
    """Provider response (without API key)."""

    tenant_id: str
    has_api_key: bool = Field(description="Whether API key is configured")
    created_at: datetime
    updated_at: datetime

    class Config:
        from_attributes = True


class ProviderListResponse(BaseModel):
    """List of providers response."""

    providers: list[ProviderResponse]
    total: int


class ProviderTestResult(BaseModel):
    """Result of testing provider connection."""

    success: bool
    message: str
    latency_ms: int | None = None


# ============================================================================
# Model Schemas
# ============================================================================

ModelType = Literal["llm", "image", "multimodal", "embedding", "reranker"]


class ModelBase(BaseModel):
    """Base model fields."""

    model_id: str = Field(
        ..., min_length=1, max_length=100, description="Model identifier for API calls"
    )
    provider_id: str = Field(
        ..., min_length=1, max_length=50, description="Provider this model belongs to"
    )
    display_name: str = Field(..., min_length=1, max_length=100, description="Display name")
    model_type: ModelType = Field(
        default="llm",
        description="Model type: llm, image, multimodal, embedding, or reranker",
    )
    context_window: int = Field(default=128000, ge=1, description="Context window size in tokens")
    max_output_tokens: int = Field(default=4096, ge=1, description="Maximum output tokens")
    supports_vision: bool = Field(
        default=False, description="Whether the model supports vision/images"
    )
    supports_tools: bool = Field(
        default=True, description="Whether the model supports tool calling"
    )
    input_price_per_1k: Decimal = Field(
        default=Decimal("0"), ge=0, description="Input price per 1K tokens in USD"
    )
    output_price_per_1k: Decimal = Field(
        default=Decimal("0"), ge=0, description="Output price per 1K tokens in USD"
    )
    access_level: str = Field(
        default="public", description="Access level: public, premium, or admin"
    )
    is_enabled: bool = Field(default=True, description="Whether the model is enabled")
    sort_order: int = Field(default=0, description="Sort order for UI display")


class ModelCreate(ModelBase):
    """Request to create a model."""

    pass


class ModelUpdate(BaseModel):
    """Request to update a model.

    ``model_id`` is renameable — the frontend now exposes it in the edit
    dialog so a provider's model rename (e.g. ``gemini-3-flash-preview`` →
    ``gemini-3.1-flash``) doesn't require delete+recreate. Server-side
    rename pushes an UPDATE on the primary key; the pricing-table sync
    also follows the new id.
    """

    model_id: str | None = Field(None, min_length=1, max_length=100)
    display_name: str | None = Field(None, min_length=1, max_length=100)
    model_type: ModelType | None = None
    context_window: int | None = Field(None, ge=1)
    max_output_tokens: int | None = Field(None, ge=1)
    supports_vision: bool | None = None
    supports_tools: bool | None = None
    input_price_per_1k: Decimal | None = Field(None, ge=0)
    output_price_per_1k: Decimal | None = Field(None, ge=0)
    access_level: str | None = None
    is_enabled: bool | None = None
    sort_order: int | None = None


class ModelResponse(ModelBase):
    """Model response."""

    tenant_id: str
    created_at: datetime
    updated_at: datetime

    class Config:
        from_attributes = True


class ModelListResponse(BaseModel):
    """List of models response."""

    models: list[ModelResponse]
    total: int


class ModelToggleRequest(BaseModel):
    """Request to toggle model enabled state."""

    is_enabled: bool
