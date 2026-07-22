"""
Context Engine - Stable Prefix Design for KV-Cache Optimization.

Implements the Manus Context Engineering principles for optimal LLM performance:
- Stable content positioned at the top of the context for maximum cache hits
- Layered structure: Static -> User-level -> Session-level -> Request-level
- Conversation history append-only to maintain cache coherence
- Provider-specific cache control hints (e.g., Anthropic's ephemeral cache)

The stable prefix design ensures that the most frequently reused content
(system prompts, tool definitions, user preferences) remains at the beginning
of the context, maximizing KV-Cache hit rates across multiple requests.

References:
- Manus Context Engineering: https://manus.ai/blog/context-engineering
- Anthropic Prompt Caching: https://docs.anthropic.com/en/docs/build-with-claude/prompt-caching
"""

from __future__ import annotations

import copy
import json
from dataclasses import dataclass, field
from typing import Any

CONTEXT_PACKET_ORDER = [
    "stable_system_policy",
    "current_turn_and_session_state",
    "selected_capability_metadata",
    "scoped_memory_snippets",
    "rag_source_summaries",
    "recent_tool_artifact_summaries",
    "compaction_summary",
    "budget_telemetry",
]


@dataclass
class ContextStructure:
    """
    Stable prefix design for KV-Cache optimization.

    Organizes context into four layers ordered by stability (most stable first):

    Layer 1 - Static (highest cache hit potential):
        - system_prompt: Core instructions that rarely change
        - tool_definitions: Available tools and their schemas

    Layer 2 - User-level (changes per user, stable within user session):
        - user_preferences: User-specific settings and preferences
        - long_term_memory: Persistent information about the user

    Layer 3 - Session-level (changes per session):
        - task_state: Current task progress and state
        - conversation_history: Append-only message history

    Layer 4 - Request-level (changes every request):
        - current_context: RAG context, KB results for current query
        - current_query: The user's current input

    This ordering ensures stable content is at the top of the context,
    maximizing KV-Cache reuse across multiple requests within the same
    session or across sessions for the same user.
    """

    # Layer 1: Static content (highest cache hit potential)
    system_prompt: str
    tool_definitions: list[dict[str, Any]] = field(default_factory=list)

    # Layer 2: User-level content (stable within user session)
    user_preferences: str | None = None
    long_term_memory: str | None = None

    # Layer 3: Session-level content (stable within session)
    task_state: str | None = None
    conversation_history: list[dict[str, Any]] = field(default_factory=list)

    # Layer 4: Request-level content (changes every request)
    current_context: str | None = None
    current_query: str = ""
    current_images: list[str] = field(default_factory=list)


@dataclass
class ContextAssemblyPlan:
    """
    Budget-driven context assembly plan.

    Captures how each layer consumed budget and whether compaction happened.
    """

    model_context_window: int
    reserved_output_tokens: int
    budget_tokens: dict[str, int] = field(default_factory=dict)
    used_tokens: dict[str, int] = field(default_factory=dict)
    compacted: bool = False
    dropped_history_messages: int = 0
    compaction_reason: str | None = None
    trimmed_history: list[dict[str, Any]] = field(default_factory=list)
    trimmed_current_context: str | None = None
    dropped_request_context_chars: int = 0
    dropped_invalid_tool_messages: int = 0
    protected_overflow_tokens: int = 0
    budget_status: str = "within_budget"

    def to_budget_event(self) -> dict[str, Any]:
        return {
            "model_context_window": self.model_context_window,
            "reserved_output_tokens": self.reserved_output_tokens,
            "budget_tokens": self.budget_tokens,
            "used_tokens": self.used_tokens,
            "compacted": self.compacted,
            "compaction": self.to_compaction_event(),
            "context_packet_order": list(CONTEXT_PACKET_ORDER),
            "protected_overflow_tokens": self.protected_overflow_tokens,
            "budget_status": self.budget_status,
        }

    def to_compaction_event(self) -> dict[str, Any]:
        return {
            "dropped_history_messages": self.dropped_history_messages,
            "reason": self.compaction_reason,
            "remaining_history_messages": len(self.trimmed_history),
            "dropped_request_context_chars": self.dropped_request_context_chars,
            "dropped_invalid_tool_messages": self.dropped_invalid_tool_messages,
        }


def _tool_call_ids(message: dict[str, Any]) -> set[str]:
    calls = message.get("tool_calls")
    if not isinstance(calls, list):
        return set()
    return {
        str(call.get("id") or "")
        for call in calls
        if isinstance(call, dict) and str(call.get("id") or "")
    }


