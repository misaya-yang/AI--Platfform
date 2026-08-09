"""Recovery decisions for post-tool provider failures."""

from __future__ import annotations

from typing import Any

import httpx

from .agent_loop_models import StreamingModelTurn


def is_recoverable_post_tool_bad_request(
    error: httpx.HTTPStatusError,
    *,
    iteration: int,
    model_turn: StreamingModelTurn,
    last_tool_failed: bool,
    messages: list[dict[str, Any]],
) -> bool:
    """Allow compact synthesis only after a successful tool result."""

    return bool(
        error.response.status_code == 400
        and iteration > 1
        and not last_tool_failed
        and not model_turn.content
        and not model_turn.tool_calls
        and messages
        and messages[-1].get("role") == "tool"
    )
