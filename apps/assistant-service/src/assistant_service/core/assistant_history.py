"""Conversation-history helpers shared by Assistant orchestration."""

from __future__ import annotations

from typing import Any

from .agent.agent_loop import PRIOR_TOOL_RESULTS_MARKER


def _session_history_to_messages(
    session_history: list[Any],
) -> list[dict[str, Any]]:
    """Convert ``SessionMessage`` records to the ``{role, content}`` shape the
    rest of the pipeline expects, while preserving prior tool output so
    cross-turn (and especially cross-model) follow-ups can reference it.

    Bug fix 2026-04-21: previously this was a naive
    ``[{"role": m.role, "content": m.content} for m in session.history]``.
    That drops ``m.metadata.tool_results`` entirely — so when a tool (quiz,
    web search, …) produced rich output on turn N and the user asked
    "explain those questions above" on turn N+1 (possibly on a different
    model that doesn't keep provider-side state), the follow-up model saw
    only the assistant's terse text ("已为您生成5道测试题…") and
    hallucinated a brand-new quiz.

    We now append a compact ``[Previous tool results]`` block to the
    assistant message content whenever the persisted metadata contains
    tool_results. The block is bounded per-tool and per-turn to keep
    history budgets predictable.
    """
    out: list[dict[str, Any]] = []
    for m in session_history or []:
        role = getattr(m, "role", None) or "user"
        content = getattr(m, "content", "")
        if role not in ("user", "assistant"):
            continue
        metadata = getattr(m, "metadata", None) or {}
        tool_results = metadata.get("tool_results") if isinstance(metadata, dict) else None
        if role == "assistant" and isinstance(tool_results, list) and tool_results:
            content = _append_tool_results_block(str(content or ""), tool_results)
        out.append({"role": role, "content": content})
    return out


def _append_tool_results_block(
    content: str,
    tool_results: list[dict[str, Any]],
    *,
    per_tool_char_cap: int = 2000,
    total_char_cap: int = 6000,
) -> str:
    """Append a ``[Previous tool results]`` block to an assistant message.

    Each tool entry is capped independently so one verbose tool (e.g. a
    50-question quiz) can't crowd out the others; a total budget also
    protects the full history from blowing up token counts. The block is
    framed with explicit markers so the next-turn model reads it as prior
    context rather than a new instruction.
    """
    if not tool_results:
        return content

    total_used = 0
    # Keep the opening marker stable across stored history and runtime prompt
    # assembly so prior tool evidence remains clearly framed as untrusted data.
    lines: list[str] = [
        "",
        f"{PRIOR_TOOL_RESULTS_MARKER} — for your reference only, not shown to the user]",
    ]
    for entry in tool_results:
        if not isinstance(entry, dict):
            continue
        name = str(entry.get("name") or "tool").strip() or "tool"
        result = entry.get("result")
        if result is None:
            continue
        text = str(result).strip()
        if not text:
            continue
        if len(text) > per_tool_char_cap:
            text = text[: per_tool_char_cap - 3].rstrip() + "..."
        remaining = total_char_cap - total_used
        if remaining <= 0:
            lines.append("... [more tool results truncated from history]")
            break
        if len(text) > remaining:
            text = text[: max(0, remaining - 3)].rstrip() + "..."
        lines.append(f"- {name}: {text}")
        total_used += len(text)
    lines.append("[End previous tool results]")
    return (content or "") + "\n" + "\n".join(lines)
