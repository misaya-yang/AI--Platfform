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

import contextlib
from collections.abc import AsyncGenerator
from typing import TYPE_CHECKING, Any

from ai_gateway_core.logging import get_logger

if TYPE_CHECKING:
    from ...runtime.compat.runtime_adapter import AssistantRuntimeAdapter
    from ..agent_loop import AgentLoopContext, AgentLoopEvent

logger = get_logger(__name__)


# Max length per snippet after sanitization. Prevents any single snippet
# from dominating the context block.
_MAX_SNIPPET_LEN = 240


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

        # Local imports keep this module importable without pulling agent_loop
        # (which imports middlewares lazily via the package entry point).
        from ..agent_loop import AgentLoopEvent

        try:
            memory_result = await self._runtime.load_memory_context(
                tenant_id=ctx.tenant_id,
                user_id=ctx.user_id,
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
            if memory_result.snippets:
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
                },
            )
        except Exception:
            logger.exception("assistant memory retrieval failed")

        # Reflection scheduling is best-effort and independent of retrieval.
        with contextlib.suppress(Exception):
            scheduled_job_id = await self._runtime.schedule_daily_reflection(
                tenant_id=ctx.tenant_id,
                user_id=ctx.user_id,
                payload={"run_id": ctx.run_id, "session_id": ctx.session_id},
            )
            if scheduled_job_id:
                yield AgentLoopEvent(
                    phase=self._phase,
                    event_type="memory_reflection_scheduled",
                    data={"job_id": scheduled_job_id},
                )
