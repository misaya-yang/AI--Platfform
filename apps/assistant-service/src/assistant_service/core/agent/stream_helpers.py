"""
Shared streaming helpers for agent loops.

Extracted from agent_loop.py to allow reuse by SubAgentManager
and any future agent loop implementations.
"""

from __future__ import annotations

from typing import Any


def merge_stream_tool_calls(
    chunks: list[dict[str, Any]],
    accumulator: dict[str, dict[str, Any]],
    order: list[str],
    anonymous_counter: int,
) -> int:
    """Merge provider tool-call delta chunks into complete tool calls.

    Each streaming delta may contain partial tool call data (name fragment,
    argument fragment, id).  This function accumulates them into complete
    tool call dicts keyed by a stable key derived from index or id.

    Returns the updated anonymous_counter.
    """
    for chunk in chunks:
        if not isinstance(chunk, dict):
            continue

        raw_index = chunk.get("index")
        tool_id = str(chunk.get("id") or "").strip()
        if raw_index is not None:
            try:
                key = f"idx:{int(raw_index)}"
            except (TypeError, ValueError):
                key = f"id:{tool_id}" if tool_id else f"anon:{anonymous_counter}"
        elif tool_id:
            key = f"id:{tool_id}"
        else:
            key = f"anon:{anonymous_counter}"

        if key.startswith("anon:"):
            anonymous_counter += 1

        if key not in accumulator:
            accumulator[key] = {
                "id": "",
                "type": "function",
                "function": {"name": "", "arguments": ""},
            }
            order.append(key)

        merged = accumulator[key]
        if tool_id:
            merged["id"] = tool_id
        chunk_type = chunk.get("type")
        if chunk_type:
            merged["type"] = str(chunk_type)

        function_data = (
            chunk.get("function") if isinstance(chunk.get("function"), dict) else {}
        )
        fn_name = function_data.get("name")
        if fn_name:
            merged["function"]["name"] = str(fn_name)
        fn_args = function_data.get("arguments")
        if fn_args:
            merged["function"]["arguments"] += str(fn_args)

        # Gemini 3 thoughtSignature passthrough.
        if chunk.get("thoughtSignature"):
            merged["thoughtSignature"] = chunk["thoughtSignature"]

    return anonymous_counter
