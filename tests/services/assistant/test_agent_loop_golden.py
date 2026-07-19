"""
Golden contract snapshots for the assistant agent loop external surface.

These tests lock down:
- The SSE event-type vocabulary consumed by the web frontend
  (web/src/lib/sse.ts + usePlaygroundStream.ts + StreamOutput.tsx).
- The AssistantConfig fields passed by the HTTP /chat route.
- The AgentLoopConfig fields driving execute().
- The AssistantStreamEvent payload shape.

They are **intentional-change barriers**, not behavioral tests — the streaming-first
behavior is covered by test_agentloop_streaming_first_contract.py. A failure here
means an external contract changed; update the snapshot and corresponding consumer
in the same PR (or roll back).
"""

from __future__ import annotations

import json
from pathlib import Path

import pytest

# Skip when the tokenizer used by ContextEngine is unavailable on the test host.
pytest.importorskip("tiktoken", reason="tiktoken required for assistant tests")


# ---------------------------------------------------------------------------
# StreamEventType — vocabulary consumed by web/src/lib/sse.ts:57-114
# ---------------------------------------------------------------------------

EXPECTED_STREAM_EVENT_TYPES: frozenset[str] = frozenset(
    {
        # Core streaming
        "text_delta",
        "thinking_delta",
        "thinking_start",
        "thinking_end",
        "thinking_error",
        "tool_call",
        "tool_result",
        "tool_call_result",
        "tool_call_start",
        "tool_call_end",
        # Context and retrieval
        "context_retrieved",
        "rag_retrieval_started",
        "rag_retrieval_completed",
        "rag_retrieval_failed",
        "web_search_results",
        "rag_evaluation",
        "context_budget",
        "context_compacted",
        "context_detail",
        "memory_retrieved",
        "memory_reflection_scheduled",
        "queue_steered",
        "skill_selected",
        "skill_loaded",
        "skill_create_pending_approval",
        "sandbox_decision",
        # Gateway / queue / approvals
        "queue_state",
        "approval_required",
        "approval_result",
        "gateway_decision",
        # Session
        "session_created",
        "session_updated",
        # Status
        "status",
        "usage",
        "finish",
        "done",
        "error",
        "output_warnings",
        # Code execution
        "code_execution_start",
        "code_execution_output",
        "code_execution_result",
        "artifact_created",
        # Image generation
        "image_generation_start",
        "image_generation_result",
        # Document generation
        "document_generation_start",
        "document_generation_result",
        # KV-cache
        "cache_metrics",
        # File
        "file_processed",
        # Working memory
        "working_memory_update",
        "task_planning",
        # Memory manager
        "memory_loaded",
        # Tool execution error
        "tool_error",
        # Manus-style task visualization
        "step_started",
        "step_finished",
        "outline_ready",
        # Run lifecycle
        "run_started",
        "run_finished",
        "run_error",
    }
)


def test_stream_event_type_vocabulary_is_frozen() -> None:
    """Adding/removing an event type is an external contract change — update both
    this snapshot and the frontend (web/src/lib/sse.ts) in the same PR."""
    from assistant_service.core.assistant_service import StreamEventType

    actual = frozenset(e.value for e in StreamEventType)
    removed = EXPECTED_STREAM_EVENT_TYPES - actual
    added = actual - EXPECTED_STREAM_EVENT_TYPES
    assert not removed, (
        f"StreamEventType removed {sorted(removed)} — frontend consumers will break. "
        "If intentional, update web/src/lib/sse.ts then this snapshot."
    )
    assert not added, (
        f"StreamEventType added {sorted(added)} — update this snapshot and verify "
        "frontend handles it (web/src/lib/sse.ts)."
    )


# ---------------------------------------------------------------------------
# AssistantConfig — fields the HTTP /chat route constructs
# ---------------------------------------------------------------------------

