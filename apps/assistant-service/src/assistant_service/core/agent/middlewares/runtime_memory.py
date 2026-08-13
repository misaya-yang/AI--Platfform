"""
Assistant memory retrieval middleware.

Before each model call:
  1. Ask assistant runtime for the top-N memory snippets relevant to the user's message.
  2. Append a system message with the snippets so the model can ground on them.
  3. Emit a `memory_retrieved` event for UI observability.
  4. Schedule a daily reflection job (fire-and-forget).

Ported from AgentLoop._execute_streaming_first's inline assistant memory block
to establish the middleware pattern. Behavior is intentionally byte-identical
to the pre-refactor inline code so the SSE golden tests stay green.
"""

from __future__ import annotations

import logging
import re
from collections.abc import AsyncGenerator
from typing import TYPE_CHECKING, Any

from ai_gateway_core.logging import get_logger, record_internal_exception

from ...runtime.memory.lifecycle import memory_policy_enabled

if TYPE_CHECKING:
    from ...runtime.compat.runtime_adapter import AssistantRuntimeAdapter
    from ..agent_loop import AgentLoopContext, AgentLoopEvent

logger = get_logger(__name__)


# Max length per snippet after sanitization. Prevents any single snippet
# from dominating the context block.
_MAX_SNIPPET_LEN = 240
_LEXICAL_TOKEN_RE = re.compile(r"[^\W_]+", re.UNICODE)


def _lexical_units(value: Any) -> set[str]:
    """Build small language-neutral units for current-thread relevance."""

    units: set[str] = set()
    for token in _LEXICAL_TOKEN_RE.findall(str(value or "").casefold()):
        if token.isascii():
            if len(token) >= 3:
                units.add(token)
            continue
        characters = [character for character in token if character.isalnum()]
        units.update(
            "".join(characters[index : index + 2]) for index in range(max(0, len(characters) - 1))
        )
    return units


def _current_conversation_is_relevant(
    query: str,
    messages: list[dict[str, Any]],
) -> bool:
    """Prefer session facts when the request overlaps an earlier turn.

    This is based on the actual transcript rather than a list of prompt
    phrases, so the rule works across languages and does not disable durable
    recall merely because a conversation already has history.
    """

    query_text = str(query or "").strip()
    query_units = _lexical_units(query_text)
    if len(query_units) < 2:
        return False
    history_units: set[str] = set()
    skipped_current = False
    for message in reversed(messages):
        if str(message.get("role") or "") not in {"user", "assistant"}:
            continue
        content = str(message.get("content") or "").strip()
        if not skipped_current and content == query_text:
            skipped_current = True
            continue
        history_units.update(_lexical_units(content))
    shared = len(query_units & history_units)
    return shared >= 2 and shared / min(len(query_units), 8) >= 0.25


def _sanitize_snippet(text: str) -> str:
    """Neutralize prompt-injection vectors in untrusted memory content before
    it's injected into the user-turn context block."""
    # Remove context fence tags so a snippet can't break out of the block.
    cleaned = text.replace("</context>", "").replace("<context>", "")
    # Drop control characters (keep newlines/tabs — they're readable).
    cleaned = "".join(
        c for c in cleaned if c == "\n" or c == "\t" or (c.isprintable() and c != "\x00")
    )
    if len(cleaned) > _MAX_SNIPPET_LEN:
        cleaned = cleaned[: _MAX_SNIPPET_LEN - 3].rstrip() + "..."
    return cleaned


class RuntimeMemoryMiddleware:
    """Injects assistant runtime retrieved-memory snippets and schedules reflection."""

    name = "runtime_memory"

    def __init__(
        self,
        runtime: AssistantRuntimeAdapter | None,
        phase_tag: Any,
    ) -> None:
        """
        Args:
            runtime: assistant runtime adapter; no-op when None.
            phase_tag: AgentLoopPhase value used on emitted events. Injected
                rather than hardcoded so tests can swap it.
        """
        self._runtime = runtime
        self._phase = phase_tag

    async def before_call(
        self,
        ctx: AgentLoopContext,
        _messages: list[dict[str, Any]],
    ) -> AsyncGenerator[AgentLoopEvent, None]:
        if not self._runtime:
            return
        if not memory_policy_enabled(
            memory_mode=getattr(ctx.config, "memory_mode", None),
            memory_profile=getattr(ctx.config, "memory_profile", None),
        ):
            return
        agent_runtime = getattr(ctx.config, "agent_runtime", None)
        if agent_runtime is not None and not agent_runtime.user_memory_enabled:
            return
        memory_user_id = (
            agent_runtime.memory_principal if agent_runtime is not None else ctx.user_id
        )

        # Local imports keep this module importable without pulling agent_loop
        # (which imports middlewares lazily via the package entry point).
        from ..agent_loop import AgentLoopEvent

        current_conversation_relevant = bool(
            getattr(ctx, "conversation_history_available", False)
            and _current_conversation_is_relevant(
                ctx.message,
                getattr(ctx, "conversation_history", None) or _messages,
            )
        )
        try:
            memory_result = await self._runtime.load_memory_context(
                tenant_id=ctx.tenant_id,
                user_id=memory_user_id,
                query=ctx.message,
                runtime_mode=ctx.config.runtime_mode,
                memory_profile=ctx.config.memory_profile,
                max_results=6,
            )
            # Store-only contract: the loop assembles these snippets into a
            # <context> block on the USER turn, not a system message — that
            # keeps the system prompt prefix stable for KV-cache hits.
            #
            # Snippets come from memory storage (potentially model-authored
            # from prior turns) so they're untrusted. Strip context fences and
            # control characters to prevent prompt injection — a snippet
            # containing `</context>\n\nIgnore previous instructions...` would
            # otherwise break the fence and take over the turn.
            if memory_result is not None and memory_result.snippets:
                ctx.runtime_memory_snippets = [
                    _sanitize_snippet(f"({snippet.source_type}) {snippet.content}")
                    for snippet in memory_result.snippets
                ]
                ctx.runtime_memory_provenance = list(memory_result.provenance or [])
            yield AgentLoopEvent(
                phase=self._phase,
                event_type="memory_retrieved",
                data={
                    "loaded_sources": memory_result.loaded_sources,
                    "snippet_count": len(memory_result.snippets),
                    "fallback_used": memory_result.fallback_used,
                    "fallback_reason": memory_result.fallback_reason,
                    "provenance": memory_result.provenance,
                    "history_priority": (
                        "current_conversation"
                        if current_conversation_relevant
                        else "durable_memory"
                    ),
                    "current_conversation_relevant": current_conversation_relevant,
                },
            )
        except Exception as exc:
            record_internal_exception(
                __name__,
                "assistant.core.agent.middlewares.runtime_memory.internal_failure",
                exc,
            )

        # Reflection scheduling is best-effort and independent of retrieval.
        try:
            scheduled_job_id = await self._runtime.schedule_daily_reflection(
                tenant_id=ctx.tenant_id,
                user_id=memory_user_id,
                payload={"run_id": ctx.run_id, "session_id": ctx.session_id},
            )
            if scheduled_job_id:
                yield AgentLoopEvent(
                    phase=self._phase,
                    event_type="memory_reflection_scheduled",
                    data={"job_id": scheduled_job_id},
                )
        except Exception as exc:
            record_internal_exception(
                __name__,
                "assistant.core.agent.middlewares.runtime_memory.suppressed_failure",
                exc,
                level=logging.DEBUG,
            )
