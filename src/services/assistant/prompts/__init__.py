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
    PRESENTATION_GENERATION_PROMPT,
    REPORT_GENERATION_PROMPT,
    EMAIL_GENERATION_PROMPT,
    SUMMARY_GENERATION_PROMPT,
    build_generation_prompt,
    build_outline_prompt,
    build_section_prompt,
    build_repair_prompt,
    build_presentation_prompt,
    build_report_prompt,
    build_email_prompt,
    build_summary_prompt,
)
from .planning_prompts import (
    # Constants
    INTENT_TYPES,
    COMPLEXITY_LEVELS,
    TASK_PLANNING_SYSTEM_PROMPT,
    INTENT_ANALYSIS_PROMPT,
    AGENTIC_TASK_DECOMPOSITION_PROMPT,
    TODO_TRACKING_PROMPT,
    TASK_REFINEMENT_PROMPT,
    EXECUTION_REFLECTION_PROMPT,
    # Helper functions
    get_intent_types_description,
    get_intent_type_info,
    get_complexity_info,
    # Builder functions
    build_planning_prompt,
    build_intent_analysis_prompt,
    build_task_decomposition_prompt,
    build_todo_tracking_prompt,
    build_task_refinement_prompt,
    build_execution_reflection_prompt,
)
from .scenario_analysis_prompts import (
    # Constants
    SCENARIO_TYPES,
    EXPERT_TEMPLATES,
    # Prompt Templates
    SCENARIO_DETECTION_PROMPT,
    FAST_SCENARIO_DETECTION_PROMPT,  # New: TTFT-optimized version
    MULTI_DIMENSIONAL_ANALYSIS_PROMPT,
    DOCUMENT_ANALYSIS_PROMPT,
    DOCUMENT_QA_PROMPT,
    KB_ENHANCED_ANALYSIS_PROMPT,
    # Builder Functions
    build_scenario_detection_prompt,
    build_analysis_prompt,
    build_document_analysis_prompt,
    build_document_qa_prompt,
    build_kb_enhanced_prompt,
    # Scenario Metadata Accessors
    get_scenario_types_description,
    get_scenario_codes,
    get_scenario_keywords,
    get_analysis_dimensions,
    get_expert_template,
    get_scenario_metadata,  # New: Complete metadata accessor
    get_retrieval_strategy,  # New: Manus-style retrieval strategy
    get_tool_affinity,  # New: Action space management
    get_confidence_threshold,  # New: Classification thresholds
    # Scenario Detection Helpers
    detect_scenario_by_keywords,  # New: Fast keyword-based detection
    validate_scenario_detection,  # New: LLM result validation
    get_all_scenario_types,  # New: Full scenario dictionary
    list_scenario_codes,  # New: Valid scenario codes list
)
# Manus-style modular prompts (v2) - Core system prompt
from .system_prompt_v2 import (
    # Core sections (static, high KV-cache potential)
    AGENT_IDENTITY,
    AGENT_CORE_BEHAVIOR,
    AGENTIC_WORKFLOW,
    AGENT_LOOP,  # Backward compatibility alias for AGENTIC_WORKFLOW
    ANTI_HALLUCINATION,
    ERROR_RECOVERY,
    CONTEXT_MANAGEMENT,
    # Advanced capability sections
    PARALLEL_TOOL_CALLING,
    THINKING_GUIDANCE,
    STATE_TRACKING,
    # Output and system capability
    OUTPUT_RULES,
    SYSTEM_CAPABILITY_TEMPLATE,
    DEFAULT_TOOL_DESCRIPTIONS,
    # Context injection templates
    KB_CONTEXT_TEMPLATE,
    WEB_CONTEXT_TEMPLATE,
    DOCUMENT_CONTEXT_TEMPLATE,
    USER_PREFERENCES_TEMPLATE,
    CONVERSATION_HISTORY_TEMPLATE,
    # Builder functions
    build_system_prompt_v2,
    build_scenario_aware_prompt,
    # Context injection functions
    inject_kb_context,
    inject_web_context,
    inject_document_context,
    inject_user_preferences,
    inject_conversation_history,
    inject_all_context,
    # Convenience functions
    get_default_system_prompt,
    get_minimal_system_prompt,
    get_tool_focused_system_prompt,
    get_agentic_system_prompt,
    get_document_analysis_prompt,
    # Utility functions
    estimate_prompt_tokens,
    get_prompt_size_info,
    # TTFT optimization functions
    get_ttft_optimized_prompt,
    get_cache_stable_prompt_hash,
)
from .guardrails import (
    GUARDRAILS,
    CUSTOMER_SERVICE_GUARDRAILS,
    SALES_CONSULTATION_GUARDRAILS,
    TECHNICAL_SUPPORT_GUARDRAILS,
    PRODUCT_INQUIRY_GUARDRAILS,
    POLICY_INQUIRY_GUARDRAILS,
    DATA_ANALYSIS_GUARDRAILS,
    GENERAL_INQUIRY_GUARDRAILS,
    SCENARIO_GUARDRAILS_MAP,
    get_guardrails,
    get_minimal_guardrails,
    get_anti_hallucination_guardrails,
    get_guardrails_for_scenario,
)
from .agent_freedom import (
    AGENT_FREEDOM,
    COMMUNICATION_STYLE,
    CUSTOMER_SERVICE_FREEDOM,
    SALES_CONSULTATION_FREEDOM,
    TECHNICAL_SUPPORT_FREEDOM,
    PRODUCT_INQUIRY_FREEDOM,
    POLICY_INQUIRY_FREEDOM,
    DATA_ANALYSIS_FREEDOM,
    GENERAL_INQUIRY_FREEDOM,
    SCENARIO_FREEDOM_MAP,
    get_agent_freedom,
    get_minimal_agent_freedom,
    get_agentic_freedom,
    get_freedom_for_scenario,
)

