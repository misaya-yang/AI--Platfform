"""Tool + policy management endpoints.

Response shapes match the gateway's ``ToolsListResponse`` /
``AssistantPoliciesResponse`` so Phase 5b proxy routes see identical
payloads regardless of whether the flag is ON or OFF.
"""
from __future__ import annotations

from fastapi import APIRouter, Depends, Request

from ...auth import UserContext, get_user_context
from ..deps import get_assistant_service

router = APIRouter()


@router.get("/tools")
async def list_tools(request: Request, user: UserContext = Depends(get_user_context)):
    """List tools visible to this user.

    Matches the gateway's ``ToolsListResponse`` shape:
    ``{"tools": [{name, description, category, risk_level,
    when_to_use?, when_not_to_use?}, ...]}``

    Permission filtering uses the same logic as the gateway —
    ``tool_registry.list_tools(user=...)`` filters by ``required_permissions``
    (role / tier lattice defined in tool_registry itself).
    """
    from ...core.tools import get_tool_registry

    registry = get_tool_registry()
    tools = registry.list_tools(user=user)

    return {
        "tools": [
            {
                "name": t.name,
                "description": t.description,
                "category": t.category.value if hasattr(t.category, "value") else str(t.category),
                "risk_level": t.risk_level.value if hasattr(t.risk_level, "value") else str(t.risk_level),
                "when_to_use": getattr(t, "when_to_use", None),
                "when_not_to_use": getattr(t, "when_not_to_use", None),
            }
            for t in tools
        ]
    }


@router.get("/policies")
async def get_policies(request: Request, user: UserContext = Depends(get_user_context)):
    """Return the assistant gateway policy snapshot.

    Matches the gateway's ``AssistantPoliciesResponse`` shape:
    ``{"policies": {...}}``. The snapshot comes from
    ``AssistantService.get_gateway_policies()`` which reads from the
    in-process ``execution_gateway`` — no persistent state crosses
    the GW/AS boundary here.
    """
    _ = user  # auth is enforced by get_user_context; user-scoping is
    # applied inside execution_gateway if needed
    assistant = get_assistant_service(request)
    return {"policies": assistant.get_gateway_policies()}
