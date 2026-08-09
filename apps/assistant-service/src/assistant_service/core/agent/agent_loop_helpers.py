"""Pure helpers shared by the agent loop and streaming executor."""

from __future__ import annotations

import copy
import hashlib
import json
from typing import Any

from ai_gateway_core.security import redact_trace_text as _redact_trace_text_shared

from ..runtime.context import ContextPacket, envelope_external_content
from .agent_loop_models import AgentLoopContext


def _reject_nonstandard_json_constant(value: str) -> None:
    raise ValueError(f"non-standard JSON constant: {value}")


def _parse_model_tool_arguments(value: Any) -> dict[str, Any]:
    """Parse model-proposed arguments as a finite JSON object."""
    if isinstance(value, dict):
        parsed = value
    elif isinstance(value, str):
        parsed = (
            json.loads(value, parse_constant=_reject_nonstandard_json_constant) if value else {}
        )
    else:
        raise ValueError("tool arguments must be a JSON object")
    if not isinstance(parsed, dict):
        raise ValueError("tool arguments must decode to an object")
    # Reject NaN/Infinity from direct dicts and numeric overflow (for example
    # ``1e309``), both of which Python otherwise permits past ``json.loads``.
    json.dumps(parsed, allow_nan=False)
    return parsed


def _apply_tool_schema_correction_limit(
    ctx: AgentLoopContext,
    tool_name: str,
    validation: dict[str, Any],
) -> dict[str, Any]:
    """Allow one model correction for a tool schema failure in this run."""

    correction_attempt = ctx.tool_schema_correction_counts.get(tool_name, 0) + 1
    ctx.tool_schema_correction_counts[tool_name] = correction_attempt
    return {
        **validation,
        "correction_attempt": correction_attempt,
        "correction_allowed": correction_attempt == 1,
    }


def _effective_packet_output_tokens(
    packet: ContextPacket | None,
    requested: int | None,
) -> int | None:
    if packet is None or packet.reserved_output_tokens <= 0:
        return requested
    if requested is None:
        return packet.reserved_output_tokens
    return min(max(1, int(requested)), packet.reserved_output_tokens)


def _model_turn_finish_is_successful(
    finish_reason: str | None,
    *,
    has_tool_calls: bool,
) -> bool:
    """Classify only explicit provider terminal reasons known to be complete."""

    if finish_reason is None:
        # Preserve compatibility with older OpenAI-compatible streams that
        # terminate using only ``[DONE]``.
        return True
    normalized = finish_reason.strip().lower()
    if has_tool_calls:
        return normalized in {"stop", "tool_calls", "function_call", "tool_use"}
    return normalized in {"stop", "end_turn", "stop_sequence"}


def _tool_name_log_label(value: Any, allowed_names: set[str]) -> str:
    """Log authorized capability names; hash every model-controlled unknown."""

    name = str(value or "")
    if name in allowed_names and all(
        character.isalnum() or character in "._:-" for character in name
    ):
        return name
    digest = hashlib.sha256(name.encode("utf-8")).hexdigest()[:12]
    return f"unrecognized_tool_sha256:{digest}"


# Opening line of the "[Previous tool results]" block that
# ``_session_history_to_messages`` (assistant_service.py) appends to old
# assistant messages so cross-turn / cross-model follow-ups can reference
# prior tool output. BOTH sides import this constant so the framing remains
# stable across the storage and runtime compatibility paths.
PRIOR_TOOL_RESULTS_MARKER = "[Previous tool results"

# Redaction lives in ai_gateway_core.security so trace_writer.py and agent_loop.py
# share one pattern set instead of maintaining copies that can drift out of sync.
_redact_trace_text = _redact_trace_text_shared


def _external_tool_source(tool_name: str) -> str:
    normalized = str(tool_name or "tool").casefold()
    if normalized == "search_knowledge_base":
        return "knowledge_base"
    if "web" in normalized or normalized in {"search", "browser_search"}:
        return "web"
    if normalized.startswith(("mcp_", "mcp:")):
        return "mcp"
    return "tool"


def _envelope_tool_result(content: object, *, tool_name: str, tool_id: str) -> str:
    return envelope_external_content(
        content,
        source=f"{_external_tool_source(tool_name)}:{tool_name}",
        scope="session",
        source_id=tool_id,
    )


def _streaming_tool_step_info(name: str, args: dict[str, Any]) -> dict[str, str]:
    """Map a tool call to the compact Manus-style task panel fields."""
    if name == "search_knowledge_base":
        return {
            "title": "检索知识库",
            "description": str(args.get("query") or "")[:120],
            "icon": "kb",
        }
    if name == "execute_python_code":
        return {"title": "执行代码", "description": "Python", "icon": "code"}
    if name == "generate_image":
        return {
            "title": "生成图片",
            "description": str(args.get("prompt") or "")[:120],
            "icon": "image",
        }
    if name == "generate_document":
        return {
            "title": "生成文档",
            "description": str(args.get("title") or "Document")[:120],
            "icon": "doc",
        }
    if name == "generate_pptx":
        return {
            "title": "生成PPT",
            "description": str(args.get("title") or "Presentation")[:120],
            "icon": "ppt",
        }
    return {"title": f"执行工具: {name}", "description": "", "icon": "tool"}


