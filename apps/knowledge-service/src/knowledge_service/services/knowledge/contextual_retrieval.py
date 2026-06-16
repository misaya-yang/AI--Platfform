"""
Contextual Retrieval - Anthropic's approach to reduce retrieval failures by 67%.

Before embedding each chunk, prepends a 50-100 token context summary that
situates the chunk within the full document. This resolves ambiguity issues
like anaphoric references ("it", "the company") that traditional chunking breaks.

Two strategies:
1. Template-based prefix (for chunks with structured source metadata - zero LLM cost)
2. LLM-based prefix (for general content - requires LLM call)

The contextual prefix is embedded WITH the text for semantic search, but stored
separately so display/citation uses the original text.
"""

from __future__ import annotations

from typing import Any


class ContextualRetrieval:
    """Generate contextual prefixes for chunks to improve retrieval quality."""

    async def generate_context_prefix(
        self,
        chunk_text: str,
        document_text: str,
        document_metadata: dict[str, Any] | None = None,
        chunk_metadata: dict[str, Any] | None = None,
    ) -> str:
        """Generate a contextual prefix for a chunk.

        Strategy:
        1. If structured source metadata is available, use deterministic
           template-based prefix (zero cost).
        2. Otherwise, use document title + section header as prefix.

        Args:
            chunk_text: The chunk's text content
            document_text: Full document text (for context)
            document_metadata: Document-level metadata (title, etc.)
            chunk_metadata: Chunk-level metadata (source_type, etc.)

        Returns:
            A contextual prefix string (50-100 tokens).
        """
        doc_meta = document_metadata or {}
        chunk_meta = chunk_metadata or {}

        # Strategy 1: Template-based for structured source metadata.
        source_type = chunk_meta.get("source_type")
        if source_type and source_type != "unknown":
            prefix = self._template_based_prefix(chunk_meta, doc_meta)
            if prefix:
                return prefix

        # Strategy 2: Document context fallback
        return self._document_context_prefix(chunk_meta, doc_meta)

    def _template_based_prefix(
        self,
        chunk_meta: dict[str, Any],
        doc_meta: dict[str, Any],
    ) -> str:
        """Deterministic template prefix for structured source metadata."""
        source_type = str(chunk_meta.get("source_type") or "").strip()
        source_ref = chunk_meta.get("source_reference", {})
        doc_title = doc_meta.get("title") or doc_meta.get("name") or ""

        parts: list[str] = []
        if source_type:
            parts.append(f"source type: {source_type}")
        if doc_title:
            parts.append(f"document: {doc_title}")
        if isinstance(source_ref, dict):
            for key in ("page", "page_number", "section", "title", "url"):
                value = source_ref.get(key)
                if value:
                    parts.append(f"{key}: {value}")

        if parts:
            return "This passage is from " + ", ".join(parts) + ". "

        return ""

    def _document_context_prefix(
        self,
        chunk_meta: dict[str, Any],
        doc_meta: dict[str, Any],
    ) -> str:
        """Fallback: use document title and section header."""
        doc_title = doc_meta.get("title") or doc_meta.get("name") or ""
        section = chunk_meta.get("section_header") or chunk_meta.get("heading") or ""

        parts = []
        if doc_title:
            parts.append(f"From document '{doc_title}'")
        if section:
            parts.append(f"section: {section}")

        if parts:
            return ". ".join(parts) + ". "
        return ""
