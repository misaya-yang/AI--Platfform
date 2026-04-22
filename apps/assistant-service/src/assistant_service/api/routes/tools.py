"""Tool management endpoints."""

from __future__ import annotations

from fastapi import APIRouter, Depends, Request

from ...auth import UserContext, get_user_context

router = APIRouter()


@router.get("/tools")
async def list_tools(request: Request, user: UserContext = Depends(get_user_context)):
    """List available tools."""
    from ...core.tools import get_tool_registry
    registry = get_tool_registry()
    tools = registry.list_tools()
    return {
        "tools": [
            {
                "name": t.name,
                "description": t.description,
                "category": t.category.value if hasattr(t.category, "value") else str(t.category),
                "risk_level": t.risk_level.value if hasattr(t.risk_level, "value") else str(t.risk_level),
            }
            for t in tools
        ]
    }