def _validated_tool_call_ids(message: dict[str, Any]) -> set[str] | None:
    """Return unique non-empty call IDs, or ``None`` for malformed calls."""

    calls = message.get("tool_calls")
    if calls is None:
        return set()
    if not isinstance(calls, list):
        return None
    identifiers: set[str] = set()
    for call in calls:
        if not isinstance(call, dict):
            return None
        call_id = str(call.get("id") or "")
        function = call.get("function")
        if not call_id or call_id in identifiers or not isinstance(function, dict):
            return None
        if not str(function.get("name") or ""):
            return None
        identifiers.add(call_id)
    return identifiers


def _history_units(
    history: list[dict[str, Any]],
) -> tuple[list[list[dict[str, Any]]], int]:
    """Group assistant tool calls with their results and drop orphan tool messages."""

    units: list[list[dict[str, Any]]] = []
    invalid = 0
    index = 0
    while index < len(history):
        raw = history[index]
        if not isinstance(raw, dict):
            invalid += 1
            index += 1
            continue
        message = copy.deepcopy(raw)
        role = str(message.get("role") or "")
        if role not in {"user", "assistant", "tool"}:
            invalid += 1
            index += 1
            continue
        if role == "tool":
            invalid += 1
            index += 1
            continue

        expected = _validated_tool_call_ids(message) if role == "assistant" else set()
        if expected is None:
            invalid += 1
            index += 1
            continue
        if not expected:
            units.append([message])
            index += 1
            continue

        unit = [message]
        found: set[str] = set()
        cursor = index + 1
        while cursor < len(history):
            candidate = history[cursor]
            if not isinstance(candidate, dict) or candidate.get("role") != "tool":
                break
            tool_call_id = str(candidate.get("tool_call_id") or "")
            if tool_call_id in expected and tool_call_id not in found:
                unit.append(copy.deepcopy(candidate))
                found.add(tool_call_id)
            else:
                invalid += 1
            cursor += 1
        if found == expected:
            units.append(unit)
        else:
            # An incomplete historical tool exchange is not provider-valid.
            invalid += len(unit)
        index = cursor
    return units, invalid


def _flatten_history_units(units: list[list[dict[str, Any]]]) -> list[dict[str, Any]]:
    return [message for unit in units for message in unit]


def _trim_history_preserving_tool_pairs(
    history: list[dict[str, Any]],
    *,
    max_tokens: int,
    min_recent_messages: int,
) -> tuple[list[dict[str, Any]], int, int]:
    units, invalid = _history_units(history)
    if not units:
        return [], len(history), invalid

    # Select from complete exchange units, never individual messages. Only the
    # newest complete tool exchange is hard-protected; ordinary recent chat is
    # a soft target and may be pruned to preserve current request context.
    protected_index: int | None = None
    for unit_index in range(len(units) - 1, -1, -1):
        if any(_tool_call_ids(message) for message in units[unit_index]):
            protected_index = unit_index
            break

    selected_by_index: dict[int, list[dict[str, Any]]] = {}
    for unit_index in range(len(units) - 1, -1, -1):
        unit = units[unit_index]
        candidate = [
            candidate_unit
            for index, candidate_unit in sorted({**selected_by_index, unit_index: unit}.items())
        ]
        candidate_tokens = estimate_history_tokens(_flatten_history_units(candidate))
        if unit_index == protected_index or candidate_tokens <= max(0, max_tokens):
            selected_by_index[unit_index] = unit
            if (
                protected_index is None
                and sum(len(value) for value in selected_by_index.values())
                >= max(1, min_recent_messages)
                and candidate_tokens >= max(0, max_tokens)
            ):
                break

    selected = [unit for _, unit in sorted(selected_by_index.items())]
    selected_tokens = estimate_history_tokens(_flatten_history_units(selected))

    trimmed = _flatten_history_units(selected)
    dropped = max(0, len(history) - len(trimmed))
    # ``selected_tokens`` is deliberately computed even when the protected
    # suffix exceeds the budget. The caller reports that as a typed protected
    # overflow instead of silently dropping a current-turn tool exchange.
    _ = selected_tokens
    return trimmed, dropped, invalid


