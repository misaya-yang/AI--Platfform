"""
StreamingWriter - Write-While-Search Pattern Implementation.

This module enables streaming text output with inline tool calls for fact-checking
against the knowledge base. When verification triggers are detected in the text,
it performs KB searches and streams the results back to the client interleaved
with the text output.

Key Features:
- Streaming text generation with trigger detection
- Inline KB searches when verification phrases are found
- Bilingual support (English and Chinese triggers)
- Buffer-based streaming for efficient output

Usage:
    from src.services.assistant.streaming_writer import StreamingWriter, StreamChunk

    writer = StreamingWriter(kb_service=kb_service, assistant_service=assistant_service)

    async for chunk in writer.write_with_verification(
        writing_prompt="Write about our refund policy",
        dataset_ids=["policy_docs"],
    ):
        if chunk.type == "text":
            print(chunk.content, end="")
        elif chunk.type == "search_result":
            print(f"[Verified: {chunk.metadata}]")
"""

from __future__ import annotations

import re
import time
from collections.abc import AsyncGenerator
from dataclasses import dataclass, field
from typing import TYPE_CHECKING, Any

from ....core.observability.logging import get_logger

if TYPE_CHECKING:
    from ....core.auth.user_resolver import UserContext
    from ...knowledge.knowledge_service import KnowledgeService
    from .assistant_service import AssistantService

logger = get_logger(__name__)


# Default verification triggers for fact-checking
# Includes common phrases in English and Chinese that indicate factual claims
DEFAULT_VERIFICATION_TRIGGERS = [
    # English triggers
    "according to",
    "policy states",
    "based on",
    "as per",
    "as stated in",
    "per the",
    "the policy",
    "our policy",
    "company policy",
    "the guidelines",
    # Chinese triggers
    "根据",
    "依据",
    "规定",
    "政策",
    "按照",
    "遵照",
    "依照",
    "根据规定",
    "政策规定",
    "文件规定",
]


@dataclass
class StreamChunk:
    """
    Represents a chunk of data in the streaming output.

    The StreamingWriter emits these chunks during the write-while-search process.
    Each chunk has a type that indicates what kind of data it contains.

    Attributes:
        type: The chunk type, one of:
            - "text": Regular text content to display
            - "search_start": Indicates a KB search is beginning
            - "search_result": Contains KB search results
            - "search_end": Indicates a KB search has completed
            - "error": Contains error information
        content: The main content of the chunk (text, search query, results, etc.)
        metadata: Optional additional data (scores, timing, source info, etc.)

    Examples:
        >>> chunk = StreamChunk(type="text", content="According to ")
        >>> chunk = StreamChunk(
        ...     type="search_result",
        ...     content="The refund policy allows...",
        ...     metadata={"score": 0.95, "source": "policy_doc_v2"}
        ... )
    """

    type: str  # "text", "search_start", "search_result", "search_end", "error"
    content: str
    metadata: dict[str, Any] | None = None

    def __post_init__(self):
        """Validate chunk type."""
        valid_types = {"text", "search_start", "search_result", "search_end", "error"}
        if self.type not in valid_types:
            raise ValueError(f"Invalid chunk type: {self.type}. Must be one of {valid_types}")


@dataclass
class VerificationContext:
    """
    Internal context for tracking verification state during streaming.

    Attributes:
        trigger: The trigger phrase that was detected
        trigger_position: Position in the text where trigger was found
        query: The extracted verification query
        results: KB search results
        verified: Whether verification was successful
    """

    trigger: str
    trigger_position: int
    query: str
    results: list[dict[str, Any]] = field(default_factory=list)
    verified: bool = False


