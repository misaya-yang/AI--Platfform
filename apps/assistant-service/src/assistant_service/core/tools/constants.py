"""
Tool name constants — single source of truth.

ADR-003 Phase 1: Centralizes all tool name strings used across
agent_loop.py, tool_invoker.py, builtin_tools.py, etc.
"""

from enum import Enum


class ToolName(str, Enum):
    """Canonical tool names. Use these instead of hardcoded strings."""

    # Retrieval — web search is delegated to model-native APIs
    # (Qwen `enable_search`, Anthropic `web_search_20250305`); the only
    # in-tree retrieval tool is the KB.
    SEARCH_KB = "search_knowledge_base"

    # Generation
    GENERATE_IMAGE = "generate_image"
    GENERATE_DOCUMENT = "generate_document"
    GENERATE_PPTX = "generate_pptx"
    GENERATE_QUIZ = "generate_quiz"

    # Analysis
    EXECUTE_CODE = "execute_python_code"

    # Utility
    UPDATE_MEMORY = "update_user_memory"
    SPAWN_SUBAGENT = "spawn_subagent"


# Sets for quick membership checks
RETRIEVAL_TOOLS = {ToolName.SEARCH_KB}
GENERATION_TOOLS = {ToolName.GENERATE_IMAGE, ToolName.GENERATE_DOCUMENT, ToolName.GENERATE_PPTX, ToolName.GENERATE_QUIZ}

# Tools always available in Q&A mode (no creation intent)
QA_TOOLS = {ToolName.SEARCH_KB, ToolName.UPDATE_MEMORY}