def _trim_text_to_token_budget(value: str, max_tokens: int) -> str:
    if not value or max_tokens <= 0:
        return ""
    if estimate_tokens(value) <= max_tokens:
        return value
    marker = "\n...[context truncated by budget]"
    low = 0
    high = len(value)
    while low < high:
        midpoint = (low + high + 1) // 2
        candidate = value[:midpoint].rstrip() + marker
        if estimate_tokens(candidate) <= max_tokens:
            low = midpoint
        else:
            high = midpoint - 1
    return value[:low].rstrip() + marker if low else ""


class ContextBudgetManager:
    """
    Compute per-layer token budgets and compact history when needed.

    This keeps context assembly policy-driven instead of hard-coded prompt branching.
    """

    def __init__(
        self,
        reserved_output_tokens: int = 4096,
        min_recent_messages: int = 6,
        max_history_tokens: int = 40000,
    ) -> None:
        self.reserved_output_tokens = reserved_output_tokens
        self.min_recent_messages = min_recent_messages
        self.max_history_tokens = max(512, int(max_history_tokens))

    def create_plan(
        self,
        context: ContextStructure,
        model_context_window: int,
    ) -> ContextAssemblyPlan:
        window = max(1, int(model_context_window))
        # A configured output reserve cannot consume more than half a tiny
        # model window. This keeps the calculation truthful while still
        # leaving a bounded response budget.
        effective_reserved_output_tokens = min(
            max(0, int(self.reserved_output_tokens)),
            window // 2,
        )
        available = max(1, window - effective_reserved_output_tokens)
        budget_tokens = {
            "system": int(available * 0.30),
            "user_memory": int(available * 0.12),
            "skills": int(available * 0.08),  # Skill L1 metadata + L2 instructions
            "session": int(available * 0.20),
            "request": int(available * 0.30),
        }

        system_text = ContextEngine(provider="openai")._build_system_content(context)
        system_tokens = estimate_message_tokens({"role": "system", "content": system_text})
        query_tokens = estimate_message_tokens(
            {
                "role": "user",
                "content": context.current_query,
                "images": context.current_images,
            }
        )
        tool_tokens = estimate_tokens(serialize_tools_deterministic(context.tool_definitions))
        current_context = context.current_context or ""
        history = list(context.conversation_history or [])

        # Calculate the complete recent suffix first. It is protected together
        # with the stable policy, current request and effective tool schema.
        recent_history, _, _ = _trim_history_preserving_tool_pairs(
            history,
            max_tokens=0,
            min_recent_messages=self.min_recent_messages,
        )
        recent_history_tokens = estimate_history_tokens(recent_history)
        protected_tokens = system_tokens + query_tokens + tool_tokens + recent_history_tokens
        remaining_after_protected = max(0, available - protected_tokens)
        extra_history_reserve = min(
            max(0, budget_tokens["session"] - recent_history_tokens),
            remaining_after_protected // 2,
        )
        request_context_budget = max(0, remaining_after_protected - extra_history_reserve)
        trimmed_current_context = _trim_text_to_token_budget(
            current_context,
            request_context_budget,
        )
        dropped_request_context_chars = max(
            0,
            len(current_context) - len(trimmed_current_context),
        )
        request_tokens = query_tokens + estimate_tokens(trimmed_current_context)

        memory_tokens = estimate_tokens(context.user_preferences or "") + estimate_tokens(
            context.long_term_memory or ""
        )
        session_tokens = estimate_tokens(context.task_state or "")

        max_history_tokens = max(
            0,
            available - system_tokens - request_tokens - tool_tokens,
        )
        max_history_tokens = min(
            max_history_tokens,
            budget_tokens["session"] + budget_tokens["request"],
            self.max_history_tokens,
        )

        trimmed_history, dropped, invalid_tool_messages = _trim_history_preserving_tool_pairs(
            history,
            max_tokens=max_history_tokens,
            min_recent_messages=self.min_recent_messages,
        )
        compacted = bool(dropped or dropped_request_context_chars)
        reasons: list[str] = []
        if dropped:
            reasons.append(f"history exceeded budget({max_history_tokens}) or was invalid")
        if dropped_request_context_chars:
            reasons.append("lower-priority request context exceeded budget")
        compaction_reason = "; ".join(reasons) or None

        actual_input_tokens = (
            system_tokens + tool_tokens + request_tokens + estimate_history_tokens(trimmed_history)
        )
        protected_overflow_tokens = max(0, actual_input_tokens - available)
        budget_status = (
            "protected_overflow"
            if protected_overflow_tokens
            else "compacted"
            if compacted
            else "within_budget"
        )

        used_tokens = {
            "system": system_tokens,
            "user_memory": memory_tokens,
            "skills": tool_tokens,
            "session": session_tokens + estimate_history_tokens(trimmed_history),
            "request": request_tokens,
        }

        return ContextAssemblyPlan(
            model_context_window=window,
            reserved_output_tokens=effective_reserved_output_tokens,
            budget_tokens=budget_tokens,
            used_tokens=used_tokens,
            compacted=compacted,
            dropped_history_messages=dropped,
            compaction_reason=compaction_reason,
            trimmed_history=trimmed_history,
            trimmed_current_context=trimmed_current_context or None,
            dropped_request_context_chars=dropped_request_context_chars,
            dropped_invalid_tool_messages=invalid_tool_messages,
            protected_overflow_tokens=protected_overflow_tokens,
            budget_status=budget_status,
        )


