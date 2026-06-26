"""
Intelligent Context Compression for Enterprise AI Assistant.

This module implements context compression following Manus principles:
- Preserve structure (URLs, code, tables) over prose
- Maintain recoverability
- Keep recent messages intact

The ContextCompressor reduces token usage while preserving critical information
by extracting and maintaining structural elements (URLs, code blocks, JSON, tables)
and generating concise summaries of compressed content.

Usage:
    ```python
    compressor = ContextCompressor(llm_service, max_summary_tokens=500)

    compressed = await compressor.compress(
        messages=conversation_history,
        target_tokens=4000,
        preserve_recent=6,
    )

    # Access compressed context
    print(compressed.summary)
    print(compressed.preserved_urls)
    print(compressed.preserved_code_blocks)
    print(compressed.token_count)
    ```
"""

from __future__ import annotations

import logging
import re
from dataclasses import dataclass, field
from typing import TYPE_CHECKING, Any, Protocol, runtime_checkable

# =============================================================================
# Preserve Patterns
# =============================================================================

# Patterns for content that should be preserved during compression.
# These structural elements carry more information density than prose.
PRESERVE_PATTERNS: dict[str, str] = {
    "urls": r'https?://[^\s<>"{}|\\^`\[\]]+',
    "code_blocks": r"```[\s\S]*?```",
    "tables": r"\|[^\n]+\|[\n\r]+\|[-:| ]+\|[\s\S]*?(?=\n\n|\Z)",
    "json": r"\{[\s\S]*?\}",
}

# Pattern for extracting artifact references
ARTIFACT_PATTERN: str = r"artifact[_-]?(?:id)?[:\s]*([a-zA-Z0-9_-]+)"

# Maximum limits for preserved content to prevent memory bloat
MAX_PRESERVED_URLS: int = 20
MAX_PRESERVED_CODE_BLOCKS: int = 5


# =============================================================================
# Protocol Definition
# =============================================================================


@runtime_checkable
class LLMService(Protocol):
    """
    Protocol for LLM service interface.

    This protocol defines the minimal interface required for generating
    summaries during context compression. Implementations should provide
    an async completion method.
    """

    async def complete(self, prompt: str, max_tokens: int = 200) -> str:
        """
        Generate a completion for the given prompt.

        Args:
            prompt: The input prompt to complete
            max_tokens: Maximum number of tokens to generate

        Returns:
            The generated completion text
        """
        ...


# =============================================================================
# Data Classes
# =============================================================================


@dataclass
class CompressedContext:
    """
    Container for compressed conversation context.

    Holds the compressed representation of a conversation history,
    including a summary of compressed content, preserved structural
    elements, and recent messages kept intact.

    Attributes:
        summary: Concise summary of the compressed messages
        preserved_urls: List of URLs extracted from compressed messages
        preserved_code_blocks: List of code blocks extracted from compressed messages
        key_artifacts: List of artifact IDs/names referenced in the conversation
        recent_messages: List of recent messages preserved in full
        token_count: Estimated token count of the compressed context
    """

    summary: str
    preserved_urls: list[str] = field(default_factory=list)
    preserved_code_blocks: list[str] = field(default_factory=list)
    key_artifacts: list[str] = field(default_factory=list)
    recent_messages: list[dict[str, Any]] = field(default_factory=list)
    token_count: int = 0


# =============================================================================
# ContextCompressor Class
# =============================================================================


