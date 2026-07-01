"""MCP Management API — list servers, tools, refresh connections."""

from __future__ import annotations

import logging

from fastapi import APIRouter, Depends, HTTPException, Request

from ...core.auth.permissions import Capability
from ...core.auth.user_resolver import UserContext
from ..deps import AuthContext, get_auth_context, get_user_context, require_gateway_capability

logger = logging.getLogger(__name__)
router = APIRouter(prefix="/assistant/mcp", tags=["mcp"])


def _get_mcp_manager(request: Request):
    mgr = getattr(request.app.state, "mcp_manager", None)
    if mgr is None:
        raise HTTPException(503, "MCP manager not initialized")
    return mgr


@router.get("/servers")
async def list_mcp_servers(
    request: Request,
    user: UserContext = Depends(get_user_context),
    auth: AuthContext = Depends(get_auth_context),
):
    """List configured MCP servers and their connection status."""
    require_gateway_capability(request, auth, Capability.GATEWAY_MCP_READ)
    mgr = _get_mcp_manager(request)
    return {"servers": mgr.get_servers_status()}


@router.get("/tools")
async def list_mcp_tools(
    request: Request,
    user: UserContext = Depends(get_user_context),
    auth: AuthContext = Depends(get_auth_context),
):
    """List all tools registered from MCP servers."""
    require_gateway_capability(request, auth, Capability.GATEWAY_MCP_READ)
    from ai_gateway_core.enums import ToolCategory
    registry = getattr(request.app.state, "tool_registry", None)
    if registry is None:
        return {"tools": [], "total": 0}
    mcp_tools = [
        {"name": t.name, "description": t.description, "category": t.category.value}
        for t in registry.list_tools()
        if t.category == ToolCategory.MCP
    ]
    return {"tools": mcp_tools, "total": len(mcp_tools)}


@router.post("/servers/{server_name}/refresh")
async def refresh_mcp_server(
    server_name: str,
    request: Request,
    user: UserContext = Depends(get_user_context),
    auth: AuthContext = Depends(get_auth_context),
):
    """Re-discover tools from an MCP server (hot-reload)."""
    _ = user
    require_gateway_capability(request, auth, Capability.GATEWAY_MCP_WRITE)
    mgr = _get_mcp_manager(request)
    results = await mgr.refresh_tools(server_name)
    count = results.get(server_name, -1)
    if count < 0:
        raise HTTPException(404, f"MCP server '{server_name}' not found or refresh failed")
    return {"server": server_name, "tools_refreshed": count}
