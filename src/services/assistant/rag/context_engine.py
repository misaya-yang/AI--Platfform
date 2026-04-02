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

from dataclasses import dataclass, field
from typing import Any


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

    def to_budget_event(self) -> dict[str, Any]:
        return {
            "model_context_window": self.model_context_window,
            "reserved_output_tokens": self.reserved_output_tokens,
            "budget_tokens": self.budget_tokens,
            "used_tokens": self.used_tokens,
            "compacted": self.compacted,
        }

    def to_compaction_event(self) -> dict[str, Any]:
        return {
            "dropped_history_messages": self.dropped_history_messages,
            "reason": self.compaction_reason,
            "remaining_history_messages": len(self.trimmed_history),
        }


class ContextBudgetManager:
    """
    Compute per-layer token budgets and compact history when needed.

    This keeps context assembly policy-driven instead of hard-coded prompt branching.
    """

    def __init__(
        self,
        reserved_output_tokens: int = 4096,
        min_recent_messages: int = 6,
    ) -> None:
        self.reserved_output_tokens = reserved_output_tokens
        self.min_recent_messages = min_recent_messages

    def create_plan(
        self,
        context: ContextStructure,
        model_context_window: int,
    ) -> ContextAssemblyPlan:
        available = max(1024, model_context_window - self.reserved_output_tokens)
        budget_tokens = {
            "system": int(available * 0.30),
            "user_memory": int(available * 0.12),
            "skills": int(available * 0.08),   # Skill L1 metadata + L2 instructions
            "session": int(available * 0.20),
            "request": int(available * 0.30),
        }

        system_text = ContextEngine(provider="openai")._build_system_content(context)
        system_tokens = estimate_tokens(system_text)
        request_tokens = estimate_tokens(context.current_query) + estimate_tokens(
            context.current_context or ""
        )

        history = list(context.conversation_history or [])
        history_tokens = estimate_history_tokens(history)

        memory_tokens = estimate_tokens(context.user_preferences or "") + estimate_tokens(
            context.long_term_memory or ""
        )
        session_tokens = estimate_tokens(context.task_state or "")

        max_history_tokens = max(512, available - system_tokens - request_tokens - memory_tokens)
        max_history_tokens = min(max_history_tokens, budget_tokens["session"] + budget_tokens["request"])

        trimmed_history = history
        dropped = 0
        compacted = False
        compaction_reason = None
        if history_tokens > max_history_tokens and len(history) > self.min_recent_messages:
            compacted = True
            compaction_reason = (
                f"history_tokens({history_tokens}) exceeded budget({max_history_tokens})"
            )
            trimmed_history = history[-self.min_recent_messages :]
            dropped = len(history) - len(trimmed_history)
            while estimate_history_tokens(trimmed_history) > max_history_tokens and len(
                trimmed_history
            ) > 1:
                trimmed_history = trimmed_history[1:]
                dropped += 1

        used_tokens = {
            "system": system_tokens,
            "user_memory": memory_tokens,
            "session": session_tokens + estimate_history_tokens(trimmed_history),
            "request": request_tokens,
        }

        return ContextAssemblyPlan(
            model_context_window=model_context_window,
            reserved_output_tokens=self.reserved_output_tokens,
            budget_tokens=budget_tokens,
            used_tokens=used_tokens,
            compacted=compacted,
            dropped_history_messages=dropped,
            compaction_reason=compaction_reason,
            trimmed_history=trimmed_history,
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

        # Add current query (always changes, placed at the end)
        if context.current_query:
            user_content = context.current_query

            # Prepend current context (RAG results, etc.) if available
            if context.current_context:
                user_content = f"{context.current_context}\n\n{user_content}"

            messages.append({"role": "user", "content": user_content})

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

    # Count CJK characters (Chinese, Japanese, Korean)
    cjk_count = sum(
        1
        for c in text
        if "\u4e00" <= c <= "\u9fff"
        or "\u3040" <= c <= "\u30ff"  # Japanese hiragana/katakana
        or "\uac00" <= c <= "\ud7af"
    )  # Korean

    # Non-CJK characters
    ascii_count = len(text) - cjk_count

    # Estimate: CJK ~1.5 tokens/char, ASCII ~0.25 tokens/char
    # Using conservative estimates to avoid overflow
    cjk_tokens = cjk_count * 1.5
    ascii_tokens = ascii_count / 3.5  # ~3.5 chars per token for English

    # Add 15% safety margin to prevent context overflow
    # This accounts for tokenizer variations across different models
    base_estimate = cjk_tokens + ascii_tokens
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
    content = message.get("content", "")
    if isinstance(content, list):
        # Handle multi-part content (text + images)
        text_parts = [p.get("text", "") for p in content if p.get("type") == "text"]
        content = " ".join(text_parts)

    # Add overhead for message formatting (~4 tokens per message)
    return estimate_tokens(str(content)) + 4


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
    import json

    if not tools:
        return ""

    # Sort tools by name for consistent ordering
    sorted_tools = sorted(tools, key=lambda t: t.get("name", ""))

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
