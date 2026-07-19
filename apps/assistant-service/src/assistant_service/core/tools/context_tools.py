"""
Model-invokable context management tools.

`context_compact` lets the model (or a user-issued `/compact` slash command
routed as a synthetic tool call) explicitly request conversation-history
compression mid-run. The executor itself does no I/O — it stamps a signal
in `ToolCallResult.metadata["compact_context"]` that the agent loop detects
after tool execution and acts on by running `ContextCompressor` over the
live `messages` list.

Design notes:
- Turn-based, not message-count-based. A turn starts at each `role="user"`
  message; `keep_recent_turns=3` means "keep the last 3 user turns + all
  messages following them intact, compress everything before."
- System messages are always preserved (they carry the prompt).
- No-op when there aren't enough turns to compact (caller still gets a
  success result explaining why).

Why signal-via-metadata: tool executors don't hold a reference to the
loop's live `messages` list. Passing it through `ToolCallRequest` would
break the stateless tool contract. The metadata signal keeps the tool
pure while letting the loop do the mutation at a safe point.
"""

from __future__ import annotations

import time
from typing import Any

from ai_gateway_core.logging import get_logger

from .tool_registry import (
    ToolCallRequest,
    ToolCallResult,
    ToolCategory,
    ToolDefinition,
    ToolExecutor,
    ToolParameter,
    ToolRiskLevel,
    register_tool,
)

logger = get_logger(__name__)


# Sensible bounds — too few turns loses the current task context,
# too many defeats the purpose of compacting.
_MIN_KEEP_TURNS = 1
_MAX_KEEP_TURNS = 10
_DEFAULT_KEEP_TURNS = 3


CONTEXT_COMPACT_DEFINITION = ToolDefinition(
    name="context_compact",
    description=(
        "Compress older conversation history into a short summary, preserving "
        "the most recent user turns intact. Call this when the conversation "
        "has grown long and earlier context is no longer directly relevant — "
        "for example, after finishing a multi-step subtask and starting a new "
        "one. Preserves URLs, code blocks, and referenced artifacts from "
        "the compressed region.\n\n"
        "A 'turn' is one user message plus all assistant/tool messages that "
        "follow it. `keep_recent_turns=3` means the last 3 user questions and "
        "everything the assistant did in response stay in full; everything "
        "before gets summarized."
    ),
    parameters=[
        ToolParameter(
            name="keep_recent_turns",
            type="integer",
            description=(
                "Number of recent user turns to preserve intact (1-10, "
                "default 3). Lower = more aggressive compaction."
            ),
            required=False,
            default=_DEFAULT_KEEP_TURNS,
        ),
        ToolParameter(
            name="reason",
            type="string",
            description=(
                "Optional short reason for the compaction (audit/observability). "
                "Example: 'finished research phase, starting draft'."
            ),
            required=False,
            default="",
        ),
    ],
    category=ToolCategory.UTILITY,
    risk_level=ToolRiskLevel.LOW,
    when_to_use=(
        "After completing a discrete subtask and the details of that subtask "
        "are no longer needed for the next step. Also when the tool-result "
        "history contains large blobs (search results, file contents) that "
        "are now stale."
    ),
    when_not_to_use=(
        "Don't call this in the middle of a subtask — you'll lose working "
        "context. Don't call it if the conversation is short (fewer than "
        "keep_recent_turns+1 user messages)."
    ),
    timeout_seconds=2,
)


class ContextCompactExecutor(ToolExecutor):
    """Signals the agent loop to compress history.

    The executor is intentionally a no-op on `messages` — it just validates
    the argument and stamps a metadata signal. The loop reads that signal
    after the tool completes and runs `ContextCompressor` over the live
    `messages` list. This keeps the tool stateless and testable in isolation.
    """

    async def execute(self, request: ToolCallRequest) -> ToolCallResult:
        start = time.time()

        keep_raw = request.arguments.get("keep_recent_turns", _DEFAULT_KEEP_TURNS)
        try:
            keep_turns = int(keep_raw)
        except (TypeError, ValueError):
            return ToolCallResult(
                call_id=request.call_id,
                tool_name=request.tool_name,
                success=False,
                error=f"keep_recent_turns must be an integer, got {keep_raw!r}",
                duration_ms=(time.time() - start) * 1000,
            )
        keep_turns = max(_MIN_KEEP_TURNS, min(_MAX_KEEP_TURNS, keep_turns))

        reason = str(request.arguments.get("reason") or "").strip()[:200]

        logger.info(
            "context_compact requested (call_id=%s keep_recent_turns=%d reason=%r)",
            request.call_id,
            keep_turns,
            reason,
        )

        # The loop reads `metadata["compact_context"]` and performs the
        # actual compression. Putting a structured dict (not a flag) leaves
        # room to add knobs later (e.g., target_tokens, force) without
        # another round of tool-contract changes.
        signal: dict[str, Any] = {
            "keep_recent_turns": keep_turns,
            "reason": reason,
        }

        return ToolCallResult(
            call_id=request.call_id,
            tool_name=request.tool_name,
            success=True,
            result=(
                f"Compaction scheduled: keeping last {keep_turns} user turn(s); "
                "earlier history will be summarized while preserving URLs, "
                "code blocks, and artifacts."
            ),
            metadata={"compact_context": signal},
            duration_ms=(time.time() - start) * 1000,
        )


def register_context_tools() -> None:
    """Register context management tools. Idempotent."""
    register_tool(CONTEXT_COMPACT_DEFINITION, ContextCompactExecutor())


__all__ = [
    "CONTEXT_COMPACT_DEFINITION",
    "ContextCompactExecutor",
    "register_context_tools",
]