def _trim_history_for_streaming(
    messages_history: list[dict[str, Any]],
    max_messages: int = 24,
    max_chars: int = 20000,
) -> list[dict[str, Any]]:
    """Sanitize legacy history without silently compacting model-visible data.

    ``max_messages`` and ``max_chars`` remain in the private helper signature
    for compatibility with older callers. Budget reduction now belongs to the
    prepare/validate/commit compaction path, which records lineage; this helper
    only filters unsupported roles and preserves complete allowed messages.
    """

    del max_messages, max_chars
    sanitized: list[dict[str, Any]] = []
    for item in messages_history:
        role = str(item.get("role") or "user")
        if role not in {"user", "assistant", "tool"}:
            continue
        message: dict[str, Any] = {
            "role": role,
            "content": copy.deepcopy(item.get("content", "")),
        }
        for key in ("name", "tool_call_id", "tool_calls", "thought_signature"):
            if item.get(key) is not None:
                message[key] = copy.deepcopy(item[key])
        sanitized.append(message)
    return sanitized


def _compact_forced_synthesis_messages(
    messages: list[dict[str, Any]],
    user_message: str,
) -> tuple[list[dict[str, Any]], list[dict[str, Any]]]:
    """Rebuild a minimal alternating-role prompt after an empty synthesis."""
    tool_messages = [message for message in messages if message.get("role") == "tool"]
    tool_summaries: list[dict[str, Any]] = []
    for message in tool_messages[-5:]:
        tool_name = message.get("name") or "tool"
        content = str(message.get("content") or "").strip()
        if content:
            tool_summaries.append(
                {
                    "name": str(tool_name),
                    "summary": content[:1200],
                }
            )
    system_messages = [message for message in messages if message.get("role") == "system"]
    return (
        [
            *system_messages,
            {
                "role": "user",
                "content": (
                    f"{user_message}\n\n"
                    "Please give the user a direct, helpful answer using the "
                    "untrusted tool-result sources. If they did not find what the "
                    "user needed, say so and suggest one concrete next step."
                ),
            },
        ],
        tool_summaries,
    )


def _forced_synthesis_fallback(messages: list[dict[str, Any]]) -> str:
    """Build the final user-facing fallback from recent tool observations."""
    summary_bits: list[str] = []
    tool_messages = [message for message in messages if message.get("role") == "tool"]
    for message in tool_messages[-3:]:
        tool_name = message.get("name") or "tool"
        content = str(message.get("content") or "").strip()
        if content:
            summary_bits.append(f"- **{tool_name}**: {content[:220]}")
    if summary_bits:
        return (
            "I ran into trouble composing a final answer, but here's what I found. "
            "Please try rephrasing your question or ask a follow-up.\n\n" + "\n".join(summary_bits)
        )
    return (
        "I wasn't able to complete this request. Please try rephrasing your question "
        "or breaking it into smaller parts."
    )


def _coerce_slides(raw: Any) -> list[dict[str, Any]]:
    """Normalise the ``slides`` arg passed by the model to ``generate_pptx``.

    Models (Qwen 3.6 in particular) regularly mis-shape this arg in three
    ways that all crashed the outline emitter at agent_loop.py:2446:

      * Whole arg as a JSON-encoded string (``slides='[{"title": ...}]'``).
        Model itself diagnosed this in chain-of-thought during the
        2026-04-28 incident: "I'm passing slides as a JSON string instead
        of an array."
      * Items as plain strings (``slides=["intro", "method", ...]``) —
        pre-bullet model output before tool-call shape is finalised.
      * Mixed list (some dicts, some strings).

    Anything else (None, int, etc.) → empty list. The tool itself can
    still validate; this helper just ensures we never AttributeError
    inside ``slide.get(...)``.
    """
    if isinstance(raw, str):
        # Try once to parse the whole arg as JSON; fall back to empty
        # rather than treating the string as a single slide title (the
        # tool would then produce a 1-slide deck with no content).
        try:
            parsed = json.loads(raw)
        except (TypeError, ValueError):
            return []
        raw = parsed

    if not isinstance(raw, list):
        return []

    out: list[dict[str, Any]] = []
    for idx, item in enumerate(raw, start=1):
        if isinstance(item, dict):
            out.append(item)
        elif isinstance(item, str):
            # Lift bare-string items into the minimal dict shape the
            # downstream renderer expects. Use the string as the slide
            # title so the user sees their model-generated outline,
            # not a placeholder.
            out.append(
                {
                    "title": item[:80] or f"Slide {idx}",
                    "layout": "content",
                    "bullets": [],
                }
            )
        # else: silently skip — int / None / nested-list have no sane
        # interpretation and would surprise the tool.
    return out
