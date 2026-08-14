"""Public data models for the Assistant service.

Keeping these contracts separate from orchestration makes the service module
smaller without changing its import surface; ``assistant_service`` re-exports
the names for compatibility.
"""

from __future__ import annotations

import time
from dataclasses import dataclass, field
from datetime import datetime
from enum import Enum
from typing import Any

from ai_gateway_core.enums import RAGMode

from .agent.runtime_context import AgentRuntimeExecutionContext
from .content.structured_output import OutputFormat
from .models.model_registry import ModelProvider
from .tool_invoker import CapabilityAllowlist


class StreamEventType(str, Enum):
    """SSE event types for assistant streaming responses."""

    TEXT_DELTA = "text_delta"
    THINKING_DELTA = "thinking_delta"
    THINKING_START = "thinking_start"
    THINKING_END = "thinking_end"
    THINKING_ERROR = "thinking_error"
    TOOL_CALL = "tool_call"
    TOOL_RESULT = "tool_result"
    TOOL_CALL_RESULT = "tool_call_result"
    TOOL_CALL_START = "tool_call_start"
    TOOL_CALL_END = "tool_call_end"

    CONTEXT_RETRIEVED = "context_retrieved"
    RAG_RETRIEVAL_STARTED = "rag_retrieval_started"
    RAG_RETRIEVAL_COMPLETED = "rag_retrieval_completed"
    RAG_RETRIEVAL_FAILED = "rag_retrieval_failed"
    WEB_SEARCH_RESULTS = "web_search_results"
    RAG_EVALUATION = "rag_evaluation"
    CONTEXT_BUDGET = "context_budget"
    CONTEXT_COMPACTED = "context_compacted"
    CONTEXT_DETAIL = "context_detail"
    MEMORY_RETRIEVED = "memory_retrieved"
    MEMORY_REFLECTION_SCHEDULED = "memory_reflection_scheduled"
    QUEUE_STEERED = "queue_steered"
    SKILL_SELECTED = "skill_selected"
    SKILL_LOADED = "skill_loaded"
    SKILL_CREATE_PENDING_APPROVAL = "skill_create_pending_approval"
    SANDBOX_DECISION = "sandbox_decision"

    QUEUE_STATE = "queue_state"
    APPROVAL_REQUIRED = "approval_required"
    APPROVAL_RESULT = "approval_result"
    GATEWAY_DECISION = "gateway_decision"

    SESSION_CREATED = "session_created"
    SESSION_UPDATED = "session_updated"

    STATUS = "status"
    USAGE = "usage"
    FINISH = "finish"
    DONE = "done"
    ERROR = "error"
    OUTPUT_WARNINGS = "output_warnings"

    CODE_EXECUTION_START = "code_execution_start"
    CODE_EXECUTION_OUTPUT = "code_execution_output"
    CODE_EXECUTION_RESULT = "code_execution_result"
    ARTIFACT_CREATED = "artifact_created"

    IMAGE_GENERATION_START = "image_generation_start"
    IMAGE_GENERATION_RESULT = "image_generation_result"
    DOCUMENT_GENERATION_START = "document_generation_start"
    DOCUMENT_GENERATION_RESULT = "document_generation_result"
    CACHE_METRICS = "cache_metrics"
    FILE_PROCESSED = "file_processed"
    WORKING_MEMORY_UPDATE = "working_memory_update"
    TASK_PLANNING = "task_planning"
    MEMORY_LOADED = "memory_loaded"
    TOOL_ERROR = "tool_error"
    STEP_STARTED = "step_started"
    STEP_FINISHED = "step_finished"
    OUTLINE_READY = "outline_ready"
    RUN_STARTED = "run_started"
    RUN_FINISHED = "run_finished"
    RUN_ERROR = "run_error"
    RUN_BUDGET_EXCEEDED = "run_budget_exceeded"


