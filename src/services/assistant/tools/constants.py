"""
Tool name constants — single source of truth.

ADR-003 Phase 1: Centralizes all tool name strings used across
agent_loop.py, tool_invoker.py, builtin_tools.py, etc.
"""

from enum import Enum


class ToolName(str, Enum):
    """Canonical tool names. Use these instead of hardcoded strings."""

    # Retrieval
    SEARCH_KB = "search_knowledge_base"
    SEARCH_WEB = "search_web"

    # Generation
    GENERATE_IMAGE = "generate_image"
    GENERATE_DOCUMENT = "generate_document"
    GENERATE_PPTX = "generate_pptx"
    GENERATE_QUIZ = "generate_quiz"

    # Analysis
    EXECUTE_CODE = "execute_python_code"

    # Utility
    UPDATE_MEMORY = "update_user_memory"


# Sets for quick membership checks
RETRIEVAL_TOOLS = {ToolName.SEARCH_KB, ToolName.SEARCH_WEB}
GENERATION_TOOLS = {ToolName.GENERATE_IMAGE, ToolName.GENERATE_DOCUMENT, ToolName.GENERATE_PPTX, ToolName.GENERATE_QUIZ}

# Tools always available in Q&A mode (no creation intent)
QA_TOOLS = {ToolName.SEARCH_KB, ToolName.SEARCH_WEB, ToolName.UPDATE_MEMORY}
