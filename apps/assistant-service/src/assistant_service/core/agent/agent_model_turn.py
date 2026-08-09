"""Provider streaming and terminal synthesis for AgentLoop."""

from __future__ import annotations

import contextlib
import copy
import json
import time
from collections.abc import AsyncGenerator
from typing import Any

from ai_gateway_core.enums import StreamEventType
from ai_gateway_core.logging import get_logger

from ..models.model_registry import should_use_native_search
from ..quality.cache_optimizer import (
    normalize_provider_cache_usage,
)
from ..run_budget import (
    RunBudgetExceeded,
)
from ..runtime.context import (
    ContextPacketOverflowError,
)
from .agent_loop_helpers import (
    _effective_packet_output_tokens,
    _model_turn_finish_is_successful,
    _tool_name_log_label,
)
from .agent_loop_models import (
    AgentLoopContext,
    AgentLoopEvent,
    AgentLoopPhase,
    StreamingModelTurn,
)
from .stream_helpers import merge_stream_tool_calls
from .tool_result_formatter import (
    split_text_for_stream as _fmt_split_text_for_stream,
)
from .tool_result_formatter import (
    tool_schema_name as _fmt_tool_schema_name,
)

logger = get_logger(__name__)


class AgentModelTurnMixin:
    """Internal methods extracted from :class:`AgentLoop` without behavior changes."""

    async def _stream_model_turn(
        self,
        ctx: AgentLoopContext,
        *,
        messages: list[dict[str, Any]],
        tools: list[dict[str, Any]],
        phase: AgentLoopPhase,
        provider_name: str,
        iteration: int,
        started_at: float,
        ttft_start: float,
        denied_tools: set[str],
        kb_search_completed: bool,
        dataset_name_map: dict[str, str] | None,
        result: StreamingModelTurn,
    ) -> AsyncGenerator[AgentLoopEvent, None]:
        if ctx.run_budget is None:
            raise RuntimeError("run_budget_not_initialized")
        ctx.run_budget.consume_model_turn()
        llm_started_at = time.time()
        logger.info(
            "[STREAMING-FIRST] Starting LLM call (iter=%s), total prep: %.0fms",
            iteration,
            (llm_started_at - started_at) * 1000,
        )
        tools_for_call: list[dict[str, Any]] | None = tools or None
        if tools_for_call and kb_search_completed:
            filtered_tools = [
                schema
                for schema in tools_for_call
                if _fmt_tool_schema_name(schema) != "search_knowledge_base"
            ]
            if len(filtered_tools) != len(tools_for_call):
                tools_for_call = filtered_tools
                logger.debug(
                    "[STREAMING-FIRST] Removed search_knowledge_base from remaining "
                    "toolset after first KB completion."
                )

        model_info = self.model_registry.get_model(ctx.config.model_id)
        native_search_config: dict[str, Any] | None = None
        if (
            model_info
            and getattr(model_info, "supports_native_search", False)
            and should_use_native_search(ctx.message)
        ):
            native_search_config = getattr(model_info, "native_search_config", None)

        if tools_for_call and denied_tools:
            tools_for_call = [
                tool
                for tool in tools_for_call
                if (
                    tool.get("function", {}).get("name")
                    if isinstance(tool, dict)
                    else getattr(tool, "name", "")
                )
                not in denied_tools
            ]

        if ctx.config.use_context_engine and ctx.context_packet and ctx.context_assembler:
            effective_tool_names = [_fmt_tool_schema_name(tool) for tool in (tools_for_call or [])]
            boundary_system_prompt, candidate_system_prompt_hash = (
                self._build_streaming_system_prompt(
                    ctx,
                    available_tool_names=effective_tool_names,
                    dataset_name_map=dataset_name_map,
                )
            )
            messages[0] = {**messages[0], "content": boundary_system_prompt}
            rule_revision = dict(ctx.context_cache_dimensions.get("rule_revision") or {})
            rule_revision["candidate_system_prompt_hash"] = candidate_system_prompt_hash
            boundary_dimensions = {
                **ctx.context_cache_dimensions,
                "rule_revision": rule_revision,
            }
            rebound_packet = ctx.context_assembler.bind_model_boundary(
                packet=ctx.context_packet,
                messages=messages,
                tool_definitions=list(tools_for_call or []),
                trusted_system_prompt=boundary_system_prompt,
                cache_dimensions=boundary_dimensions,
                previous_cache_receipt=ctx.context_packet_receipt,
            )
            ctx.context_packet = rebound_packet
            ctx.context_packet_receipt = rebound_packet.receipt()
            ctx.context_cache_dimensions = boundary_dimensions
            messages[:] = rebound_packet.materialize_messages()
            tools_for_call = rebound_packet.materialize_tools()
            ctx.messages = list(messages)
            yield AgentLoopEvent(
                phase=phase,
                event_type=StreamEventType.CONTEXT_BUDGET.value,
                data={
                    "run_id": ctx.run_id,
                    "thread_id": ctx.session_id,
                    "session_id": ctx.session_id,
                    "mode": "model_boundary",
                    "iteration": iteration,
                    "context_packet": ctx.context_packet_receipt,
                },
            )

        await self._save_checkpoint(
            ctx,
            phase="model_turn_started",
            iteration=iteration,
            messages=messages,
            resume_payload={
                "tool_count": len(tools_for_call or []),
                "generated_content_chars": len(ctx.generated_content or ""),
            },
        )

        tool_calls_accumulated: dict[str, dict[str, Any]] = {}
        tool_call_order: list[str] = []
        anonymous_tool_counter = 0
        call_usage: dict[str, int] = {}
        thinking_started = False
        thinking_ended = False
        accumulated_thinking = ""
        async for streamed in self._stream_chat_with_failover(
            ctx,
            phase=phase,
            messages=messages,
            temperature=ctx.config.temperature,
            max_tokens=_effective_packet_output_tokens(
                ctx.context_packet,
                ctx.config.max_tokens,
            ),
            tools=tools_for_call,
            thinking_level=ctx.config.thinking_level,
            native_search_config=native_search_config,
        ):
            if isinstance(streamed, AgentLoopEvent):
                yield streamed
                continue
            delta = streamed
            if delta.thinking_content:
                if not thinking_started:
                    thinking_started = True
                    yield AgentLoopEvent(
                        phase=phase,
                        event_type="thinking_start",
                        data={"model_id": ctx.config.model_id},
                    )
                accumulated_thinking += delta.thinking_content
                result.thinking_content += delta.thinking_content
                yield AgentLoopEvent(
                    phase=phase,
                    event_type="thinking_delta",
                    data=delta.thinking_content,
                )

            if delta.content:
                if thinking_started and not thinking_ended:
                    thinking_ended = True
                    yield AgentLoopEvent(
                        phase=phase,
                        event_type="thinking_end",
                        data={"content": accumulated_thinking},
                    )
                for text_chunk in _fmt_split_text_for_stream(delta.content):
                    result.content += text_chunk
                    ctx.generated_content += text_chunk
                    if not result.first_token_emitted:
                        ttft_ms = (time.time() - ttft_start) * 1000
                        result.first_token_emitted = True
                        logger.info("[STREAMING-FIRST] TTFT: %.0fms", ttft_ms)
                        yield AgentLoopEvent(
                            phase=phase,
                            event_type="ttft",
                            data={"ttft_ms": round(ttft_ms, 2)},
                        )
                    yield AgentLoopEvent(
                        phase=phase,
                        event_type="text_delta",
                        data=text_chunk,
                    )

            if delta.tool_calls:
                anonymous_tool_counter = merge_stream_tool_calls(
                    delta.tool_calls,
                    tool_calls_accumulated,
                    tool_call_order,
                    anonymous_tool_counter,
                )

            if delta.finish_reason:
                result.finish_reason = delta.finish_reason
            if delta.provider_content_blocks is not None:
                result.provider_content_blocks = copy.deepcopy(delta.provider_content_blocks)

            if delta.usage:
                normalized_usage = normalize_provider_cache_usage(
                    delta.usage,
                    provider_name,
                )
                for key, value in normalized_usage.items():
                    if isinstance(value, (int, float)):
                        call_usage[key] = max(call_usage.get(key, 0), int(value))
                    elif value is not None:
                        with contextlib.suppress(Exception):
                            call_usage[key] = int(value)

        for key, value in call_usage.items():
            ctx.usage[key] = int(value)

        if thinking_started and not thinking_ended:
            yield AgentLoopEvent(
                phase=phase,
                event_type="thinking_end",
                data={"content": accumulated_thinking},
            )

        tool_calls = [tool_calls_accumulated[key] for key in tool_call_order]
        if len(tool_calls) > 1:
            seen: set[tuple[str, str]] = set()
            deduped: list[dict[str, Any]] = []
            for tool_call in tool_calls:
                function = tool_call.get("function") or {}
                name = str(function.get("name") or "")
                raw_arguments = function.get("arguments") or ""
                try:
                    parsed = json.loads(raw_arguments) if raw_arguments else {}
                    normalized_arguments = json.dumps(
                        parsed,
                        sort_keys=True,
                        ensure_ascii=False,
                    )
                except (json.JSONDecodeError, ValueError):
                    normalized_arguments = str(raw_arguments)
                key = (name, normalized_arguments)
                if key in seen:
                    allowed_names = {_fmt_tool_schema_name(tool) for tool in (tools_for_call or [])}
                    logger.info(
                        "[STREAMING-FIRST] Dropping duplicate tool call at "
                        "batch-level: name=%s (same name+args as a prior call "
                        "this iteration)",
                        _tool_name_log_label(name, allowed_names),
                    )
                    continue
                seen.add(key)
                deduped.append(tool_call)
            tool_calls = deduped
        result.tool_calls = tool_calls

    async def _run_forced_synthesis(
        self,
        ctx: AgentLoopContext,
        *,
        messages: list[dict[str, Any]],
        phase: AgentLoopPhase,
        provider_name: str,
        ttft_start: float,
        attempt_label: str,
        tool_result_summaries: list[dict[str, Any] | str] | None = None,
        fresh_context: bool = False,
    ) -> AsyncGenerator[AgentLoopEvent, None]:
        first_token_emitted = bool(ctx.generated_content)
        forced_usage: dict[str, int] = {}
        try:
            if ctx.run_budget is None:
                raise RuntimeError("run_budget_not_initialized")
            ctx.run_budget.consume_model_turn()
            synthesis_messages = copy.deepcopy(messages)
            if synthesis_messages:
                no_tools_system_prompt, _ = self._build_streaming_system_prompt(
                    ctx,
                    available_tool_names=[],
                    dataset_name_map={},
                    capabilities_enabled=False,
                )
                synthesis_messages[0] = {
                    **synthesis_messages[0],
                    "content": no_tools_system_prompt,
                }
            model_messages, packet_receipt = self._compile_auxiliary_context_packet(
                ctx,
                messages=synthesis_messages,
                purpose=f"forced_synthesis:{attempt_label}",
                fresh=fresh_context or attempt_label == "compact",
                tool_result_summaries=tool_result_summaries,
            )
            if packet_receipt is not None:
                yield AgentLoopEvent(
                    phase=phase,
                    event_type=StreamEventType.CONTEXT_BUDGET.value,
                    data={
                        "run_id": ctx.run_id,
                        "thread_id": ctx.session_id,
                        "session_id": ctx.session_id,
                        "mode": "forced_synthesis",
                        "attempt": attempt_label,
                        "context_packet": packet_receipt,
                    },
                )
            forced_chunks: list[str] = []
            forced_finish_reason: str | None = None
            async for streamed in self._stream_chat_with_failover(
                ctx,
                phase=phase,
                messages=model_messages,
                temperature=min(ctx.config.temperature, 0.3),
                max_tokens=_effective_packet_output_tokens(
                    ctx.context_packet,
                    min(ctx.config.max_tokens or 2048, 2048),
                ),
                tools=None,
            ):
                if isinstance(streamed, AgentLoopEvent):
                    yield streamed
                    continue
                delta = streamed
                if delta.tool_calls:
                    raise RuntimeError("provider_synthesis_returned_tool_calls")
                if delta.finish_reason is not None:
                    forced_finish_reason = str(delta.finish_reason)
                if delta.content:
                    forced_chunks.extend(_fmt_split_text_for_stream(delta.content))
                if delta.usage:
                    for key, value in normalize_provider_cache_usage(
                        delta.usage,
                        provider_name,
                    ).items():
                        if isinstance(value, (int, float)):
                            forced_usage[key] = max(forced_usage.get(key, 0), int(value))
                        elif value is not None:
                            with contextlib.suppress(Exception):
                                forced_usage[key] = int(value)
            if not _model_turn_finish_is_successful(
                forced_finish_reason,
                has_tool_calls=False,
            ):
                raise RuntimeError("provider_turn_incomplete")
            if not forced_chunks:
                raise RuntimeError("provider_synthesis_returned_no_text")
            for text_chunk in forced_chunks:
                ctx.generated_content += text_chunk
                if not first_token_emitted:
                    ttft_ms = (time.time() - ttft_start) * 1000
                    first_token_emitted = True
                    logger.info(
                        "[STREAMING-FIRST] TTFT (forced/%s): %.0fms",
                        attempt_label,
                        ttft_ms,
                    )
                    yield AgentLoopEvent(
                        phase=phase,
                        event_type="ttft",
                        data={"ttft_ms": round(ttft_ms, 2)},
                    )
                yield AgentLoopEvent(
                    phase=phase,
                    event_type="text_delta",
                    data=text_chunk,
                )
        except RunBudgetExceeded:
            raise
        except ContextPacketOverflowError as exc:
            logger.warning(
                "[STREAMING-FIRST] Forced synthesis context overflow: overflow_tokens=%s",
                exc.overflow_tokens,
            )
            # A single synthesis attempt is recoverable: the caller retries
            # with a compact packet. Keep this diagnostic non-terminal so a
            # successful compact retry cannot coexist with an earlier
            # run_error for the same run.
            yield AgentLoopEvent(
                phase=phase,
                event_type=StreamEventType.CONTEXT_BUDGET.value,
                data={
                    "run_id": ctx.run_id,
                    "thread_id": ctx.session_id,
                    "session_id": ctx.session_id,
                    "mode": "forced_synthesis",
                    "attempt": attempt_label,
                    "status": "overflow",
                    "error": "protected_context_exceeds_model_window",
                    "overflow_tokens": exc.overflow_tokens,
                    "recoverable": True,
                },
            )
        except Exception as exc:
            logger.error(
                "[STREAMING-FIRST] Forced synthesis (%s) raised; continuing to next fallback "
                "(exception_type=%s)",
                attempt_label,
                type(exc).__name__,
            )
        for key, value in forced_usage.items():
            ctx.usage[key] = int(value)

    # =========================================================================
    # Streaming-First Mode Implementation (Manus-style)
    # =========================================================================


# =============================================================================
# Tool-arg coercion helpers
# =============================================================================


# =============================================================================
# Factory Function
# =============================================================================