class ContextCompressor:
    """
    Intelligent context compression for conversation history.

    Compresses conversation history while preserving critical structural
    elements and recent context. Follows Manus principles:
    - Preserve structure (URLs, code, tables) over prose
    - Maintain recoverability through extracted elements
    - Keep recent messages intact for context continuity

    Attributes:
        llm_service: LLM service for generating summaries
        max_summary_tokens: Maximum tokens for generated summaries
    """

    def __init__(
        self,
        llm_service: LLMService,
        max_summary_tokens: int = 500,
    ) -> None:
        """
        Initialize the context compressor.

        Args:
            llm_service: LLM service for generating summaries
            max_summary_tokens: Maximum tokens for generated summaries (default: 500)
        """
        self.llm_service = llm_service
        self.max_summary_tokens = max_summary_tokens

    async def compress(
        self,
        messages: list[dict[str, Any]],
        target_tokens: int,
        preserve_recent: int = 6,
    ) -> CompressedContext:
        """
        Compress conversation messages to fit within target token count.

        Preserves the most recent messages intact while compressing older
        messages into a summary with extracted structural elements.

        Args:
            messages: List of conversation messages to compress
            target_tokens: Soft ceiling for the compressed context. Caps the
                summary-generation budget (clamped to at least 100 tokens so the
                summary stays useful even under aggressive budgets).
            preserve_recent: Number of recent messages to keep intact (default: 6)

        Returns:
            CompressedContext containing summary, preserved elements, and recent messages
        """
        # target_tokens caps the summary budget for this call. Floor at 100
        # tokens — a summary shorter than that is worthless.
        effective_summary_tokens = max(100, min(self.max_summary_tokens, target_tokens))

        if not messages:
            return CompressedContext(
                summary="",
                preserved_urls=[],
                preserved_code_blocks=[],
                key_artifacts=[],
                recent_messages=[],
                token_count=0,
            )

        # Split messages into those to compress and those to preserve
        if len(messages) <= preserve_recent:
            # Not enough messages to compress, keep all
            return CompressedContext(
                summary="",
                preserved_urls=[],
                preserved_code_blocks=[],
                key_artifacts=[],
                recent_messages=messages.copy(),
                token_count=self._count_tokens(messages),
            )

        messages_to_compress = messages[:-preserve_recent]
        recent_messages = messages[-preserve_recent:]

        # Extract preservable content from messages to compress
        preserved_urls = self._extract_all(messages_to_compress, "urls")
        preserved_code_blocks = self._extract_all(messages_to_compress, "code_blocks")
        key_artifacts = self._extract_artifacts(messages_to_compress)

        # Apply limits to preserved content
        preserved_urls = preserved_urls[:MAX_PRESERVED_URLS]
        preserved_code_blocks = preserved_code_blocks[:MAX_PRESERVED_CODE_BLOCKS]

        # Generate summary of compressed messages
        summary = await self._generate_summary(
            messages_to_compress, budget_tokens=effective_summary_tokens
        )

        # Calculate total token count
        token_count = self._estimate_compressed_tokens(
            summary=summary,
            urls=preserved_urls,
            code_blocks=preserved_code_blocks,
            recent_messages=recent_messages,
        )

        return CompressedContext(
            summary=summary,
            preserved_urls=preserved_urls,
            preserved_code_blocks=preserved_code_blocks,
            key_artifacts=key_artifacts,
            recent_messages=recent_messages,
            token_count=token_count,
        )

    def _extract_all(
        self,
        messages: list[dict[str, Any]],
        pattern_name: str,
    ) -> list[str]:
        """
        Extract all matches of a pattern from messages.

        Searches through all message content and extracts matches
        for the specified pattern type.

        Args:
            messages: List of messages to search
            pattern_name: Name of the pattern to extract ("urls", "code_blocks", "tables", "json")

        Returns:
            List of unique extracted matches
        """
        if pattern_name not in PRESERVE_PATTERNS:
            return []

        pattern = PRESERVE_PATTERNS[pattern_name]
        matches: list[str] = []
        seen: set[str] = set()

        for message in messages:
            content = self._get_message_content(message)
            if not content:
                continue

            found = re.findall(pattern, content, re.IGNORECASE)
            for match in found:
                # Normalize match for deduplication
                normalized = match.strip()
                if normalized and normalized not in seen:
                    seen.add(normalized)
                    matches.append(normalized)

        return matches

    def _extract_artifacts(self, messages: list[dict[str, Any]]) -> list[str]:
        """
        Extract artifact IDs/names from messages.

        Searches for artifact references using the artifact pattern
        and returns unique artifact identifiers.

        Args:
            messages: List of messages to search

        Returns:
            List of unique artifact IDs/names
        """
        artifacts: list[str] = []
        seen: set[str] = set()

        for message in messages:
            content = self._get_message_content(message)
            if not content:
                continue

            found = re.findall(ARTIFACT_PATTERN, content, re.IGNORECASE)
            for match in found:
                normalized = match.strip()
                if normalized and normalized not in seen:
                    seen.add(normalized)
                    artifacts.append(normalized)

        return artifacts

    async def _generate_summary(
        self, messages: list[dict[str, Any]], budget_tokens: int | None = None
    ) -> str:
        """
        Generate a concise summary of compressed messages using LLM.

        Creates a summary focusing on tasks attempted, results obtained,
        and decisions made during the conversation.

        Args:
            messages: List of messages to summarize
            budget_tokens: Optional per-call max_tokens ceiling (defaults to
                the compressor's `max_summary_tokens`).

        Returns:
            Generated summary text
        """
        if not messages:
            return ""

        # Build conversation text for summarization
        conversation_parts: list[str] = []
        for message in messages:
            role = message.get("role", "unknown")
            content = self._get_message_content(message)
            if content:
                # Truncate very long messages to avoid overwhelming the summarizer
                if len(content) > 1000:
                    content = content[:1000] + "..."
                conversation_parts.append(f"{role}: {content}")

        conversation_text = "\n".join(conversation_parts)

        # Truncate if conversation is too long
        max_input_chars = 8000  # Roughly 2000 tokens
        if len(conversation_text) > max_input_chars:
            conversation_text = conversation_text[:max_input_chars] + "\n[truncated]"

        prompt = f"""Summarize this conversation in 2-3 sentences. Focus on:
- Tasks attempted
- Results obtained
- Decisions made

Conversation:
{conversation_text}

Summary:"""

        try:
            summary = await self.llm_service.complete(
                prompt=prompt,
                max_tokens=budget_tokens or self.max_summary_tokens,
            )
            return summary.strip()
        except Exception:
            # Fallback to simple summary if LLM fails
            message_count = len(messages)
            return f"Previous conversation context ({message_count} messages compressed)."

    def _count_tokens(self, messages: list[dict[str, Any]]) -> int:
        """
        Estimate token count for a list of messages.

        Uses a rough estimate of 4 characters per token.

        Args:
            messages: List of messages to count tokens for

        Returns:
            Estimated token count
        """
        total_chars = 0
        for message in messages:
            content = self._get_message_content(message)
            if content:
                total_chars += len(content)
            # Add some overhead for message structure (role, etc.)
            total_chars += 20

        # Rough estimate: 4 characters per token
        return total_chars // 4

    def _estimate_compressed_tokens(
        self,
        summary: str,
        urls: list[str],
        code_blocks: list[str],
        recent_messages: list[dict[str, Any]],
    ) -> int:
        """
        Estimate total tokens after compression.

        Calculates the approximate token count for the compressed
        context including summary, preserved elements, and recent messages.

        Args:
            summary: Generated summary text
            urls: List of preserved URLs
            code_blocks: List of preserved code blocks
            recent_messages: List of recent messages kept intact

        Returns:
            Estimated total token count
        """
        total_chars = 0

        # Count summary
        total_chars += len(summary)

        # Count URLs
        for url in urls:
            total_chars += len(url)

        # Count code blocks
        for code in code_blocks:
            total_chars += len(code)

        # Count recent messages
        for message in recent_messages:
            content = self._get_message_content(message)
            if content:
                total_chars += len(content)
            total_chars += 20  # Overhead for message structure

        # Add overhead for formatting and structure
        total_chars += 100

        # Rough estimate: 4 characters per token
        return total_chars // 4

    def _get_message_content(self, message: dict[str, Any]) -> str:
        """
        Extract text content from a message.

        Handles different message formats including simple string content
        and complex content structures (like lists of content blocks).

        Args:
            message: Message dictionary

        Returns:
            Extracted text content, or empty string if not found
        """
        content = message.get("content", "")

        if isinstance(content, str):
            return content

        # Handle complex content structures (e.g., list of content blocks)
        if isinstance(content, list):
            text_parts: list[str] = []
            for item in content:
                if isinstance(item, str):
                    text_parts.append(item)
                elif isinstance(item, dict):
                    # Handle content blocks like {"type": "text", "text": "..."}
                    if item.get("type") == "text":
                        text_parts.append(item.get("text", ""))
                    elif "text" in item:
                        text_parts.append(item["text"])
            return " ".join(text_parts)

        return ""


# =============================================================================
# LLM Adapter — concrete LLMService backed by ModelRegistry
# =============================================================================

if TYPE_CHECKING:
    from ..models.model_registry import ModelRegistry

_compressor_logger = logging.getLogger(__name__)


class ModelRegistryLLMService:
    """LLMService impl wrapping ModelRegistry.chat().

    Concrete counterpart to the `LLMService` Protocol in this module; keeps
    compressor callers from having to construct their own adapter."""

    def __init__(
        self,
        model_registry: ModelRegistry,
        model_id: str,
        max_tokens: int = 500,
        temperature: float = 0.3,
    ) -> None:
        self._registry = model_registry
        self._model_id = model_id
        self._max_tokens = max_tokens
        self._temperature = temperature

    async def complete(self, prompt: str, max_tokens: int = 200) -> str:
        effective_max = min(max_tokens or self._max_tokens, self._max_tokens)
        try:
            content, _usage = await self._registry.chat(
                model_id=self._model_id,
                messages=[{"role": "user", "content": prompt}],
                temperature=self._temperature,
                max_tokens=effective_max,
            )
            return content or ""
        except Exception:
            _compressor_logger.exception("ModelRegistryLLMService.complete failed")
            return ""
