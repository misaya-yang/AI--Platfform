"""
System prompts for the general enterprise Assistant.

The production system prompt is built by ``build_system_prompt_v2`` in
``system_prompt_v2``: one small stable policy plus only the capabilities
available for the current request. Authorization, approvals, tool schema
validation, and side-effect controls are runtime responsibilities, not
prompt text.

``generation_prompts`` is the remaining specialized surface, with a live
caller in ``core/agent``.

The ``agent_freedom`` and ``planning_prompts`` modules were removed because
every symbol they exported was unreferenced outside this package. The legacy
"guardrails + freedom" prompt-stack they belonged to was superseded by the
compact builder above.
"""

from .generation_prompts import (
    DOCUMENT_GENERATION_SYSTEM_PROMPT,
    EMAIL_GENERATION_PROMPT,
    OUTLINE_GENERATION_PROMPT,
    PRESENTATION_GENERATION_PROMPT,
    REPAIR_PROMPT,
    REPORT_GENERATION_PROMPT,
    SECTION_GENERATION_PROMPT,
    SUMMARY_GENERATION_PROMPT,
    build_email_prompt,
    build_generation_prompt,
    build_outline_prompt,
    build_presentation_prompt,
    build_repair_prompt,
    build_report_prompt,
    build_section_prompt,
    build_summary_prompt,
)
from .guardrails import (
    CUSTOMER_SERVICE_GUARDRAILS,
    DATA_ANALYSIS_GUARDRAILS,
    GENERAL_INQUIRY_GUARDRAILS,
    GUARDRAILS,
    POLICY_INQUIRY_GUARDRAILS,
    PRODUCT_INQUIRY_GUARDRAILS,
    SALES_CONSULTATION_GUARDRAILS,
    SCENARIO_GUARDRAILS_MAP,
    TECHNICAL_SUPPORT_GUARDRAILS,
    get_anti_hallucination_guardrails,
    get_guardrails,
    get_guardrails_for_scenario,
    get_minimal_guardrails,
)

# Manus-style modular prompts (v2) - Core system prompt
from .system_prompt_v2 import (
    AGENT_CORE_BEHAVIOR,
    # Core sections (static, high KV-cache potential)
    AGENT_IDENTITY,
    AGENT_LOOP,  # Backward compatibility alias for AGENTIC_WORKFLOW
    AGENTIC_WORKFLOW,
    ANTI_HALLUCINATION,
    CONTEXT_MANAGEMENT,
    CONVERSATION_HISTORY_TEMPLATE,
    DEFAULT_TOOL_DESCRIPTIONS,
    DOCUMENT_CONTEXT_TEMPLATE,
    ERROR_RECOVERY,
    # Context injection templates
    KB_CONTEXT_TEMPLATE,
    # Output and system capability
    OUTPUT_RULES,
    # Advanced capability sections
    PARALLEL_TOOL_CALLING,
    STATE_TRACKING,
    SYSTEM_CAPABILITY_TEMPLATE,
    THINKING_GUIDANCE,
    USER_PREFERENCES_TEMPLATE,
    WEB_CONTEXT_TEMPLATE,
    build_scenario_aware_prompt,
    # Builder functions
    build_system_prompt_v2,
    # Utility functions
    estimate_prompt_tokens,
    get_agentic_system_prompt,
    get_cache_stable_prompt_hash,
    # Convenience functions
    get_default_system_prompt,
    get_document_analysis_prompt,
    get_minimal_system_prompt,
    get_prompt_size_info,
    get_tool_focused_system_prompt,
    # TTFT optimization functions
    get_ttft_optimized_prompt,
    inject_all_context,
    inject_conversation_history,
    inject_document_context,
    # Context injection functions
    inject_kb_context,
    inject_user_preferences,
    inject_web_context,
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
]