class ContextEngine:
    """
    Manages context construction with KV-Cache optimization.

    The ContextEngine builds message lists from ContextStructure in a way
    that maximizes KV-Cache hit rates by:

    1. Placing stable content (system prompt, user preferences) first
    2. Using cache control hints for supported providers (Anthropic)
    3. Keeping conversation history append-only
    4. Placing dynamic content (current query, context) at the end

    Usage:
        ```python
        engine = ContextEngine(provider="anthropic")

        context = ContextStructure(
            system_prompt="You are a helpful assistant.",
            tool_definitions=[{"name": "search", "description": "Search the web"}],
            user_preferences="Prefer concise responses",
            conversation_history=[
                {"role": "user", "content": "Hello"},
                {"role": "assistant", "content": "Hi there!"}
            ],
            current_query="What's the weather like?"
        )

        messages = engine.build_messages(context)
        # Send messages to LLM API
        ```

    Supported Providers:
        - anthropic: Adds cache_control hints for prompt caching
        - openai: Standard message format (no cache hints)
        - other: Falls back to standard message format
    """

    # Cache breakpoint markers for different providers
    # These values are assigned to message["cache_control"] directly
    CACHE_BREAKPOINTS: dict[str, dict[str, Any] | None] = {
        "anthropic": {"type": "ephemeral"},
        "openai": None,  # OpenAI doesn't support explicit cache control
    }

    def __init__(self, provider: str) -> None:
        """
        Initialize the ContextEngine for a specific provider.

        Args:
            provider: The LLM provider name (e.g., "anthropic", "openai").
                      Case-insensitive.
        """
        self.provider = provider.lower()

    def build_messages(self, context: ContextStructure) -> list[dict[str, Any]]:
        """
        Build messages with stable prefix for cache optimization.

        Constructs a message list ordered for maximum KV-Cache efficiency:
        1. System message (stable, includes user preferences and memory)
        2. Conversation history (append-only, maintains cache coherence)
        3. Current user message (dynamic, always at the end)

        For Anthropic provider, adds cache_control hints to enable prompt caching.

        Args:
            context: The ContextStructure containing all context layers.

        Returns:
            List of message dictionaries ready for LLM API consumption.
            Each message has at minimum 'role' and 'content' keys.
            For Anthropic, system message includes 'cache_control' key.

        Example output:
            ```python
            [
                {
                    "role": "system",
                    "content": "You are a helpful assistant.\n\n## User Preferences\n...",
                    "cache_control": {"type": "ephemeral"}  # Anthropic only
                },
                {"role": "user", "content": "Hello"},
                {"role": "assistant", "content": "Hi there!"},
                {"role": "user", "content": "Current context...\n\nWhat's the weather?"}
            ]
            ```
        """
        messages: list[dict[str, Any]] = []

        # Build system message (stable, high cache hit potential)
        system_content = self._build_system_content(context)
        system_msg: dict[str, Any] = {
            "role": "system",
            "content": system_content,
        }

        # Add cache breakpoint for Anthropic to enable prompt caching
        if self.provider == "anthropic":
            cache_control = self.CACHE_BREAKPOINTS.get("anthropic")
            if cache_control:
                system_msg["cache_control"] = cache_control

        messages.append(system_msg)

        # Add conversation history (append-only for cache coherence)
        # This ensures that extending the conversation doesn't invalidate
        # the cache for the system message and previous history
        if context.conversation_history:
            messages.extend(context.conversation_history)

        # Always preserve the current user turn. Empty-text requests are valid
        # compatibility inputs and may also carry attachments or scoped
        # context; omitting the role would change provider conversation shape.
        user_content = context.current_query or ""

        # Prepend current context (RAG results, etc.) if available
        if context.current_context:
            user_content = (
                f"{context.current_context}\n\n{user_content}"
                if user_content
                else context.current_context
            )

        current_message: dict[str, Any] = {"role": "user", "content": user_content}
        if context.current_images:
            current_message["images"] = list(context.current_images)
        messages.append(current_message)

        return messages

    def _build_system_content(self, context: ContextStructure) -> str:
        """
        Build stable system prompt content.

        Combines all stable context elements into a single system prompt:
        1. Base system prompt (always included)
        2. User preferences (if available)
        3. Long-term memory / background knowledge (if available)
        4. Current task state (if available)

        The order is intentional - most stable content first, followed by
        progressively more dynamic content. This ordering within the system
        message further optimizes cache hit rates.

        Args:
            context: The ContextStructure containing system prompt and optional
                     user-level and session-level context.

        Returns:
            A formatted string containing the complete system prompt with
            all applicable sections separated by Markdown headers.

        Example output:
            ```
            You are a helpful assistant.

            ## User Preferences
            Prefer concise responses. Use formal language.

            ## Background Knowledge
            User is a software engineer working on Python projects.

            ## Current Task State
            Working on implementing a REST API.
            ```
        """
        parts: list[str] = [context.system_prompt]

        # Add user preferences (Layer 2 - User-level)
        if context.user_preferences:
            parts.append(f"\n## User Preferences\n{context.user_preferences}")

        # Add long-term memory / background knowledge (Layer 2 - User-level)
        if context.long_term_memory:
            parts.append(f"\n## Background Knowledge\n{context.long_term_memory}")

        # Add current task state (Layer 3 - Session-level)
        if context.task_state:
            parts.append(f"\n## Current Task State\n{context.task_state}")

        return "\n".join(parts)


