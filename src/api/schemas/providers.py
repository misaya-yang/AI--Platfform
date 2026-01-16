"""
Provider and Model API Schemas.

Pydantic models for LLM provider and model management endpoints.
"""

from datetime import datetime
from decimal import Decimal
from typing import List, Optional

from pydantic import BaseModel, Field


# ============================================================================
# Provider Schemas
# ============================================================================


class ProviderBase(BaseModel):
    """Base provider fields."""
    provider_id: str = Field(..., min_length=1, max_length=50, description="Unique provider identifier")
    display_name: str = Field(..., min_length=1, max_length=100, description="Display name")
    api_type: str = Field(default="openai", description="API type: openai, anthropic, or google")
    base_url: Optional[str] = Field(None, max_length=500, description="Custom API base URL")
    is_enabled: bool = Field(default=True, description="Whether the provider is enabled")


class ProviderCreate(ProviderBase):
    """Request to create a provider."""
    api_key: Optional[str] = Field(None, description="API key (will be encrypted)")


class ProviderUpdate(BaseModel):
    """Request to update a provider."""
    display_name: Optional[str] = Field(None, min_length=1, max_length=100)
    api_type: Optional[str] = None
    base_url: Optional[str] = Field(None, max_length=500)
    api_key: Optional[str] = Field(None, description="New API key (will be encrypted)")
    is_enabled: Optional[bool] = None


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
    providers: List[ProviderResponse]
    total: int


class ProviderTestResult(BaseModel):
    """Result of testing provider connection."""
    success: bool
    message: str
    latency_ms: Optional[int] = None


# ============================================================================
# Model Schemas
# ============================================================================


class ModelBase(BaseModel):
    """Base model fields."""
    model_id: str = Field(..., min_length=1, max_length=100, description="Model identifier for API calls")
    provider_id: str = Field(..., min_length=1, max_length=50, description="Provider this model belongs to")
    display_name: str = Field(..., min_length=1, max_length=100, description="Display name")
    context_window: int = Field(default=128000, ge=1, description="Context window size in tokens")
    max_output_tokens: int = Field(default=4096, ge=1, description="Maximum output tokens")
    supports_vision: bool = Field(default=False, description="Whether the model supports vision/images")
    supports_tools: bool = Field(default=True, description="Whether the model supports tool calling")
    input_price_per_1k: Decimal = Field(default=Decimal("0"), ge=0, description="Input price per 1K tokens in USD")
    output_price_per_1k: Decimal = Field(default=Decimal("0"), ge=0, description="Output price per 1K tokens in USD")
    access_level: str = Field(default="public", description="Access level: public, premium, or admin")
    is_enabled: bool = Field(default=True, description="Whether the model is enabled")
    sort_order: int = Field(default=0, description="Sort order for UI display")


class ModelCreate(ModelBase):
    """Request to create a model."""
    pass


class ModelUpdate(BaseModel):
    """Request to update a model."""
    display_name: Optional[str] = Field(None, min_length=1, max_length=100)
    context_window: Optional[int] = Field(None, ge=1)
    max_output_tokens: Optional[int] = Field(None, ge=1)
    supports_vision: Optional[bool] = None
    supports_tools: Optional[bool] = None
    input_price_per_1k: Optional[Decimal] = Field(None, ge=0)
    output_price_per_1k: Optional[Decimal] = Field(None, ge=0)
    access_level: Optional[str] = None
    is_enabled: Optional[bool] = None
    sort_order: Optional[int] = None


class ModelResponse(ModelBase):
    """Model response."""
    tenant_id: str
    created_at: datetime
    updated_at: datetime

    class Config:
        from_attributes = True


class ModelListResponse(BaseModel):
    """List of models response."""
    models: List[ModelResponse]
    total: int


class ModelToggleRequest(BaseModel):
    """Request to toggle model enabled state."""
    is_enabled: bool
