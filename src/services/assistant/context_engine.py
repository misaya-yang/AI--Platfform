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
from typing import Any, Dict, List, Optional


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
    tool_definitions: List[Dict[str, Any]] = field(default_factory=list)

    # Layer 2: User-level content (stable within user session)
    user_preferences: Optional[str] = None
    long_term_memory: Optional[str] = None

    # Layer 3: Session-level content (stable within session)
    task_state: Optional[str] = None
    conversation_history: List[Dict[str, Any]] = field(default_factory=list)

    # Layer 4: Request-level content (changes every request)
    current_context: Optional[str] = None
    current_query: str = ""


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
    CACHE_BREAKPOINTS: Dict[str, Optional[Dict[str, Any]]] = {
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

    def build_messages(self, context: ContextStructure) -> List[Dict[str, Any]]:
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
        messages: List[Dict[str, Any]] = []

        # Build system message (stable, high cache hit potential)
        system_content = self._build_system_content(context)
        system_msg: Dict[str, Any] = {
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
        parts: List[str] = [context.system_prompt]

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
