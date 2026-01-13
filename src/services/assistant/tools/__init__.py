"""
Assistant tools module.

Phase 2: Agentic Tool Registry + Execution Layer

Provides:
- Tool Registry for centralized tool management
- Built-in tools (KB Search, Web Search)
- Tool execution lifecycle management
- Schema generation for OpenAI/Anthropic
"""

from .tavily_search import TavilySearchTool
from .tool_registry import (
    ToolRegistry,
    ToolDefinition,
    ToolParameter,
    ToolExample,
    ToolCategory,
    ToolRiskLevel,
    ToolExecutor,
    ToolCallRequest,
    ToolCallResult,
    get_tool_registry,
    register_tool,
)
from .builtin_tools import (
    KB_SEARCH_DEFINITION,
    WEB_SEARCH_DEFINITION,
    KBSearchExecutor,
    WebSearchExecutor,
    register_builtin_tools,
)

__all__ = [
    # Tavily
    "TavilySearchTool",
    # Tool Registry
    "ToolRegistry",
    "ToolDefinition",
    "ToolParameter",
    "ToolExample",
    "ToolCategory",
    "ToolRiskLevel",
    "ToolExecutor",
    "ToolCallRequest",
    "ToolCallResult",
    "get_tool_registry",
    "register_tool",
    # Built-in tools
    "KB_SEARCH_DEFINITION",
    "WEB_SEARCH_DEFINITION",
    "KBSearchExecutor",
    "WebSearchExecutor",
    "register_builtin_tools",
]
