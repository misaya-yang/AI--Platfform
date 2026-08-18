from __future__ import annotations

from dataclasses import dataclass
from typing import Any

import pytest
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
    description: str = "test tool"
    when_to_use: str | None = None
    capability_metadata: dict[str, Any] | None = None

    def model_argument_schema(self) -> dict[str, Any]:
        return {"type": "object", "properties": {}}

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

    selected = select_tools(tools, user_message="summarize", max_tokens=10_000, mode="budget")

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

    selected = select_tools(
        [tool], user_message="completely unrelated wording", max_tokens=10_000, mode="budget"
    )

    assert selected == [tool]


def test_dynamic_catalog_order_is_stable_for_equal_scores() -> None:
    tools = [
        FakeToolDefinition("mcp_zeta__opaque", category=ToolCategory.MCP),
        FakeToolDefinition("mcp_alpha__opaque", category=ToolCategory.MCP),
    ]

    first = select_tools(tools, user_message="unmatched", max_tokens=10_000, mode="budget")
    second = select_tools(
        list(reversed(tools)), user_message="unmatched", max_tokens=10_000, mode="budget"
    )

    assert [item.name for item in first] == ["mcp_alpha__opaque", "mcp_zeta__opaque"]
    assert [item.name for item in second] == ["mcp_alpha__opaque", "mcp_zeta__opaque"]


def test_discovery_bridges_survive_a_tight_schema_budget() -> None:
    tools = [
        *tool_discovery_definitions(),
        FakeToolDefinition("mcp_vendor__large_schema", category=ToolCategory.MCP),
    ]

    selected = select_tools(tools, user_message="unmatched", max_tokens=1)

    assert {item.name for item in selected} == DISCOVERY_TOOL_NAMES


@pytest.mark.parametrize(
    "user_message",
    [
        "hello",
        "What is the capital of France?",
        "解释一下光合作用",
    ],
)
def test_discover_mode_plain_question_advertises_only_discovery_bridges(
    user_message: str,
) -> None:
    tools = [
        *tool_discovery_definitions(),
        FakeToolDefinition("spawn_subagent"),
        FakeToolDefinition("update_user_memory"),
        FakeToolDefinition("mcp_vendor__opaque", category=ToolCategory.MCP),
        FakeToolDefinition("generate_pptx"),
        FakeToolDefinition("mcp_docgen__generate_document", category=ToolCategory.MCP),
        FakeToolDefinition("search_knowledge_base"),
    ]

    selected = select_tools(tools, user_message=user_message, max_tokens=10_000)

    assert {item.name for item in selected} == DISCOVERY_TOOL_NAMES


def test_discover_mode_without_bridges_keeps_bounded_tools_reachable() -> None:
    tools = [
        FakeToolDefinition("spawn_subagent"),
        FakeToolDefinition("generate_pptx"),
    ]

    selected = select_tools(tools, user_message="hello", max_tokens=10_000)

    assert {item.name for item in selected} == {"spawn_subagent", "generate_pptx"}


def test_discover_mode_advertises_relevant_generation_backend() -> None:
    tools = [
        *tool_discovery_definitions(),
        FakeToolDefinition("mcp_docgen__generate_document", category=ToolCategory.MCP),
        FakeToolDefinition("mcp_vendor__opaque", category=ToolCategory.MCP),
        FakeToolDefinition("spawn_subagent"),
    ]

    selected = select_tools(tools, user_message="create a PDF document", max_tokens=10_000)
    names = {item.name for item in selected}

    assert "mcp_docgen__generate_document" in names
    assert "mcp_vendor__opaque" not in names
    assert "spawn_subagent" not in names


@pytest.mark.parametrize(
    "user_message",
    [
        "run Python code to verify the totals",
        "calculate this with python",
        "repair and test this function against edge cases",
    ],
)
def test_discover_mode_advertises_explicit_code_request(user_message: str) -> None:
    tools = [
        *tool_discovery_definitions(),
        FakeToolDefinition(
            "execute_python_code",
            description="Execute Python code to test and verify behavior.",
        ),
        FakeToolDefinition(
            "update_user_memory",
            description="Store a durable user preference.",
        ),
    ]

    selected = select_tools(tools, user_message=user_message, max_tokens=10_000)
    names = {item.name for item in selected}

    assert "execute_python_code" in names
    assert "update_user_memory" not in names


@pytest.mark.parametrize(
    "user_message",
    [
        "The financial report returned data for review.",
        "Analyze the financial data and explain the trends.",
        "Provide a legal analysis of this contract.",
        "Summarize the research data and key findings.",
    ],
)
def test_discover_mode_plain_analysis_does_not_advertise_code_executor(
    user_message: str,
) -> None:
    tools = [
        *tool_discovery_definitions(),
        FakeToolDefinition(
            "execute_python_code",
            description="Execute Python code for data analysis and research.",
        ),
    ]

    selected = select_tools(tools, user_message=user_message, max_tokens=10_000)

    assert {item.name for item in selected} == DISCOVERY_TOOL_NAMES


def test_ascii_alias_requires_a_complete_word_boundary() -> None:
    tools = [
        *tool_discovery_definitions(),
        FakeToolDefinition("custom_runner", relevance_keywords=["run"]),
    ]

    returned = select_tools(tools, user_message="The request returned successfully")
    explicit = select_tools(tools, user_message="Run the requested task")

    assert {item.name for item in returned} == DISCOVERY_TOOL_NAMES
    assert "custom_runner" in {item.name for item in explicit}


def test_discover_mode_advertises_explicitly_pinned_tool() -> None:
    tools = [
        *tool_discovery_definitions(),
        FakeToolDefinition("spawn_subagent"),
        FakeToolDefinition("update_user_memory"),
    ]

    selected = select_tools(
        tools,
        user_message="hello",
        max_tokens=1,
        extra_always={"spawn_subagent"},
    )
    names = {item.name for item in selected}

    assert names == {*DISCOVERY_TOOL_NAMES, "spawn_subagent"}
