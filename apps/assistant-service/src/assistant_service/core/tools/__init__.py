"""
Assistant tools module.

Phase 2: Agentic Tool Registry + Execution Layer

Provides:
- Tool Registry for centralized tool management
- Built-in tools (KB Search, Web Search)
- Tool execution lifecycle management
- Schema generation for OpenAI/Anthropic
"""

from .builtin_tools import (
    KB_SEARCH_DEFINITION,
    KBSearchExecutor,
    register_builtin_tools,
)
from .code_executor_tool import (
    CODE_EXECUTOR_TOOL,
    CodeExecutorToolExecutor,
    register_code_executor_tool,
)
from .document_generator_tool import (
    DOCUMENT_GENERATION_DEFINITION,
    DocumentGeneratorExecutor,
    register_document_generation_tool,
)
from .pptx_generator_tool import (
    PPTX_GENERATION_DEFINITION,
    PPTXGeneratorExecutor,
    register_pptx_generation_tool,
)
from .quiz_tool import (
    QUIZ_GENERATION_DEFINITION,
    QuizGeneratorExecutor,
    register_quiz_tool,
)
from .tool_registry import (
    ToolCallRequest,
    ToolCallResult,
    ToolCategory,
    ToolDefinition,
    ToolExample,
    ToolExecutor,
    ToolParameter,
    ToolRegistry,
    ToolRiskLevel,
    get_tool_registry,
    register_tool,
)

__all__ = [
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
    "KBSearchExecutor",
    "register_builtin_tools",
    # Code executor tool
    "CODE_EXECUTOR_TOOL",
    "CodeExecutorToolExecutor",
    "register_code_executor_tool",
    # Document generator tool
    "DOCUMENT_GENERATION_DEFINITION",
    "DocumentGeneratorExecutor",
    "register_document_generation_tool",
    # PPTX generator tool
    "PPTX_GENERATION_DEFINITION",
    "PPTXGeneratorExecutor",
    "register_pptx_generation_tool",
    # Quiz generator tool
    "QUIZ_GENERATION_DEFINITION",
    "QuizGeneratorExecutor",
    "register_quiz_tool",
]
