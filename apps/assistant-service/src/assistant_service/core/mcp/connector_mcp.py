"""Connector MCP Service — re-export shim.

Phase 5d moved the canonical implementation to
``ai_gateway_core.connectors`` so gateway OAuth callback routes can
reach it without a compile-time dep on ``assistant_service``.
"""

from __future__ import annotations

from ai_gateway_core.connectors import ConnectorMCPService, get_connector_mcp_service

__all__ = ["ConnectorMCPService", "get_connector_mcp_service"]
