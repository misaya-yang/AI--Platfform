from __future__ import annotations

from dataclasses import dataclass
from typing import Any

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
