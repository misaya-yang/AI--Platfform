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
import re
from collections.abc import AsyncGenerator
from typing import TYPE_CHECKING, Any

from ai_gateway_core.logging import get_logger

from ...runtime.memory.lifecycle import memory_policy_enabled

if TYPE_CHECKING:
    from ...runtime.compat.runtime_adapter import AssistantRuntimeAdapter
    from ..agent_loop import AgentLoopContext, AgentLoopEvent

logger = get_logger(__name__)


# Max length per snippet after sanitization. Prevents any single snippet
# from dominating the context block.
_MAX_SNIPPET_LEN = 240

_CURRENT_CONVERSATION_REFERENCES = (
    re.compile(r"(?:当前|本次|这个|这次)(?:会话|对话)"),
    re.compile(r"(?:刚才|方才|上一条|上一轮|上文)(?:说|提|写|告诉|消息|回复)?"),
    re.compile(r"我(?:最早|之前|此前)告诉你的"),
    re.compile(
        r"\b(?:this|current)\s+(?:conversation|session|chat|thread)\b",
        re.IGNORECASE,
    ),
    re.compile(
        r"\b(?:earlier|previous|last)\s+(?:message|turn|reply)\b",
        re.IGNORECASE,
    ),
    re.compile(
        r"\bI\s+(?:just|earlier|previously)\s+(?:told|said|mentioned|wrote)\b", re.IGNORECASE
    ),
)


def _prefers_current_conversation(query: str) -> bool:
    """Return whether the request explicitly scopes recall to this thread."""

    value = str(query or "").strip()
    return bool(value) and any(
        pattern.search(value) for pattern in _CURRENT_CONVERSATION_REFERENCES
    )


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

        current_conversation_only = bool(
            getattr(ctx, "conversation_history_available", False)
            and _prefers_current_conversation(ctx.message)
        )
        try:
            if current_conversation_only:
                memory_result = None
            else:
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
                    "loaded_sources": (
                        0 if memory_result is None else memory_result.loaded_sources
                    ),
                    "snippet_count": 0 if memory_result is None else len(memory_result.snippets),
                    "fallback_used": current_conversation_only
                    or (memory_result is not None and memory_result.fallback_used),
                    "fallback_reason": (
                        "current_conversation_preferred"
                        if current_conversation_only
                        else memory_result.fallback_reason
                    ),
                    "provenance": [] if memory_result is None else memory_result.provenance,
                },
            )
        except Exception as exc:
            logger.error(
                "assistant memory retrieval failed (exception_type=%s)",
                type(exc).__name__,
            )

        # Reflection scheduling is best-effort and independent of retrieval.
        with contextlib.suppress(Exception):
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
