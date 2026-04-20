"""
Debug endpoint — list tools actually registered in the in-process
ToolRegistry right now.

Purpose: the model's "I don't have a create_page tool" problem can
have two very different causes — the code isn't deployed, or the code
is deployed but `register_confluence_tools()` was never called on this
process. Without a visibility endpoint, operators have to guess.

This returns what the model would see on the next request:
- total count
- per-category breakdown
- flagged Confluence tools (expected: confluence_read + confluence_write)

Authenticated (bearer token same as other /api/v1 routes) — the tool
names alone are low-sensitivity, but they reveal which integrations
this tenant has activated, which counts as reconnaissance info.
"""

from __future__ import annotations

from typing import Any

from fastapi import APIRouter, Depends

from ...core.auth.user_resolver import UserContext
from ...services.assistant.tools.tool_registry import get_tool_registry
from ..deps import get_user_context

router = APIRouter(prefix="/debug", tags=["Debug"])


@router.get("/tools")
async def list_registered_tools(
    user: UserContext = Depends(get_user_context),
) -> dict[str, Any]:
    """Return the live in-process tool registry contents.

    Operator sanity check: if `confluence_read` / `confluence_write` are
    missing here but the frontend shows Confluence 'connected', the
    startup auto-rehydration (apps/assistant-service/main.py lifespan)
    failed — check logs for 'Auto-registered Confluence tools'.
    """
    reg = get_tool_registry()
    tools = reg.list_tools()

    by_category: dict[str, list[str]] = {}
    for t in tools:
        cat = t.category.value if t.category else "unknown"
        by_category.setdefault(cat, []).append(t.name)

    confluence_tools = [t.name for t in tools if "confluence" in t.name]
    has_new_meta = "confluence_read" in confluence_tools and "confluence_write" in confluence_tools
    has_old_single_purpose = any(
        n in confluence_tools
        for n in (
            "search_confluence",
            "read_confluence_page",
            "create_confluence_page",
            "update_confluence_page",
            "delete_confluence_page",
            "add_confluence_comment",
            "list_confluence_spaces",
            "get_confluence_space",
        )
    )

    return {
        "total": len(tools),
        "by_category": by_category,
        "confluence": {
            "tools": confluence_tools,
            "new_meta_tools_present": has_new_meta,
            "stale_single_purpose_tools_present": has_old_single_purpose,
            "status": (
                "OK — new meta-tools loaded"
                if has_new_meta and not has_old_single_purpose
                else (
                    "STALE — old 8-tool code still active; rebuild and redeploy"
                    if has_old_single_purpose
                    else (
                        "MISSING — Confluence tools not registered. "
                        "Check that the confluence_connections DB table has "
                        "an active row and that startup auto-rehydration ran."
                    )
                )
            ),
        },
    }
