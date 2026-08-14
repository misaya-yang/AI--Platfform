"""Typed API contracts for the connector catalog (admin-managed provider definitions).

The catalog maps onto the existing ``connector_configs`` columns; ``mode``
(live | ingest | both) is a product-level view of the same row.
"""

from __future__ import annotations

from typing import Any, Literal

from pydantic import BaseModel, ConfigDict, Field

ConnectorMode = Literal["live", "ingest", "both"]


class ConnectorAuthConfig(BaseModel):
    """OAuth app credentials/endpoints the admin manages for a provider."""

    client_id: str = Field("", description="OAuth client ID")
    client_secret: str | None = Field(
        None, description="OAuth client secret — write-only, never echoed back"
    )
    auth_url: str = Field("", description="Authorization endpoint URL")
    token_url: str = Field("", description="Token endpoint URL")
    scopes: str = Field("", description="Space-separated default scopes")
    redirect_uri: str | None = Field(None, description="OAuth redirect URI override")


class ConnectorMcpToolInfo(BaseModel):
    """Informational MCP tool surfaced by a connector provider."""

    name: str = Field("", description="Stable tool name, e.g. confluence_read")
    description: str = Field("", description="Short human-readable description")


class ConnectorProviderDefinition(BaseModel):
    """Admin-managed connector provider definition (connector_configs row)."""

    model_config = ConfigDict(extra="forbid")

    provider: str = Field(..., min_length=1, max_length=64)
    display_name: str = Field(..., min_length=1, max_length=256)
    description: str | None = Field(None, max_length=4000)
    icon_url: str | None = Field(None, max_length=512)
    mode: ConnectorMode = "live"
    enabled: bool = True
    supports_sync: bool = False
    supports_search: bool = True
    auth: ConnectorAuthConfig | None = None
    mcp_tools: list[ConnectorMcpToolInfo] = Field(default_factory=list, max_length=64)
    extra_config: dict[str, Any] = Field(default_factory=dict)


class ConnectorProviderCreate(ConnectorProviderDefinition):
    """Create payload — same shape as the definition (client_secret write-only)."""


class ConnectorProviderUpdate(BaseModel):
    """Update payload — every field optional; absent/empty secret keeps current."""

    model_config = ConfigDict(extra="forbid")

    display_name: str | None = Field(None, min_length=1, max_length=256)
    description: str | None = Field(None, max_length=4000)
    icon_url: str | None = Field(None, max_length=512)
    mode: ConnectorMode | None = None
    enabled: bool | None = None
    supports_sync: bool | None = None
    supports_search: bool | None = None
    auth: ConnectorAuthConfig | None = None
    mcp_tools: list[ConnectorMcpToolInfo] | None = Field(None, max_length=64)
    extra_config: dict[str, Any] | None = None


class ConnectorToggleRequest(BaseModel):
    """PATCH body for enabling/disabling a provider without a full update."""

    enabled: bool


class ConnectorProviderResponse(ConnectorProviderDefinition):
    """Read model — client_secret is never serialized."""

    tenant_id: str = ""
    created_at: str | None = None
    updated_at: str | None = None
