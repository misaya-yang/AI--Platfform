"""Deterministic, token-aware selection over a dynamic tool catalog.

Selection is a prompt-size optimization, never an authorization mechanism.
In discover mode only discovery bridges and explicit capability pins are
advertised directly; deferred tools remain reachable through tool_search.
"""

from __future__ import annotations

import json
from typing import TYPE_CHECKING

from ai_gateway_core.logging import get_logger, record_internal_exception

from .tool_discovery import DISCOVERY_TOOL_NAMES

if TYPE_CHECKING:
    from .tool_registry import ToolDefinition

logger = get_logger(__name__)

DEFAULT_TOOL_TOKEN_BUDGET = 2000
TIER_ALWAYS = 0
TIER_BUILTIN = 1
TIER_SKILL = 2
TIER_MCP = 3

ALWAYS_INCLUDE = {
    *DISCOVERY_TOOL_NAMES,
}


def _estimate_tool_tokens(tool_def: ToolDefinition, compact: bool = True) -> int:
    try:
        try:
            schema = tool_def.to_openai_schema(compact=compact)
        except TypeError:
            schema = tool_def.to_openai_schema()
        return max(1, len(json.dumps(schema, ensure_ascii=False, default=str)) // 4)
    except Exception as exc:
        record_internal_exception(
            __name__, "assistant.core.tools.tool_selector.internal_failure", exc
        )
        return 80


def _is_always_visible(tool_def: ToolDefinition, pinned: set[str]) -> bool:
    name = tool_def.name
    return name in ALWAYS_INCLUDE or name in pinned


def _get_tier(tool_def: ToolDefinition) -> int:
    if tool_def.name in ALWAYS_INCLUDE:
        return TIER_ALWAYS
    if tool_def.name.startswith("mcp_"):
        return TIER_MCP
    category = getattr(getattr(tool_def, "category", None), "value", "")
    if category == "skill":
        return TIER_SKILL
    return TIER_BUILTIN


def select_tools(
    all_tools: list[ToolDefinition],
    user_message: str,
    max_tokens: int = DEFAULT_TOOL_TOKEN_BUDGET,
    *,
    mode: str = "discover",
    extra_always: set[str] | None = None,
) -> list[ToolDefinition]:
    """Select direct schemas without changing the authorized catalog.

    Discover mode keeps requests to the stable discovery bridges. Explicit
    capability pins bypass the prompt budget. Budget mode retains the broader
    token-budgeted behavior for callers that explicitly request it.
    """

    if not all_tools:
        return []
    pinned = extra_always or set()
    has_discovery = any(getattr(tool, "name", "") in DISCOVERY_TOOL_NAMES for tool in all_tools)
    del user_message
    scored: list[tuple[ToolDefinition, int, int]] = []
    for tool in all_tools:
        scored.append((tool, _get_tier(tool), _estimate_tool_tokens(tool)))

    scored.sort(
        key=lambda item: (
            0 if item[1] == TIER_ALWAYS else 1,
            item[1],
            item[0].name.casefold(),
        )
    )

    # A catalog without discovery bridges has no deferred-call path. Preserve
    # the bounded direct schemas in that case, otherwise tools used by narrow
    # agents and approval-resume continuations would become unreachable.
    advertise_budget = mode == "budget" or not has_discovery
    selected: list[ToolDefinition] = []
    used_tokens = 0
    for tool, _tier, tokens in scored:
        direct = _is_always_visible(tool, pinned)
        within_budget = used_tokens + tokens <= max_tokens
        if direct or (advertise_budget and within_budget):
            selected.append(tool)
            used_tokens += tokens
        else:
            logger.debug(
                "Tool schema deferred (tool=%s tokens=%s budget=%s/%s)",
                tool.name,
                tokens,
                used_tokens,
                max_tokens,
            )

    if len(selected) < len(all_tools):
        logger.info(
            "[ToolSelector] %s/%s direct schemas, %s/%s tokens; deferred tools remain discoverable",
            len(selected),
            len(all_tools),
            used_tokens,
            max_tokens,
        )
    return selected