@dataclass
class AssistantConfig:
    """Configuration for an assistant conversation."""

    model_provider: ModelProvider = ModelProvider.DASHSCOPE
    model_id: str = "qwen3.7-plus"
    # Verified Agent Runtime only. This is the exact control-plane provider
    # identity; ``model_provider`` is merely the runtime protocol family.
    model_provider_id: str | None = field(default=None, repr=False)
    temperature: float = 0.7
    max_tokens: int | None = None

    kb_dataset_ids: list[str] = field(default_factory=list)
    kb_retrieval_configs: dict[str, dict[str, Any]] = field(default_factory=dict)
    kb_mode: RAGMode = RAGMode.AUTO
    kb_top_k: int = 5
    kb_score_threshold: float = 0.65
    kb_include_images: bool = False
    kb_max_content_length: int = 400

    web_search_enabled: bool = False
    web_search_max_results: int = 5
    file_paths: list[str] = field(default_factory=list)

    system_prompt: str | None = None
    eval_system_prompt_override: str | None = None
    trusted_agent_instructions: str | None = None
    trusted_channel_instructions: str | None = None
    trusted_capability_instructions: str | None = None

    capability_allowlist: CapabilityAllowlist | None = None
    agent_runtime: AgentRuntimeExecutionContext | None = None
    allowed_skill_ids: frozenset[str] | None = None
    allowed_skill_versions: dict[str, str] | None = None
    tools_enabled: list[str] = field(default_factory=list)

    output_max_length: int = 10000
    output_check_pii: bool = True
    output_format: OutputFormat = OutputFormat.TEXT

    use_context_engine: bool = True
    user_preferences: str | None = None
    long_term_memory: str | None = None

    enable_task_planning: bool = False
    confirm_plan: bool = False
    max_parallel_tools: int = 5

    use_agent_loop: bool = True
    use_scenario_retrieval: bool = False
    enable_rag_metrics: bool = False
    enable_memory_loading: bool = False
    enable_react_loop: bool = False
    thinking_level: str | None = None

    execution_profile: str = "safe"
    memory_mode: str = "auto"
    os_agent_enabled: bool = False
    # Selectors are not authority. The Local Node provider rechecks ownership,
    # session scope, device health, and every cited grant before exposing tools.
    local_node_device_id: str | None = None
    local_node_grant_ids: list[str] = field(default_factory=list)
    runtime_mode: str = "compat"
    queue_mode: str = "collect"
    context_detail: bool = False
    skills_enabled: bool | None = None
    memory_profile: str | None = None

    traceparent: str | None = None
    otel_trace_id: str | None = None
    resume_run_id: str | None = None
    resume_approval_id: str | None = None


@dataclass
class AssistantStreamEvent:
    """Event emitted during streaming."""

    event_type: str
    data: Any = None
    timestamp: float = field(default_factory=time.time)


@dataclass
class RetrievedContext:
    """Context retrieved from knowledge bases."""

    dataset_id: str
    dataset_name: str
    chunks: list[dict[str, Any]]
    query: str
    took_ms: float
    avg_score: float = 0.0
    top_score: float = 0.0


@dataclass
class ToolErrorInfo:
    """Structured tool failure context retained for model recovery."""

    tool_name: str
    tool_call_id: str
    error_type: str
    error_message: str
    arguments: dict[str, Any] = field(default_factory=dict)
    suggestion: str | None = None
    timestamp: datetime = field(default_factory=datetime.utcnow)

    def to_rich_context(self) -> str:
        lines = [
            f"[TOOL ERROR] {self.tool_name} failed",
            f"Error Type: {self.error_type}",
            f"Error Message: {self.error_message}",
        ]
        if self.arguments:
            args_text = ", ".join(
                f"{key}={repr(value)[:100]}" for key, value in self.arguments.items()
            )
            lines.append(f"Arguments: {args_text}")
        if self.suggestion:
            lines.append(f"Suggestion: {self.suggestion}")
        return "\n".join(lines)


__all__ = [
    "AssistantConfig",
    "AssistantStreamEvent",
    "RetrievedContext",
    "StreamEventType",
    "ToolErrorInfo",
]
