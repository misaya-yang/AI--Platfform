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
from .scenario_analysis_prompts import (
    SCENARIO_TYPES,
    EXPERT_TEMPLATES,
    SCENARIO_DETECTION_PROMPT,
    MULTI_DIMENSIONAL_ANALYSIS_PROMPT,
    DOCUMENT_ANALYSIS_PROMPT,
    DOCUMENT_QA_PROMPT,
    KB_ENHANCED_ANALYSIS_PROMPT,
    build_scenario_detection_prompt,
    build_analysis_prompt,
    build_document_analysis_prompt,
    build_document_qa_prompt,
    build_kb_enhanced_prompt,
)
# Manus-style modular prompts (v2)
from .system_prompt_v2 import (
    AGENT_IDENTITY,
    AGENT_LOOP,
    OUTPUT_RULES,
    KB_CONTEXT_TEMPLATE,
    WEB_CONTEXT_TEMPLATE,
    DOCUMENT_CONTEXT_TEMPLATE,
    build_system_prompt_v2,
    inject_kb_context,
    inject_web_context,
    inject_document_context,
    inject_user_preferences,
    get_default_system_prompt,
    get_minimal_system_prompt,
    get_tool_focused_system_prompt,
)
from .guardrails import (
    GUARDRAILS,
    CUSTOMER_SERVICE_GUARDRAILS,
    TECHNICAL_SUPPORT_GUARDRAILS,
    SALES_CONSULTATION_GUARDRAILS,
    get_guardrails,
    get_minimal_guardrails,
)
from .agent_freedom import (
    AGENT_FREEDOM,
    TECHNICAL_FREEDOM,
    CUSTOMER_SERVICE_FREEDOM,
    ANALYSIS_FREEDOM,
    get_agent_freedom,
    get_minimal_agent_freedom,
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
    # Scenario Analysis Prompts
    "SCENARIO_TYPES",
    "EXPERT_TEMPLATES",
    "SCENARIO_DETECTION_PROMPT",
    "MULTI_DIMENSIONAL_ANALYSIS_PROMPT",
    "DOCUMENT_ANALYSIS_PROMPT",
    "DOCUMENT_QA_PROMPT",
    "KB_ENHANCED_ANALYSIS_PROMPT",
    "build_scenario_detection_prompt",
    "build_analysis_prompt",
    "build_document_analysis_prompt",
    "build_document_qa_prompt",
    "build_kb_enhanced_prompt",
    # Manus-style System Prompt v2
    "AGENT_IDENTITY",
    "AGENT_LOOP",
    "OUTPUT_RULES",
    "KB_CONTEXT_TEMPLATE",
    "WEB_CONTEXT_TEMPLATE",
    "DOCUMENT_CONTEXT_TEMPLATE",
    "build_system_prompt_v2",
    "inject_kb_context",
    "inject_web_context",
    "inject_document_context",
    "inject_user_preferences",
    "get_default_system_prompt",
    "get_minimal_system_prompt",
    "get_tool_focused_system_prompt",
    # Guardrails
    "GUARDRAILS",
    "CUSTOMER_SERVICE_GUARDRAILS",
    "TECHNICAL_SUPPORT_GUARDRAILS",
    "SALES_CONSULTATION_GUARDRAILS",
    "get_guardrails",
    "get_minimal_guardrails",
    # Agent Freedom
    "AGENT_FREEDOM",
    "TECHNICAL_FREEDOM",
    "CUSTOMER_SERVICE_FREEDOM",
    "ANALYSIS_FREEDOM",
    "get_agent_freedom",
    "get_minimal_agent_freedom",
]
