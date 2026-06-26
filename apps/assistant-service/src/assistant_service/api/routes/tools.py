"""Tool + policy management endpoints.

Response shapes match the gateway's ``ToolsListResponse`` /
``AssistantPoliciesResponse`` so Phase 5b proxy routes see identical
payloads regardless of whether the flag is ON or OFF.
"""
from __future__ import annotations

from typing import Any

from fastapi import APIRouter, Depends, Request

from ...auth import UserContext, get_user_context
from ..deps import get_assistant_service

router = APIRouter()


def _enum_value(value: Any) -> str:
    return value.value if hasattr(value, "value") else str(value)


def _tool_catalog_entry(tool: Any) -> dict[str, Any]:
    """Serialize a tool catalog entry with additive capability metadata."""
    category = _enum_value(tool.category)
    risk_level = _enum_value(tool.risk_level)
    metadata = getattr(tool, "capability_metadata", None) or {}
    capability_kind = metadata.get(
        "kind",
        "mcp" if category == "mcp" else "skill" if category == "skill" else "tool",
    )
    trigger_examples = metadata.get("trigger_examples")
    if trigger_examples is None and capability_kind == "skill":
        trigger_examples = list(getattr(tool, "relevance_keywords", []) or [])[:8]

    entry = {
        "name": tool.name,
        "description": tool.description,
        "category": category,
        "risk_level": risk_level,
        "when_to_use": getattr(tool, "when_to_use", None),
        "when_not_to_use": getattr(tool, "when_not_to_use", None),
        "requires_confirmation": bool(getattr(tool, "requires_confirmation", False)),
        "required_permissions": list(getattr(tool, "required_permissions", []) or []),
        "capability_kind": capability_kind,
        "setup_state": metadata.get("setup_state", "ready"),
        "trigger_examples": list(trigger_examples or []),
    }

    for key in (
        "skill_name",
        "title",
        "summary",
        "version",
        "source",
        "tags",
        "generated",
        "lifecycle_status",
        "review_required",
        "activation_requirements",
        "mcp_server",
        "mcp_tool",
        "policy_scope",
        "external_service",
        "progressive_disclosure",
    ):
        if key in metadata:
            entry[key] = metadata[key]

    if capability_kind == "skill" and "progressive_disclosure" not in entry:
        entry["progressive_disclosure"] = {
            "level0": [
                "name",
                "description",
                "category",
                "risk_level",
                "setup_state",
                "trigger_examples",
            ],
            "level1_available": bool(getattr(tool, "when_to_use", None)),
            "level2_loaded": False,
        }

    return entry


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
        "tools": [_tool_catalog_entry(t) for t in tools]
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
