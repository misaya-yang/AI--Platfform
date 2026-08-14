"""Bounded context-management configuration for the Assistant service.

Only ``ContextConfig`` remains; the ``ContextManager``/``ContextResult``
history-management classes were superseded by the streaming context engine
(``core.rag.context_engine``) and removed with the RAG analyzer cluster.
"""

from __future__ import annotations

from dataclasses import dataclass


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
