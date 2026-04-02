"""MCP Management API — list servers, tools, refresh connections."""

from __future__ import annotations

import logging

from fastapi import APIRouter, Depends, HTTPException, Request

from ...core.auth.user_resolver import UserContext
from ..deps import get_user_context

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
):
    """List configured MCP servers and their connection status."""
    mgr = _get_mcp_manager(request)
    return {"servers": mgr.get_servers_status()}


@router.get("/tools")
async def list_mcp_tools(
    request: Request,
    user: UserContext = Depends(get_user_context),
):
    """List all tools registered from MCP servers."""
    from ...services.assistant.tools.tool_registry import ToolCategory, get_tool_registry
    registry = get_tool_registry()
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
):
    """Re-discover tools from an MCP server (hot-reload)."""
    mgr = _get_mcp_manager(request)
    results = await mgr.refresh_tools(server_name)
    count = results.get(server_name, -1)
    if count < 0:
        raise HTTPException(404, f"MCP server '{server_name}' not found or refresh failed")
    return {"server": server_name, "tools_refreshed": count}