class StreamingWriter:
    """
    Enables write-while-search pattern: streaming text output
    with inline tool calls for fact-checking.

    This class orchestrates the process of generating text while simultaneously
    verifying claims against a knowledge base. When trigger phrases are detected,
    it pauses text output, performs KB searches, and yields search events before
    resuming text output.

    Attributes:
        kb_service: Knowledge base service for retrieval
        assistant_service: Assistant service for text generation
        buffer_threshold: Characters to buffer before yielding text (default: 100)
        search_top_k: Number of KB results to retrieve (default: 3)

    Example:
        >>> writer = StreamingWriter(kb_service, assistant_service)
        >>> async for chunk in writer.write_with_verification(
        ...     writing_prompt="Explain our refund policy",
        ...     dataset_ids=["policies"],
        ... ):
        ...     if chunk.type == "text":
        ...         print(chunk.content, end="", flush=True)
        ...     elif chunk.type == "search_result":
        ...         # Handle verification result
        ...         pass
    """

    def __init__(
        self,
        kb_service: KnowledgeService,
        assistant_service: AssistantService,
        buffer_threshold: int = 100,
        search_top_k: int = 3,
    ):
        """
        Initialize the StreamingWriter.

        Args:
            kb_service: Knowledge base service for fact retrieval
            assistant_service: Assistant service for text generation
            buffer_threshold: Minimum characters to buffer before yielding text.
                             Higher values mean less frequent but larger chunks.
                             Default is 100 characters.
            search_top_k: Number of KB results to retrieve per verification.
                         Default is 3.
        """
        self.kb_service = kb_service
        self.assistant_service = assistant_service
        self.buffer_threshold = buffer_threshold
        self.search_top_k = search_top_k

        logger.debug(
            f"StreamingWriter initialized: buffer_threshold={buffer_threshold}, "
            f"search_top_k={search_top_k}"
        )

    async def write_with_verification(
        self,
        writing_prompt: str,
        dataset_ids: list[str],
        verification_triggers: list[str] | None = None,
        user: UserContext | None = None,
        temperature: float = 0.7,
        max_tokens: int | None = None,
        model_id: str = "gemini-3-flash-preview",
    ) -> AsyncGenerator[StreamChunk, None]:
        """
        Generate text while verifying facts against knowledge base.

        This method streams text output, detecting verification triggers and
        performing KB searches when found. Search events are interleaved with
        text output to provide real-time fact-checking.

        Default triggers include common phrases in English and Chinese:
        - English: "according to", "policy states", "based on", "as per", etc.
        - Chinese: "根据", "依据", "规定", "政策", "按照", etc.

        Args:
            writing_prompt: The prompt for text generation
            dataset_ids: List of KB dataset IDs to search
            verification_triggers: Optional custom trigger phrases. If None,
                                  uses DEFAULT_VERIFICATION_TRIGGERS.
            user: User context for KB access authorization
            temperature: Model temperature for generation (default: 0.7)
            max_tokens: Maximum tokens to generate (None for model default)
            model_id: Model ID to use for generation (default: "gemini-3-flash-preview")

        Yields:
            StreamChunk objects with different types:
            - "text": Regular text content
            - "search_start": KB search beginning (metadata contains query)
            - "search_result": KB results (metadata contains scores, sources)
            - "search_end": KB search completed (metadata contains timing)
            - "error": Error occurred (content contains error message)

        Examples:
            >>> async for chunk in writer.write_with_verification(
            ...     writing_prompt="Explain the refund policy",
            ...     dataset_ids=["policies"],
            ... ):
            ...     if chunk.type == "text":
            ...         print(chunk.content, end="")
            ...     elif chunk.type == "search_start":
            ...         print(f"\\n[Searching: {chunk.metadata['query']}]")
        """
        triggers = verification_triggers or DEFAULT_VERIFICATION_TRIGGERS

        # Normalize triggers for case-insensitive matching
        trigger_patterns = self._compile_trigger_patterns(triggers)

        # Text buffer for accumulating content before yielding
        buffer = ""
        total_text = ""
        search_count = 0
        start_time = time.time()

        try:
            # Get streaming response from assistant
            async for delta in self._generate_text(
                prompt=writing_prompt,
                model_id=model_id,
                temperature=temperature,
                max_tokens=max_tokens,
            ):
                if delta:
                    buffer += delta
                    total_text += delta

                    # Check for verification triggers in the buffer
                    trigger_match = self._find_trigger(buffer, trigger_patterns, triggers)

                    if trigger_match:
                        trigger, position = trigger_match

                        # Yield any text before the trigger
                        if position > 0:
                            yield StreamChunk(
                                type="text",
                                content=buffer[:position],
                            )

                        # Extract verification query from context
                        query = self._extract_verification_query(total_text, trigger)

                        if query and dataset_ids:
                            search_count += 1

                            # Emit search start event
                            yield StreamChunk(
                                type="search_start",
                                content=query,
                                metadata={
                                    "trigger": trigger,
                                    "search_number": search_count,
                                    "dataset_ids": dataset_ids,
                                },
                            )

                            # Perform KB search
                            search_start = time.time()
                            try:
                                results = await self._search_kb(
                                    query=query,
                                    dataset_ids=dataset_ids,
                                    user=user,
                                )

                                # Emit search results
                                if results:
                                    yield StreamChunk(
                                        type="search_result",
                                        content=self._format_results(results),
                                        metadata={
                                            "query": query,
                                            "result_count": len(results),
                                            "results": results,
                                        },
                                    )

                                search_duration_ms = (time.time() - search_start) * 1000

                                # Emit search end event
                                yield StreamChunk(
                                    type="search_end",
                                    content="",
                                    metadata={
                                        "query": query,
                                        "result_count": len(results),
                                        "duration_ms": search_duration_ms,
                                    },
                                )

                            except Exception as e:
                                logger.warning(f"KB search failed: {e}")
                                yield StreamChunk(
                                    type="error",
                                    content=f"Search failed: {str(e)}",
                                    metadata={"query": query, "error_type": type(e).__name__},
                                )

                        # Continue with text after the trigger
                        buffer = buffer[position:]

                    # Yield buffered text when threshold is reached
                    elif len(buffer) > self.buffer_threshold:
                        # Find a safe break point (end of word/sentence)
                        break_point = self._find_safe_break(buffer)
                        if break_point > 0:
                            yield StreamChunk(
                                type="text",
                                content=buffer[:break_point],
                            )
                            buffer = buffer[break_point:]

            # Yield remaining buffer
            if buffer:
                yield StreamChunk(
                    type="text",
                    content=buffer,
                )

            total_duration_ms = (time.time() - start_time) * 1000
            logger.info(
                f"StreamingWriter completed: {len(total_text)} chars, "
                f"{search_count} searches, {total_duration_ms:.1f}ms"
            )

        except Exception as e:
            logger.error(f"StreamingWriter error: {e}", exc_info=True)
            yield StreamChunk(
                type="error",
                content=f"Generation failed: {str(e)}",
                metadata={"error_type": type(e).__name__},
            )

    def _compile_trigger_patterns(self, triggers: list[str]) -> list[re.Pattern]:
        """
        Compile trigger phrases into regex patterns for efficient matching.

        Args:
            triggers: List of trigger phrases

        Returns:
            List of compiled regex patterns (case-insensitive for ASCII)
        """
        patterns = []
        for trigger in triggers:
            # Use word boundaries for English, direct match for Chinese
            if all(ord(c) < 128 for c in trigger):
                # ASCII text - use word boundaries and case-insensitive
                pattern = re.compile(r"\b" + re.escape(trigger) + r"\b", re.IGNORECASE)
            else:
                # Contains non-ASCII (Chinese, etc.) - direct match
                pattern = re.compile(re.escape(trigger))
            patterns.append(pattern)
        return patterns

    def _find_trigger(
        self,
        text: str,
        patterns: list[re.Pattern],
        triggers: list[str],
    ) -> tuple[str, int] | None:
        """
        Find the first verification trigger in the text.

        Args:
            text: Text to search
            patterns: Compiled regex patterns
            triggers: Original trigger strings

        Returns:
            Tuple of (trigger_phrase, position) if found, None otherwise
        """
        first_match = None
        first_position = len(text)

        for pattern, trigger in zip(patterns, triggers, strict=False):
            match = pattern.search(text)
            if match and match.start() < first_position:
                first_match = trigger
                first_position = match.start()

        if first_match is not None:
            return (first_match, first_position)
        return None

    def _extract_verification_query(self, text: str, trigger: str) -> str | None:
        """
        Extract the topic/query around a verification trigger.

        This method analyzes the context around the trigger phrase to determine
        what claim is being made and should be verified against the KB.

        Strategy:
        1. Look at text after the trigger for the subject of the claim
        2. Consider the preceding context for additional scope
        3. Extract a concise query suitable for KB search

        Args:
            text: Full text up to the current point
            trigger: The trigger phrase that was detected

        Returns:
            A query string suitable for KB search, or None if extraction fails

        Examples:
            >>> writer._extract_verification_query(
            ...     "Our company's refund policy states",
            ...     "policy states"
            ... )
            "refund policy"
        """
        if not text or not trigger:
            return None

        # Find the trigger position in the text
        trigger_lower = trigger.lower()
        text_lower = text.lower()
        trigger_pos = text_lower.rfind(trigger_lower)

        if trigger_pos == -1:
            # Trigger not found - shouldn't happen but handle gracefully
            return None

        # Extract context before trigger (for subject)
        before_context = text[:trigger_pos].strip()

        # Extract context after trigger (for predicate/claim)
        after_start = trigger_pos + len(trigger)
        after_context = text[after_start:].strip() if after_start < len(text) else ""

        # Build query from context
        query_parts = []

        # Look for the subject before the trigger
        # Take the last sentence or clause before the trigger
        if before_context:
            # Find last sentence boundary
            for sep in [". ", "。", "\n", "; ", ";", "；"]:
                last_sep = before_context.rfind(sep)
                if last_sep != -1:
                    before_context = before_context[last_sep + len(sep) :]
                    break

            # Take last few words as subject context
            words = before_context.split()
            if words:
                # Take up to last 5 words for context
                subject_words = words[-5:] if len(words) > 5 else words
                query_parts.append(" ".join(subject_words))

        # Add key words after trigger (the claim)
        if after_context:
            # Take first sentence/clause after trigger
            end_markers = [". ", "。", "\n", ", ", ",", "，", ";", "；"]
            end_pos = len(after_context)
            for marker in end_markers:
                marker_pos = after_context.find(marker)
                if marker_pos != -1 and marker_pos < end_pos:
                    end_pos = marker_pos

            claim_text = after_context[:end_pos].strip()
            if claim_text:
                # Take up to first 10 words of the claim
                words = claim_text.split()
                claim_words = words[:10] if len(words) > 10 else words
                query_parts.append(" ".join(claim_words))

        if not query_parts:
            return None

        # Combine parts into a search query
        query = " ".join(query_parts)

        # Clean up the query
        query = query.strip()
        query = re.sub(r"\s+", " ", query)  # Normalize whitespace

        # Limit query length
        max_query_length = 200
        if len(query) > max_query_length:
            query = query[:max_query_length].rsplit(" ", 1)[0]

        return query if query else None

    def _format_results(self, results: list[dict[str, Any]]) -> str:
        """
        Format KB results for inline display.

        Formats the search results into a readable string suitable for
        display to the user or injection into the response.

        Args:
            results: List of KB search results with content, score, metadata

        Returns:
            Formatted string representation of the results

        Examples:
            >>> results = [{"content": "Refunds within 30 days", "score": 0.95}]
            >>> writer._format_results(results)
            "[1] (score: 0.95) Refunds within 30 days"
        """
        if not results:
            return "[No relevant results found]"

        formatted_parts = []
        for i, result in enumerate(results, 1):
            content = result.get("content", result.get("text", ""))
            score = result.get("score", 0.0)
            source = result.get("source_url") or result.get("metadata", {}).get("source_url", "")

            # Truncate long content
            max_content_length = 300
            if len(content) > max_content_length:
                content = content[:max_content_length] + "..."

            if source:
                formatted_parts.append(f"[{i}] (score: {score:.2f}) [Source: {source}]\n{content}")
            else:
                formatted_parts.append(f"[{i}] (score: {score:.2f})\n{content}")

        return "\n\n".join(formatted_parts)

    def _find_safe_break(self, text: str) -> int:
        """
        Find a safe position to break the text buffer.

        Looks for word/sentence boundaries to avoid splitting in the middle
        of words or phrases.

        Args:
            text: Text buffer to find break point in

        Returns:
            Position to break at, or 0 if no safe break found
        """
        if len(text) <= self.buffer_threshold:
            return 0

        # Look for sentence endings
        sentence_endings = [". ", "。", "! ", "！", "? ", "？", "\n"]
        for ending in sentence_endings:
            pos = text.rfind(ending, 0, len(text))
            if pos != -1 and pos > self.buffer_threshold // 2:
                return pos + len(ending)

        # Look for word boundaries (space)
        space_pos = text.rfind(" ", 0, len(text))
        if space_pos != -1 and space_pos > self.buffer_threshold // 2:
            return space_pos + 1

        # Fall back to full buffer
        return len(text)

    async def _generate_text(
        self,
        prompt: str,
        model_id: str = "gemini-3-flash-preview",
        temperature: float = 0.7,
        max_tokens: int | None = None,
    ) -> AsyncGenerator[str, None]:
        """
        Generate text using the assistant service.

        This is an internal method that wraps the assistant service's
        streaming capability.

        Args:
            prompt: The prompt for text generation
            model_id: Model ID to use
            temperature: Sampling temperature
            max_tokens: Maximum tokens to generate

        Yields:
            String chunks of generated text
        """
        from ..models.model_registry import ChatMessage

        messages = [ChatMessage(role="user", content=prompt)]

        try:
            async for delta in self.assistant_service.model_registry.chat_stream(
                model_id=model_id,
                messages=messages,
                temperature=temperature,
                max_tokens=max_tokens,
            ):
                if delta.content:
                    yield delta.content
        except Exception as e:
            logger.error(f"Text generation failed: {e}", exc_info=True)
            raise

    async def _search_kb(
        self,
        query: str,
        dataset_ids: list[str],
        user: UserContext | None = None,
    ) -> list[dict[str, Any]]:
        """
        Search the knowledge base for verification.

        Args:
            query: Search query
            dataset_ids: List of dataset IDs to search
            user: User context for authorization

        Returns:
            List of search results as dictionaries
        """
        results = []

        for dataset_id in dataset_ids:
            try:
                # Skip if no user context provided
                if user is None:
                    logger.warning("KB search skipped: no user context provided")
                    continue

                # Call KB service retrieve
                retrieve_results, meta = await self.kb_service.retrieve(
                    user=user,
                    dataset_id=dataset_id,
                    query=query,
                    top_k=self.search_top_k,
                )

                for r in retrieve_results:
                    results.append(
                        {
                            "content": r.text,
                            "score": r.score,
                            "segment_id": r.segment_id,
                            "document_id": r.document_id,
                            "dataset_id": dataset_id,
                            "metadata": r.metadata or {},
                        }
                    )
            except Exception as e:
                logger.warning(f"KB search failed for dataset {dataset_id}: {e}")
                continue

        # Sort by score and limit to top_k
        def get_score(item: dict[str, Any]) -> float:
            score = item.get("score", 0)
            if isinstance(score, (int, float)):
                return float(score)
            return 0.0

        results.sort(key=get_score, reverse=True)
        return results[: self.search_top_k]


def create_streaming_writer(
    kb_service: KnowledgeService,
    assistant_service: AssistantService,
    buffer_threshold: int = 100,
    search_top_k: int = 3,
) -> StreamingWriter:
    """
    Factory function to create a StreamingWriter instance.

    Args:
        kb_service: Knowledge base service for retrieval
        assistant_service: Assistant service for text generation
        buffer_threshold: Characters to buffer before yielding (default: 100)
        search_top_k: Number of KB results per search (default: 3)

    Returns:
        Configured StreamingWriter instance

    Example:
        >>> from src.services.assistant import create_streaming_writer
        >>> writer = create_streaming_writer(kb_service, assistant_service)
    """
    return StreamingWriter(
        kb_service=kb_service,
        assistant_service=assistant_service,
        buffer_threshold=buffer_threshold,
        search_top_k=search_top_k,
    )