# =============================================================================
# Token Estimation Utilities
# =============================================================================


def estimate_tokens(text: str) -> int:
    """
    Fast token estimation for context management.

    Uses a simple heuristic that works well for mixed Chinese/English text:
    - Chinese characters: ~1.5 tokens per character
    - English/ASCII: ~4 characters per token
    - Average: ~3 characters per token for mixed content

    This is intentionally conservative to avoid context overflow.
    For production precision, use tiktoken or provider-specific tokenizers.

    Args:
        text: The text to estimate tokens for

    Returns:
        Estimated number of tokens (conservative estimate)

    Example:
        >>> estimate_tokens("Hello, world!")  # ~4 tokens
        >>> estimate_tokens("你好世界")        # ~3 tokens
    """
    if not text:
        return 0

    # Count non-Latin characters that tokenize differently
    # CJK: ~1.5 tokens/char; Arabic/Hebrew: ~2 tokens/char
    non_latin_count = sum(
        1
        for c in text
        if "\u4e00" <= c <= "\u9fff"  # CJK Unified
        or "\u3040" <= c <= "\u30ff"  # Japanese hiragana/katakana
        or "\uac00" <= c <= "\ud7af"  # Korean
        or "\u0600" <= c <= "\u06ff"  # Arabic
        or "\u0750" <= c <= "\u077f"  # Arabic Supplement
        or "\ufb50" <= c <= "\ufdff"  # Arabic Presentation Forms-A
        or "\ufe70" <= c <= "\ufeff"  # Arabic Presentation Forms-B
        or "\u0590" <= c <= "\u05ff"  # Hebrew
    )

    ascii_count = len(text) - non_latin_count

    # Estimate: CJK/Arabic ~1.5 tokens/char, ASCII ~0.25 tokens/char
    non_latin_tokens = non_latin_count * 1.5
    ascii_tokens = ascii_count / 3.5  # ~3.5 chars per token for English

    # Add 15% safety margin to prevent context overflow
    # This accounts for tokenizer variations across different models
    base_estimate = non_latin_tokens + ascii_tokens
    return int(base_estimate * 1.15)