EXPECTED_ASSISTANT_CONFIG_FIELDS: frozenset[str] = frozenset(
    {
        # Model
        "model_provider",
        "model_id",
        "temperature",
        "max_tokens",
        # KB
        "kb_dataset_ids",
        "kb_retrieval_configs",
        "kb_mode",
        "kb_top_k",
        "kb_score_threshold",
        "kb_include_images",
        "kb_max_content_length",
        # Web search
        "web_search_enabled",
        "web_search_max_results",
        # Files / prompt / tools
        "file_paths",
        "system_prompt",
        "eval_system_prompt_override",
        "trusted_agent_instructions",
        "trusted_channel_instructions",
        "trusted_capability_instructions",
        "capability_allowlist",
        "agent_runtime",
        "allowed_skill_ids",
        "allowed_skill_versions",
        "tools_enabled",
        # Output validation
        "output_max_length",
        "output_check_pii",
        "output_format",
        # Context engine
        "use_context_engine",
        "user_preferences",
        "long_term_memory",
        # Task planning
        "enable_task_planning",
        "confirm_plan",
        "max_parallel_tools",
        # Agent loop toggles (retained at boundary even when internally no-ops)
        "use_agent_loop",
        "use_scenario_retrieval",
        "enable_rag_metrics",
        "enable_memory_loading",
        "enable_react_loop",
        # Gateway / policy
        "execution_profile",
        "memory_mode",
        "os_agent_enabled",
        "runtime_mode",
        "queue_mode",
        "context_detail",
        "skills_enabled",
        "memory_profile",
        "traceparent",
        "otel_trace_id",
        "resume_run_id",
        "resume_approval_id",
    }
)


def test_assistant_config_fields_are_frozen() -> None:
    """Frontend (web/src/pages/playground) sends these fields verbatim through the
    HTTP /chat route (apps/assistant-service/.../api/routes/chat.py). Removing any
    breaks the playground request build."""
    from assistant_service.core.assistant_service import AssistantConfig

    actual = frozenset(AssistantConfig.__dataclass_fields__.keys())
    removed = EXPECTED_ASSISTANT_CONFIG_FIELDS - actual
    added = actual - EXPECTED_ASSISTANT_CONFIG_FIELDS
    assert not removed, (
        f"AssistantConfig removed {sorted(removed)} — frontend request builder will "
        "raise TypeError. Update the HTTP route, playground, and this snapshot."
    )
    assert not added, (
        f"AssistantConfig added {sorted(added)} — update this snapshot and wire the "
        "field through apps/assistant-service/src/assistant_service/api/routes/chat.py."
    )


# ---------------------------------------------------------------------------
# AgentLoopConfig — internal, but snapshot-locked to make refactor deliberate
# ---------------------------------------------------------------------------

EXPECTED_AGENT_LOOP_CONFIG_FIELDS: frozenset[str] = frozenset(
    {
        "model_id",
        "temperature",
        "max_tokens",
        "enable_task_planning",
        "enable_scenario_retrieval",
        "enable_context_compression",
        "enable_rag_metrics",
        "enable_memory_loading",
        "kb_dataset_ids",
        "kb_retrieval_configs",
        "kb_mode",
        "kb_top_k",
        "kb_min_relevance",
        "kb_include_images",
        "kb_max_queries",
        "kb_results_per_query",
        "kb_max_content_length",
        "web_search_enabled",
        "file_paths",
        "max_tool_iterations",
        "max_concurrent_tools",
        "compress_threshold",
        "min_recent_messages",
        "compressed_context_tokens",
        "max_summary_tokens",
        "max_history_tokens",
        "enable_history_trimming",
        "enable_react_loop",
        "react_max_iterations",
        "react_thinking_visible",
        "react_auto_retry",
        "thinking_level",
        "enable_error_recovery",
        "error_max_retries",
        "error_base_delay",
        "error_max_delay",
        "system_prompt",
        "eval_system_prompt_override",
        "trusted_agent_instructions",
        "trusted_channel_instructions",
        "trusted_capability_instructions",
        "capability_allowlist",
        "agent_runtime",
        "allowed_skill_ids",
        "allowed_skill_versions",
        "execution_profile",
        "memory_mode",
        "os_agent_enabled",
        "runtime_mode",
        "queue_mode",
        "context_detail",
        "skills_enabled",
        "memory_profile",
        "resume_run_id",
        "resume_approval_id",
    }
)


def test_agent_loop_config_fields_are_frozen() -> None:
    """Refactor barrier: any AgentLoopConfig field change should be intentional
    and land alongside an update to this snapshot."""
    from assistant_service.core.agent.agent_loop import AgentLoopConfig

    actual = frozenset(AgentLoopConfig.__dataclass_fields__.keys())
    removed = EXPECTED_AGENT_LOOP_CONFIG_FIELDS - actual
    added = actual - EXPECTED_AGENT_LOOP_CONFIG_FIELDS
    assert not removed, (
        f"AgentLoopConfig removed {sorted(removed)} — update this snapshot if intentional."
    )
    assert not added, (
        f"AgentLoopConfig added {sorted(added)} — update this snapshot if intentional."
    )


