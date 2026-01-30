"""
Contextual Retrieval - Anthropic's approach to reduce retrieval failures by 67%.

Before embedding each chunk, prepends a 50-100 token context summary that
situates the chunk within the full document. This resolves ambiguity issues
like anaphoric references ("it", "the company") that traditional chunking breaks.

Two strategies:
1. Template-based prefix (for Islamic texts with structured metadata - zero LLM cost)
2. LLM-based prefix (for general content - requires LLM call)

The contextual prefix is embedded WITH the text for semantic search, but stored
separately so display/citation uses the original text.
"""

from __future__ import annotations

from typing import Any, Dict, Optional

from .islamic_chunking import IslamicSourceType
from .islamic_metadata import SURAH_NAMES


class ContextualRetrieval:
    """Generate contextual prefixes for chunks to improve retrieval quality."""

    async def generate_context_prefix(
        self,
        chunk_text: str,
        document_text: str,
        document_metadata: Optional[Dict[str, Any]] = None,
        chunk_metadata: Optional[Dict[str, Any]] = None,
    ) -> str:
        """Generate a contextual prefix for a chunk.

        Strategy:
        1. If Islamic metadata available (source_type, surah, etc.),
           use deterministic template-based prefix (zero cost).
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

        # Strategy 1: Template-based for Islamic content
        source_type = chunk_meta.get("source_type") or chunk_meta.get("islamic_source_type")
        if source_type and source_type != "unknown":
            prefix = self._template_based_prefix(chunk_meta, doc_meta)
            if prefix:
                return prefix

        # Strategy 2: Document context fallback
        return self._document_context_prefix(chunk_meta, doc_meta)

    def _template_based_prefix(
        self,
        chunk_meta: Dict[str, Any],
        doc_meta: Dict[str, Any],
    ) -> str:
        """Deterministic template prefix for Islamic content (zero LLM cost)."""
        source_type = chunk_meta.get("source_type") or chunk_meta.get("islamic_source_type", "")
        source_ref = chunk_meta.get("source_reference", {})
        doc_title = doc_meta.get("title") or doc_meta.get("name") or ""

        if source_type == IslamicSourceType.QURAN.value or source_type == "quran":
            surah = source_ref.get("surah")
            try:
                surah_name = source_ref.get("surah_name") or (SURAH_NAMES.get(int(surah), "") if surah else "")
            except (ValueError, TypeError):
                surah_name = source_ref.get("surah_name", "")
            verse_start = source_ref.get("verse_start")
            verse_end = source_ref.get("verse_end")

            if surah and verse_start:
                verse_info = f"verse {verse_start}"
                if verse_end and verse_end != verse_start:
                    verse_info = f"verses {verse_start}-{verse_end}"
                base = f"This passage is from Surah {surah_name} ({surah}), {verse_info}. "
                if doc_title:
                    base += f"Source: {doc_title}. "
                return base
            if doc_title:
                return f"This is a Quranic text from {doc_title}. "
            return "This is a Quranic text. "

        elif source_type == IslamicSourceType.HADITH.value or source_type == "hadith":
            collection = source_ref.get("collection", "")
            book = source_ref.get("book")
            hadith_num = source_ref.get("hadith_number")
            narrator = source_ref.get("narrator", "")

            parts = []
            if collection:
                parts.append(f"from {collection}")
            if book:
                parts.append(f"Book {book}")
            if narrator:
                parts.append(f"narrated by {narrator}")

            detail = ", ".join(parts) if parts else f"from {doc_title}" if doc_title else ""
            if detail:
                return f"This hadith {detail}. "
            return "This is a Hadith narration. "

        elif source_type == IslamicSourceType.TAFSEER.value or source_type == "tafseer":
            author = source_ref.get("author", "")
            surah = source_ref.get("surah")
            verse = source_ref.get("verse")

            parts = []
            if author:
                parts.append(f"from Tafsir {author}")
            if surah:
                try:
                    surah_name = source_ref.get("surah_name") or SURAH_NAMES.get(int(surah), "")
                except (ValueError, TypeError):
                    surah_name = source_ref.get("surah_name", "")
                parts.append(f"commenting on Surah {surah_name or surah}")
            if verse:
                parts.append(f"verse {verse}")

            detail = ", ".join(parts) if parts else f"from {doc_title}" if doc_title else ""
            if detail:
                return f"This tafseer commentary {detail}. "
            return "This is Tafseer (Quran commentary). "

        elif source_type == IslamicSourceType.FIQH.value or source_type == "fiqh":
            school = source_ref.get("school", "")
            topic = source_ref.get("topic", "")

            parts = []
            if school:
                parts.append(f"from the {school.title()} school of Islamic jurisprudence")
            if topic:
                parts.append(f"discussing {topic}")
            if doc_title and not parts:
                parts.append(f"from {doc_title}")

            detail = ", ".join(parts) if parts else ""
            if detail:
                return f"This Islamic legal ruling {detail}. "
            return "This is Islamic jurisprudence (Fiqh). "

        elif source_type in (IslamicSourceType.GENERAL_ISLAMIC.value, "general_islamic"):
            if doc_title:
                return f"This passage is from the Islamic text '{doc_title}'. "
            return "This is Islamic scholarly text. "

        return ""

    def _document_context_prefix(
        self,
        chunk_meta: Dict[str, Any],
        doc_meta: Dict[str, Any],
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
