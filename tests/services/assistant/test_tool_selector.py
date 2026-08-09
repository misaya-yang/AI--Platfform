from __future__ import annotations

from dataclasses import dataclass
from typing import Any

from assistant_service.core.tools.tool_discovery import (
    DISCOVERY_TOOL_NAMES,
    tool_discovery_definitions,
)
from assistant_service.core.tools.tool_registry import ToolCategory
from assistant_service.core.tools.tool_selector import select_tools


@dataclass
class FakeToolDefinition:
    name: str
    category: Any = None
    relevance_keywords: list[str] | None = None

    def to_openai_schema(self, compact: bool = True) -> dict[str, Any]:
        del compact
        return {
            "type": "function",
            "function": {
                "name": self.name,
                "description": "test tool",
                "parameters": {"type": "object", "properties": {}},
            },
        }


def test_select_tools_tie_breaks_by_name_for_deterministic_schema_order() -> None:
    tools = [
        FakeToolDefinition("beta_report"),
        FakeToolDefinition("alpha_report"),
        FakeToolDefinition("gamma_report"),
    ]

    selected = select_tools(tools, user_message="summarize", max_tokens=10_000)

    assert [tool.name for tool in selected] == [
        "alpha_report",
        "beta_report",
        "gamma_report",
    ]


def test_dynamic_mcp_without_hardcoded_keywords_remains_selectable() -> None:
    tool = FakeToolDefinition(
        "mcp_new_vendor__opaque_action",
        category=ToolCategory.MCP,
        relevance_keywords=[],
    )

    selected = select_tools([tool], user_message="completely unrelated wording", max_tokens=10_000)

    assert selected == [tool]


def test_dynamic_catalog_order_is_stable_for_equal_scores() -> None:
    tools = [
        FakeToolDefinition("mcp_zeta__opaque", category=ToolCategory.MCP),
        FakeToolDefinition("mcp_alpha__opaque", category=ToolCategory.MCP),
    ]

    first = select_tools(tools, user_message="unmatched", max_tokens=10_000)
    second = select_tools(list(reversed(tools)), user_message="unmatched", max_tokens=10_000)

    assert [item.name for item in first] == ["mcp_alpha__opaque", "mcp_zeta__opaque"]
    assert [item.name for item in second] == ["mcp_alpha__opaque", "mcp_zeta__opaque"]


def test_discovery_bridges_survive_a_tight_schema_budget() -> None:
    tools = [
        *tool_discovery_definitions(),
        FakeToolDefinition("mcp_vendor__large_schema", category=ToolCategory.MCP),
    ]

    selected = select_tools(tools, user_message="unmatched", max_tokens=1)

    assert {item.name for item in selected} == DISCOVERY_TOOL_NAMES