__all__ = [
    # Generation Prompts
    "DOCUMENT_GENERATION_SYSTEM_PROMPT",
    "OUTLINE_GENERATION_PROMPT",
    "SECTION_GENERATION_PROMPT",
    "REPAIR_PROMPT",
    "PRESENTATION_GENERATION_PROMPT",
    "REPORT_GENERATION_PROMPT",
    "EMAIL_GENERATION_PROMPT",
    "SUMMARY_GENERATION_PROMPT",
    "build_generation_prompt",
    "build_outline_prompt",
    "build_section_prompt",
    "build_repair_prompt",
    "build_presentation_prompt",
    "build_report_prompt",
    "build_email_prompt",
    "build_summary_prompt",
    # Planning Prompts
    "INTENT_TYPES",
    "COMPLEXITY_LEVELS",
    "TASK_PLANNING_SYSTEM_PROMPT",
    "INTENT_ANALYSIS_PROMPT",
    "AGENTIC_TASK_DECOMPOSITION_PROMPT",
    "TODO_TRACKING_PROMPT",
    "TASK_REFINEMENT_PROMPT",
    "EXECUTION_REFLECTION_PROMPT",
    "get_intent_types_description",
    "get_intent_type_info",
    "get_complexity_info",
    "build_planning_prompt",
    "build_intent_analysis_prompt",
    "build_task_decomposition_prompt",
    "build_todo_tracking_prompt",
    "build_task_refinement_prompt",
    "build_execution_reflection_prompt",
    # Scenario Analysis Prompts - Constants
    "SCENARIO_TYPES",
    "EXPERT_TEMPLATES",
    # Scenario Analysis Prompts - Templates
    "SCENARIO_DETECTION_PROMPT",
    "FAST_SCENARIO_DETECTION_PROMPT",
    "MULTI_DIMENSIONAL_ANALYSIS_PROMPT",
    "DOCUMENT_ANALYSIS_PROMPT",
    "DOCUMENT_QA_PROMPT",
    "KB_ENHANCED_ANALYSIS_PROMPT",
    # Scenario Analysis Prompts - Builder Functions
    "build_scenario_detection_prompt",
    "build_analysis_prompt",
    "build_document_analysis_prompt",
    "build_document_qa_prompt",
    "build_kb_enhanced_prompt",
    # Scenario Analysis Prompts - Metadata Accessors
    "get_scenario_types_description",
    "get_scenario_codes",
    "get_scenario_keywords",
    "get_analysis_dimensions",
    "get_expert_template",
    "get_scenario_metadata",
    "get_retrieval_strategy",
    "get_tool_affinity",
    "get_confidence_threshold",
    # Scenario Analysis Prompts - Detection Helpers
    "detect_scenario_by_keywords",
    "validate_scenario_detection",
    "get_all_scenario_types",
    "list_scenario_codes",
    # Manus-style System Prompt v2 - Core sections
    "AGENT_IDENTITY",
    "AGENT_CORE_BEHAVIOR",
    "AGENTIC_WORKFLOW",
    "AGENT_LOOP",  # Backward compatibility alias
    "ANTI_HALLUCINATION",
    "ERROR_RECOVERY",
    "CONTEXT_MANAGEMENT",
    # Advanced capability sections
    "PARALLEL_TOOL_CALLING",
    "THINKING_GUIDANCE",
    "STATE_TRACKING",
    # Output and templates
    "OUTPUT_RULES",
    "SYSTEM_CAPABILITY_TEMPLATE",
    "DEFAULT_TOOL_DESCRIPTIONS",
    "KB_CONTEXT_TEMPLATE",
    "WEB_CONTEXT_TEMPLATE",
    "DOCUMENT_CONTEXT_TEMPLATE",
    "USER_PREFERENCES_TEMPLATE",
    "CONVERSATION_HISTORY_TEMPLATE",
    # Builder functions
    "build_system_prompt_v2",
    "build_scenario_aware_prompt",
    # Context injection functions
    "inject_kb_context",
    "inject_web_context",
    "inject_document_context",
    "inject_user_preferences",
    "inject_conversation_history",
    "inject_all_context",
    # Convenience functions
    "get_default_system_prompt",
    "get_minimal_system_prompt",
    "get_tool_focused_system_prompt",
    "get_agentic_system_prompt",
    "get_document_analysis_prompt",
    # Utility functions
    "estimate_prompt_tokens",
    "get_prompt_size_info",
    # TTFT optimization functions
    "get_ttft_optimized_prompt",
    "get_cache_stable_prompt_hash",
    # Guardrails
    "GUARDRAILS",
    "CUSTOMER_SERVICE_GUARDRAILS",
    "SALES_CONSULTATION_GUARDRAILS",
    "TECHNICAL_SUPPORT_GUARDRAILS",
    "PRODUCT_INQUIRY_GUARDRAILS",
    "POLICY_INQUIRY_GUARDRAILS",
    "DATA_ANALYSIS_GUARDRAILS",
    "GENERAL_INQUIRY_GUARDRAILS",
    "SCENARIO_GUARDRAILS_MAP",
    "get_guardrails",
    "get_minimal_guardrails",
    "get_anti_hallucination_guardrails",
    "get_guardrails_for_scenario",
    # Agent Freedom
    "AGENT_FREEDOM",
    "COMMUNICATION_STYLE",
    "CUSTOMER_SERVICE_FREEDOM",
    "SALES_CONSULTATION_FREEDOM",
    "TECHNICAL_SUPPORT_FREEDOM",
    "PRODUCT_INQUIRY_FREEDOM",
    "POLICY_INQUIRY_FREEDOM",
    "DATA_ANALYSIS_FREEDOM",
    "GENERAL_INQUIRY_FREEDOM",
    "SCENARIO_FREEDOM_MAP",
    "get_agent_freedom",
    "get_minimal_agent_freedom",
    "get_agentic_freedom",
    "get_freedom_for_scenario",
]
