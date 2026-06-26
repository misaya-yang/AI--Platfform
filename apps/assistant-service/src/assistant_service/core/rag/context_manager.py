"""
Context Manager - Intelligent conversation history management.

Implements industry best practices for LLM context management:
- Sliding window: Keep recent N messages in full
- Token-aware truncation: Respect model context limits
- Optional summarization: Compress old history

References:
- https://mem0.ai/blog/llm-chat-history-summarization-guide-2025
- https://www.getmaxim.ai/articles/context-window-management-strategies
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Any

import tiktoken
from ai_gateway_core.logging import get_logger

logger = get_logger(__name__)


@dataclass
class ContextConfig:
    """Configuration for context management."""

    # Sliding window settings
    max_messages: int = 30  # Keep last N messages in full
    min_recent_messages: int = 6  # Always keep at least N recent messages

    # Token budget settings
    max_context_tokens: int = 8000  # Default token budget for history
    reserved_tokens: int = 2000  # Reserved for system prompt + KB context + response

    # Summarization settings (optional)
    enable_summarization: bool = False  # Enable LLM-based summarization
    summarize_threshold: int = 20  # Trigger summarization after N messages
    summary_max_tokens: int = 500  # Max tokens for summary


@dataclass
class ContextResult:
    """Result of context processing."""

    messages: list[dict[str, str]]  # Processed message history
    summary: str | None = None  # Summary of older messages (if enabled)
    total_tokens: int = 0  # Estimated token count
    truncated_count: int = 0  # Number of messages truncated
    original_count: int = 0  # Original message count


class ContextManager:
    """
    Manages conversation context with intelligent truncation.

    Strategies (from research):
    1. Sliding Window: Keep recent messages, drop older ones
    2. Token-aware: Truncate based on model's context limit
    3. Summarization: Compress older messages (optional, higher latency)

    Best practices:
    - Keep 85% or less of model's context window for best performance
    - Recent messages in full detail, older messages can be summarized
    - Always preserve system prompt and most recent exchanges
    """

    def __init__(
        self,
        model_registry: Any | None = None,  # For summarization
        default_config: ContextConfig | None = None,
    ):
        self.model_registry = model_registry
        self.default_config = default_config or ContextConfig()

        # Try to load tiktoken for accurate token counting
        try:
            self._encoder = tiktoken.get_encoding("cl100k_base")  # GPT-4 encoding
        except Exception:
            self._encoder = None
            logger.warning("tiktoken not available, using approximate token counting")

    def process_history(
        self,
        history: list[dict[str, str]],
        model_context_window: int = 128000,
        system_prompt_tokens: int = 0,
        kb_context_tokens: int = 0,
        config: ContextConfig | None = None,
    ) -> ContextResult:
        """
        Process conversation history to fit within context limits.

        Strategy:
        1. Calculate available token budget
        2. Apply sliding window (keep last N messages)
        3. Token-aware truncation if still over budget

        Args:
            history: Full conversation history
            model_context_window: Model's max context size
            system_prompt_tokens: Tokens used by system prompt
            kb_context_tokens: Tokens used by KB context
            config: Custom context configuration

        Returns:
            ContextResult with processed messages
        """
        config = config or self.default_config
        original_count = len(history)

        if not history:
            return ContextResult(
                messages=[],
                original_count=0,
                truncated_count=0,
                total_tokens=0,
            )

        # Step 1: Calculate available token budget
        # Best practice: Use 85% of context window for optimal performance
        effective_window = int(model_context_window * 0.85)
        available_tokens = (
            effective_window - system_prompt_tokens - kb_context_tokens - config.reserved_tokens
        )
        # Clamp to reasonable bounds: at least 0, at most max_context_tokens
        available_tokens = max(0, min(available_tokens, config.max_context_tokens))

        logger.debug(
            f"Context budget: effective_window={effective_window}, "
            f"available_for_history={available_tokens}, "
            f"history_count={original_count}"
        )

        # Step 2: Apply sliding window
        messages = (
            history[-config.max_messages :] if len(history) > config.max_messages else list(history)
        )

        # Step 3: Token-aware truncation
        messages, total_tokens = self._truncate_by_tokens(
            messages=messages,
            max_tokens=available_tokens,
            min_messages=config.min_recent_messages,
        )

        truncated_count = original_count - len(messages)

        if truncated_count > 0:
            logger.info(
                f"Context truncated: {original_count} -> {len(messages)} messages, "
                f"tokens={total_tokens}/{available_tokens}"
            )

        return ContextResult(
            messages=messages,
            original_count=original_count,
            truncated_count=truncated_count,
            total_tokens=total_tokens,
        )

    def _truncate_by_tokens(
        self,
        messages: list[dict[str, str]],
        max_tokens: int,
        min_messages: int = 6,
    ) -> tuple[list[dict[str, str]], int]:
        """
        Truncate messages to fit within token budget.

        Always keeps at least min_messages recent messages.
        Removes oldest messages first until budget is satisfied.
        """
        if not messages:
            return [], 0

        # Calculate tokens for each message
        message_tokens = []
        for msg in messages:
            tokens = self._count_tokens(msg.get("content", ""))
            # Add overhead for role prefix (~4 tokens)
            tokens += 4
            message_tokens.append(tokens)

        total_tokens = sum(message_tokens)

        # If within budget, return as-is
        if total_tokens <= max_tokens:
            return messages, total_tokens

        # Need to truncate - remove oldest messages first
        # But always keep at least min_messages recent ones
        truncated = list(messages)
        truncated_token_counts = list(message_tokens)

        while len(truncated) > min_messages and total_tokens > max_tokens:
            # Remove oldest message
            removed_tokens = truncated_token_counts.pop(0)
            truncated.pop(0)
            total_tokens -= removed_tokens

        return truncated, total_tokens

    def _count_tokens(self, text: str) -> int:
        """Count tokens in text."""
        if not text:
            return 0

        if self._encoder:
            return len(self._encoder.encode(text))

        # Approximate: ~4 characters per token for English
        # ~2 characters per token for Chinese
        # Use conservative estimate
        return len(text) // 3

    def estimate_tokens(self, messages: list[dict[str, str]]) -> int:
        """Estimate total tokens for a message list."""
        total = 0
        for msg in messages:
            total += self._count_tokens(msg.get("content", "")) + 4
        return total


# Global instance for convenience
_context_manager: ContextManager | None = None


def get_context_manager() -> ContextManager:
    """Get or create the global context manager."""
    global _context_manager
    if _context_manager is None:
        _context_manager = ContextManager()
    return _context_manager
