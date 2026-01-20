"""
System Prompts for Enterprise Agent.

This module provides prompt templates that clearly separate:
1. Guardrails (硬性要求) - Must be followed, non-negotiable
2. Agent Freedom (自由空间) - Agent can decide within boundaries

Design Philosophy:
- Guardrails define WHAT boundaries exist
- Agent decides HOW to work within boundaries
- Clear separation improves both compliance and creativity

Reference: https://manus.im/blog/Context-Engineering-for-AI-Agents-Lessons-from-Building-Manus
"""

from .generation_prompts import (
    DOCUMENT_GENERATION_SYSTEM_PROMPT,
    OUTLINE_GENERATION_PROMPT,
    SECTION_GENERATION_PROMPT,
    REPAIR_PROMPT,
    build_generation_prompt,
)
from .planning_prompts import (
    TASK_PLANNING_SYSTEM_PROMPT,
    INTENT_ANALYSIS_PROMPT,
    build_planning_prompt,
)

__all__ = [
    # Generation Prompts
    "DOCUMENT_GENERATION_SYSTEM_PROMPT",
    "OUTLINE_GENERATION_PROMPT",
    "SECTION_GENERATION_PROMPT",
    "REPAIR_PROMPT",
    "build_generation_prompt",
    # Planning Prompts
    "TASK_PLANNING_SYSTEM_PROMPT",
    "INTENT_ANALYSIS_PROMPT",
    "build_planning_prompt",
]
