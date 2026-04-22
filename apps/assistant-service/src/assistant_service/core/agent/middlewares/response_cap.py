"""
ResponseCapMiddleware — uniform cap on tool result size.

Anthropic's "Writing effective tools" post recommends ~25K-token default
per-tool response caps with an inline truncation hint. Without this, a
single chatty tool call can blow the whole agent context budget — the
wider blast radius than just the one call, because the full history is
resent on every subsequent turn.

This middleware runs on the `on_tool_result` hook and rewrites oversized
`ToolCallResult.result` values in place (returns a new `ToolCallResult`,
preserving dataclass fields). It handles three shapes:

- str: truncate to the char budget, append the hint.
- dict: JSON-serialize to measure total size; if oversized, walk keys
  looking for the largest string field and truncate *that* so the
  structural keys (`url`, `status`, `content_type` …) survive intact.
- list: same logic against a temporary dict wrapper.

Per-tool overrides let operators give a tool its own cap (e.g. a fs_read
restricted to 10K tokens even when the global default is 25K).
"""

from __future__ import annotations

import json
import logging
from dataclasses import is_dataclass, replace
from typing import TYPE_CHECKING, Any

logger = logging.getLogger(__name__)

if TYPE_CHECKING:
    from ..agent_loop import AgentLoopContext


# Rough token→char ratio. Anthropic's own rule of thumb for English is
# ~4 chars/token; this is the same heuristic Cursor/Claude Code use for
# cheap size gating without a tokenizer in the hot path.
_CHARS_PER_TOKEN = 4


def _truncation_hint(max_tokens: int) -> str:
    # Neutral "truncated" marker only. Earlier wording ("call again with a
    # narrower query") was acting as an instruction: models that saw it
    # would retry search_web 5-10x trying to "narrow down". Keep the signal
    # (content is cut) but drop the implicit action hint — the model can
    # decide for itself whether it has enough.
    return f"\n\n[… response truncated — original exceeded ~{max_tokens}-token cap]"


def _truncate_str(text: str, max_chars: int, max_tokens: int) -> str:
    if len(text) <= max_chars:
        return text
    hint = _truncation_hint(max_tokens)
    # Leave room for the hint so final length doesn't exceed budget much.
    keep = max(0, max_chars - len(hint))
    return text[:keep] + hint


def _truncate_dict(
    data: dict[str, Any], max_chars: int, max_tokens: int
) -> dict[str, Any]:
    """If serialized size exceeds budget, truncate the single largest string
    field in-place (shallow-copied) rather than blowing away the structure."""
    serialized = json.dumps(data, ensure_ascii=False, default=str)
    if len(serialized) <= max_chars:
        return data

    # Find the top-level string field with the biggest content. This
    # covers the common shape where one field (`content`, `text`,
    # `output`) holds the payload and the rest is metadata.
    string_keys = [(k, len(v)) for k, v in data.items() if isinstance(v, str)]
    if not string_keys:
        # No obvious text field — fall back to truncating the JSON blob.
        return {
            "__truncated__": True,
            "preview": _truncate_str(serialized, max_chars, max_tokens),
        }

    string_keys.sort(key=lambda kv: kv[1], reverse=True)
    target_key, target_len = string_keys[0]
    # Budget for that field = total budget − (serialized size − that field size)
    overhead = len(serialized) - target_len
    field_budget = max(256, max_chars - overhead)

    out = dict(data)
    out[target_key] = _truncate_str(data[target_key], field_budget, max_tokens)
    return out


def _apply_cap(payload: Any, max_tokens: int) -> Any:
    """Public-ish helper: truncate `payload` to the given token budget.

    Returns the (possibly new) payload; leaves small payloads untouched.
    """
    max_chars = max_tokens * _CHARS_PER_TOKEN
    if payload is None:
        return None
    if isinstance(payload, str):
        return _truncate_str(payload, max_chars, max_tokens)
    if isinstance(payload, dict):
        return _truncate_dict(payload, max_chars, max_tokens)
    if isinstance(payload, list):
        # Wrap, truncate, unwrap — reuses the dict path.
        wrapped = {"items": payload}
        serialized = json.dumps(wrapped, ensure_ascii=False, default=str)
        if len(serialized) <= max_chars:
            return payload
        # Serialize-based cap on the list by treating the JSON as a string.
        truncated_json = _truncate_str(serialized, max_chars, max_tokens)
        return {"__truncated_list__": True, "preview": truncated_json}
    # Unknown type (int, bool, etc.) — too small to worry about.
    return payload


class ResponseCapMiddleware:
    """Clamp `ToolCallResult.result` to a configurable token budget."""

    name = "response_cap"

    def __init__(
        self,
        max_tokens: int = 25000,
        per_tool_overrides: dict[str, int] | None = None,
    ) -> None:
        self.max_tokens = int(max_tokens)
        self.per_tool_overrides: dict[str, int] = dict(per_tool_overrides or {})

    def _budget_for(self, tool_name: str) -> int:
        return int(self.per_tool_overrides.get(tool_name, self.max_tokens))

    async def on_tool_result(
        self,
        ctx: "AgentLoopContext",
        tool_name: str,
        arguments: dict[str, Any],
        result: Any,
    ) -> Any:
        if result is None:
            return None

        budget = self._budget_for(tool_name)
        payload = getattr(result, "result", None)
        if payload is None:
            return None  # Pass through (error-only results, etc.)

        capped = _apply_cap(payload, budget)
        if capped is payload:
            return None  # Untouched — signal passthrough.

        # Preserve structured metadata so downstream code can see truncation.
        new_metadata = dict(getattr(result, "metadata", {}) or {})
        new_metadata.setdefault("response_cap_applied", True)
        new_metadata.setdefault("response_cap_max_tokens", budget)

        if is_dataclass(result):
            return replace(result, result=capped, metadata=new_metadata)

        # Non-dataclass result: try attribute mutation (best-effort).
        try:
            result.result = capped
            result.metadata = new_metadata
            return result
        except Exception:  # pragma: no cover — defensive
            logger.warning(
                "ResponseCapMiddleware: could not mutate %r; leaving unchanged",
                type(result).__name__,
            )
            return None


__all__ = ["ResponseCapMiddleware"]