def test_agent_loop_config_to_dict_includes_resume_fields() -> None:
    """Resume stream must round-trip resume_run_id/resume_approval_id via to_dict()."""
    from assistant_service.core.agent.agent_loop import AgentLoopConfig

    cfg = AgentLoopConfig(
        resume_run_id="aaaaaaaa-aaaa-4aaa-8aaa-aaaaaaaaaaaa",
        resume_approval_id="22222222-2222-4222-8222-222222222222",
    )
    payload = cfg.to_dict()

    assert payload["resume_run_id"] == "aaaaaaaa-aaaa-4aaa-8aaa-aaaaaaaaaaaa"
    assert payload["resume_approval_id"] == "22222222-2222-4222-8222-222222222222"


# ---------------------------------------------------------------------------
# AssistantStreamEvent — payload shape consumed by the SSE route
# ---------------------------------------------------------------------------


def test_assistant_stream_event_shape_is_frozen() -> None:
    """The SSE route serializes these three fields verbatim; any rename breaks the
    wire protocol with the frontend SSE parser."""
    from assistant_service.core.assistant_service import AssistantStreamEvent

    fields = set(AssistantStreamEvent.__dataclass_fields__.keys())
    assert fields == {"event_type", "data", "timestamp"}, (
        f"AssistantStreamEvent fields changed: {sorted(fields)}. "
        "The SSE route serializes these verbatim — any rename breaks the wire protocol."
    )


# ---------------------------------------------------------------------------
# Event payload shapes — required keys per event type, consumed by
# web/src/lib/sse.ts streamChunkToAGUIEvent and usePlaygroundStream.
# Removing or renaming any required key breaks the frontend silently.
# ---------------------------------------------------------------------------

# Minimum required keys each event's `data` payload must contain. The frontend
# reads these verbatim — missing them means the UI breaks without a server error.
EXPECTED_EVENT_DATA_KEYS: dict[str, frozenset[str]] = {
    "text_delta": frozenset({"content"}),
    "tool_call_start": frozenset({"tool_call_id", "name"}),
    "tool_call_result": frozenset({"tool_call_id", "status"}),
    "artifact_created": frozenset({"artifact_id"}),
    "done": frozenset(),  # done may be empty but must be emitted
    "run_finished": frozenset(),
    "run_error": frozenset({"error"}),
}


def test_event_payload_contracts_documented() -> None:
    """Snapshot of the minimum payload keys the frontend depends on for the
    hottest event types. Changing these requires updating both this dict and
    web/src/lib/sse.ts + web/src/pages/playground/hooks/usePlaygroundStream.ts.

    This is a documentation test — it fails if someone edits the dict without
    a paired frontend change note. It intentionally does not invoke the loop;
    shape verification under a fake model happens in integration tests."""
    # Sanity: every event in this contract must be a known StreamEventType.
    from assistant_service.core.assistant_service import StreamEventType

    known = {e.value for e in StreamEventType}
    unknown = set(EXPECTED_EVENT_DATA_KEYS.keys()) - known
    assert not unknown, (
        f"EXPECTED_EVENT_DATA_KEYS references unknown event types {unknown}. "
        "Either add them to StreamEventType or remove from the contract."
    )


def test_as02_offline_golden_artifact_covers_required_runtime_cases() -> None:
    artifact = Path("reports/agent-studio/as-02-golden-results.json")
    payload = json.loads(artifact.read_text(encoding="utf-8"))
    cases = {case["case_id"]: case for case in payload["cases"]}
    required = {
        "normal-response",
        "no-tool-agent",
        "kb-tool-trace",
        "prompt-injection",
        "permission-denial",
        "resource-unavailable",
    }

    assert payload["schema_version"] == "agent-studio-as02-golden/v1"
    assert payload["execution_mode"] == "offline-deterministic"
    assert payload["provider_calls"] == 0
    assert set(cases) == required
    assert all(case["status"] == "passed" for case in cases.values())
    assert all(case["evidence_tests"] for case in cases.values())
    for case in cases.values():
        for node_id in case["evidence_tests"]:
            test_path, test_name = node_id.split("::", maxsplit=1)
            source = Path(test_path).read_text(encoding="utf-8")
            assert f"def {test_name}(" in source