def estimate_message_tokens(message: dict) -> int:
    """
    Estimate tokens for a single chat message.

    Includes overhead for message formatting (role, etc.).

    Args:
        message: A message dict with 'role' and 'content' keys

    Returns:
        Estimated tokens for this message
    """
    sanitized = copy.deepcopy(dict(message or {}))
    image_count = 0

    images = sanitized.get("images")
    if isinstance(images, list):
        image_count += len(images)
        sanitized["images"] = ["<binary-image>"] * len(images)

    content = sanitized.get("content")
    if isinstance(content, list):
        safe_parts: list[Any] = []
        for raw_part in content:
            if not isinstance(raw_part, dict):
                safe_parts.append(raw_part)
                continue
            part = copy.deepcopy(raw_part)
            part_type = str(part.get("type") or "")
            is_image = part_type in {"image", "image_url", "input_image"} or any(
                key in part for key in ("image_url", "inlineData", "inline_data")
            )
            if is_image:
                image_count += 1
                safe_parts.append({"type": part_type or "image", "data": "<binary-image>"})
            else:
                safe_parts.append(part)
        sanitized["content"] = safe_parts

    serialized = json.dumps(
        sanitized,
        ensure_ascii=False,
        sort_keys=True,
        separators=(",", ":"),
        default=str,
    )
    # Images vary by provider and resolution. A fixed conservative charge
    # prevents data URLs from being ignored without retaining their payload.
    return estimate_tokens(serialized) + image_count * 1024 + 4


def estimate_history_tokens(history: list) -> int:
    """
    Estimate total tokens for conversation history.

    Args:
        history: List of message dicts

    Returns:
        Total estimated tokens
    """
    return sum(estimate_message_tokens(msg) for msg in history)


# =============================================================================
# Module-level convenience functions
# =============================================================================


def create_context_engine(provider: str) -> ContextEngine:
    """
    Factory function to create a ContextEngine for a specific provider.

    Args:
        provider: The LLM provider name (e.g., "anthropic", "openai").

    Returns:
        A configured ContextEngine instance.
    """
    return ContextEngine(provider=provider)


def serialize_tools_deterministic(tools: list[dict[str, Any]]) -> str:
    """
    Serialize tool definitions deterministically for KV-cache optimization.

    Ensures consistent serialization order by:
    1. Sorting tools by name
    2. Removing variable fields (timestamps, etc.)
    3. Sorting keys within each tool definition

    This guarantees identical tool serialization across requests,
    maximizing cache hit rates for the static prefix.

    Args:
        tools: List of tool definition dictionaries

    Returns:
        Deterministically serialized JSON string of tools
    """
    if not tools:
        return ""

    # Sort tools by name for consistent ordering
    sorted_tools = sorted(
        tools,
        key=lambda tool: str(tool.get("name") or (tool.get("function") or {}).get("name") or ""),
    )

    # Remove variable fields and sort keys
    stable_tools = []
    variable_fields = {"created_at", "updated_at", "last_used", "usage_count"}

    for tool in sorted_tools:
        stable_tool = {k: v for k, v in sorted(tool.items()) if k not in variable_fields}
        stable_tools.append(stable_tool)

    return json.dumps(stable_tools, sort_keys=True, ensure_ascii=False, indent=2)


def format_long_term_memory(memory_context: dict[str, Any]) -> str:
    """
    Format long-term memory context for inclusion in system prompt.

    Structures frequent memories and learned patterns into a
    concise context section that helps personalize responses.

    Args:
        memory_context: Dictionary with preferences and frequent_memories

    Returns:
        Formatted string for system prompt inclusion
    """
    if not memory_context:
        return ""

    parts = []

    # Format preferences
    preferences = memory_context.get("preferences", {})
    if preferences:
        pref_items = []
        for key, value in sorted(preferences.items()):
            if value and key not in ("language",):  # Skip obvious defaults
                pref_items.append(f"- {key}: {value}")
        if pref_items:
            parts.append("### Preferences\n" + "\n".join(pref_items))

    # Format frequent memories (learned patterns)
    frequent = memory_context.get("frequent_memories", [])
    if frequent:
        memory_items = []
        preferred_name_present = any(
            str(mem.get("key", "")) == "profile:preferred_name" for mem in frequent
        )
        ordered_memories = sorted(
            frequent,
            key=lambda mem: (
                0 if str(mem.get("key", "")).startswith("profile:") else 1,
                str(mem.get("key", "")),
            ),
        )
        for mem in ordered_memories[:5]:  # Top 5 only
            key = mem.get("key", "")
            value = mem.get("value", "")
            if key and value and key != "preferences":
                if preferred_name_present and key == "user_name":
                    continue
                # Truncate long values
                val_str = str(value)[:100] if len(str(value)) > 100 else str(value)
                display_key = {
                    "profile:preferred_name": "preferred_name",
                    "profile:location": "location",
                    "user_name": "preferred_name",
                }.get(str(key), str(key))
                memory_items.append(f"- {display_key}: {val_str}")
        if memory_items:
            parts.append("### Learned Context\n" + "\n".join(memory_items))

    return "\n\n".join(parts) if parts else ""
