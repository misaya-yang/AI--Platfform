"""Explicit lifecycle state exchanged by streaming execution stages."""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any

from .tool_dedup import KBDedupState


@dataclass(slots=True)
class StreamingPreparationState:
    """Context and accumulators produced before the model/tool loop."""

    terminal: bool = False
    messages: list[dict[str, Any]] = field(default_factory=list)
    contexts_for_persistence: list[dict[str, Any]] = field(default_factory=list)
    web_search_results_for_persistence: dict[str, Any] | None = None
    quiz_id_for_persistence: str | None = None
    created_artifact_ids: list[str] = field(default_factory=list)
    turn_thinking_content: str = ""
    turn_tool_calls: list[dict[str, Any]] = field(default_factory=list)
    turn_tool_results: list[dict[str, Any]] = field(default_factory=list)
    sanitize_output_files: Any = None
    split_text_for_stream: Any = None
    compact_context_payload: Any = None
    compact_tool_result_for_model: Any = None
    kb_query_fingerprint: Any = None
    provider_name: str = ""
    available_tool_names: list[str] = field(default_factory=list)
    dataset_name_map: dict[str, str] = field(default_factory=dict)
    started_at: float = 0.0
    tools: list[dict[str, Any]] = field(default_factory=list)
    model_info: Any = None
    model_supports_vision: bool = False
    processed_files: Any = None
    available_tool_schema_hash: str = ""
    planning_context: str = ""
    rag_revision_hash: str = ""
    knowledge_provenance: dict[str, Any] = field(default_factory=dict)
    auto_knowledge_context: str = ""
    system_prompt: str = ""
    candidate_system_prompt_hash: str = ""
    dynamic_context_block: str = ""
    trimmed_history: list[dict[str, Any]] = field(default_factory=list)
    injected_file_sources: list[dict[str, Any]] = field(default_factory=list)


@dataclass(slots=True)
class StreamingLoopResult:
    """Terminal state produced by the model/tool iteration stage."""

    terminal: bool = False
    iteration: int = 0
    last_tool_failed: bool = False
    max_iterations: int = 0
    model_terminated_cleanly: bool = False
    quiz_id_for_persistence: str | None = None
    turn_thinking_content: str = ""


@dataclass(slots=True)
class StreamingToolLoopState:
    """Mutable state shared by model-turn and per-tool processing helpers."""

    messages: list[dict[str, Any]]
    contexts_for_persistence: list[dict[str, Any]]
    created_artifact_ids: list[str]
    turn_tool_calls: list[dict[str, Any]]
    turn_tool_results: list[dict[str, Any]]
    sanitize_output_files: Any
    compact_context_payload: Any
    compact_tool_result_for_model: Any
    kb_query_fingerprint: Any
    available_tool_names: list[str]
    dataset_name_map: dict[str, str]
    provider_name: str
    started_at: float
    tools: list[dict[str, Any]]
    quiz_id_for_persistence: str | None
    turn_thinking_content: str
    initial_iteration_lease: int
    max_iterations: int
    first_token_emitted: bool
    iteration: int = 0
    kb_dedup: KBDedupState = field(default_factory=KBDedupState)
    denied_tools: set[str] = field(default_factory=set)
    last_tool_failed: bool = False
    model_terminated_cleanly: bool = False
    progress_fingerprints: set[str] = field(default_factory=set)
    lease_extensions: int = 0

    @classmethod
    def from_preparation(
        cls,
        prepared: StreamingPreparationState,
        *,
        initial_iteration_lease: int,
        max_iterations: int,
        first_token_emitted: bool,
    ) -> StreamingToolLoopState:
        return cls(
            messages=prepared.messages,
            contexts_for_persistence=prepared.contexts_for_persistence,
            created_artifact_ids=prepared.created_artifact_ids,
            turn_tool_calls=prepared.turn_tool_calls,
            turn_tool_results=prepared.turn_tool_results,
            sanitize_output_files=prepared.sanitize_output_files,
            compact_context_payload=prepared.compact_context_payload,
            compact_tool_result_for_model=prepared.compact_tool_result_for_model,
            kb_query_fingerprint=prepared.kb_query_fingerprint,
            available_tool_names=prepared.available_tool_names,
            dataset_name_map=prepared.dataset_name_map,
            provider_name=prepared.provider_name,
            started_at=prepared.started_at,
            tools=prepared.tools,
            quiz_id_for_persistence=prepared.quiz_id_for_persistence,
            turn_thinking_content=prepared.turn_thinking_content,
            initial_iteration_lease=initial_iteration_lease,
            max_iterations=max_iterations,
            first_token_emitted=first_token_emitted,
        )


@dataclass(slots=True)
class StreamingToolCallState:
    """Identity, approval fence, execution outcome, and result for one tool call."""

    tool_index: int
    tool_call: dict[str, Any]
    tool_calls_batch: list[dict[str, Any]]
    subagent_results: dict[str, str] = field(default_factory=dict)
    stop_processing: bool = False
    tool_id: str = ""
    tool_name: str = "unknown"
    tool_log_name: str = "unknown"
    tool_args: dict[str, Any] = field(default_factory=dict)
    turn_call_record: dict[str, Any] = field(default_factory=dict)
    correction_allowed: bool = False
    kb_query_fp: str = ""
    approval_checkpoint: dict[str, Any] | None = None
    dispatch_idempotency: dict[str, Any] = field(default_factory=dict)
    dispatch_resume_payload: dict[str, Any] = field(default_factory=dict)
    pending_recovery_event: dict[str, Any] | None = None
    step_id: str = ""
    step_started_at: float = 0.0
    step_status_override: str | None = None
    step_success: bool | None = None
    step_error: str | None = None
    step_result_preview: str | None = None
    result: Any = None
    short_circuit_kb: bool = False
    tool_success: bool = False
    tool_error: str | None = None
    tool_metadata: dict[str, Any] = field(default_factory=dict)
    tool_output_files: list[dict[str, Any]] = field(default_factory=list)
    tool_result_for_model: str = ""
    tool_duration_ms: float | None = None
    kb_rag_started_at: float | None = None
    kb_rag_query: str = ""
    kb_rag_dataset_ids: list[str] = field(default_factory=list)
    kb_rag_top_k: int = 5
    kb_rag_score_threshold: float = 0.0
    kb_rag_include_images: bool = False
    kb_rag_retrieval_configs: dict[str, dict[str, Any]] | None = None
    title: str = ""
    tool_result: Any = None
    tool_result_text: str = ""
    tool_result_preview: str = ""
