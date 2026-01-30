"""
Citation Formatter for Wahda AI-Imam Knowledge Base

Formats citations according to the Wahda AI-Imam specification:
- Authority ordering: Quran (1) > Hadith (2) > Tafseer (3) > Fiqh (4) > Others (5+)
- Custom citation format per source type
- Grouped citation blocks for multi-source answers

Citation formats:
- Quran: "Quran [Chapter]:[Verse] - [Translation]"
- Hadith: "Sahih Bukhari, Book [X], Hadith [X]"
- Tafseer: "Tafsir Ibn Kathir, Surah [X], Verse [X]"
- Fiqh: "Islamic Jurisprudence According to the Four Sunni Schools, [School], [Topic]"
"""

from __future__ import annotations

import json
from typing import Any, Dict, List, Optional

from .islamic_metadata import get_authority_order, IslamicMetadataExtractor


def _parse_metadata(segment: Dict[str, Any]) -> Dict[str, Any]:
    """Parse segment metadata, handling both dict and JSON string formats."""
    metadata = segment.get("metadata", {})
    if isinstance(metadata, str):
        try:
            metadata = json.loads(metadata)
        except (json.JSONDecodeError, TypeError):
            metadata = {}
    return metadata


def _parse_json_field(value: Any) -> Any:
    """Parse a JSON string field, returning {} on failure."""
    if isinstance(value, str):
        try:
            return json.loads(value)
        except (json.JSONDecodeError, TypeError):
            return {}
    return value


def _get_source_type(segment: Dict[str, Any], metadata: Dict[str, Any]) -> str:
    """Extract source_type from segment or its metadata."""
    return (
        segment.get("source_type")
        or metadata.get("source_type")
        or metadata.get("islamic_source_type")
        or "unknown"
    )


class CitationFormatter:
    """Format citations for Islamic knowledge retrieval results."""

    def __init__(self):
        self._extractor = IslamicMetadataExtractor()

    def format_citation(self, segment: Dict[str, Any]) -> Optional[str]:
        """Format a single citation from segment data.

        Uses pre-computed citation_text if available, otherwise generates from metadata.
        
        Returns:
            Formatted citation string, or None if citation cannot be generated.
        """
        # Use pre-computed citation if available and non-empty
        citation = segment.get("citation_text")
        if citation and str(citation).strip():
            return str(citation).strip()

        # Generate from metadata
        metadata = _parse_metadata(segment)
        source_type_str = _get_source_type(segment, metadata)
        source_ref = _parse_json_field(
            segment.get("source_reference")
            or metadata.get("source_reference")
            or {}
        )

        from .islamic_chunking import IslamicSourceType
        try:
            source_type = IslamicSourceType(source_type_str)
        except ValueError:
            source_type = IslamicSourceType.UNKNOWN

        result = self._extractor.format_citation(source_type, source_ref)
        # Return None for empty/whitespace-only results
        if result and str(result).strip():
            return str(result).strip()
        return None

    def format_citation_block(self, segments: List[Dict[str, Any]]) -> str:
        """Format a complete citation block from multiple segments.

        Returns a markdown-formatted citation block sorted by authority.
        """
        sorted_segments = self.sort_by_authority(segments)

        citations = []
        seen = set()
        for seg in sorted_segments:
            citation = self.format_citation(seg)
            if citation and citation not in seen:
                seen.add(citation)
                citations.append(citation)

        if not citations:
            return ""

        lines = ["**Sources:**"]
        for i, citation in enumerate(citations, 1):
            lines.append(f"- {citation}")

        return "\n".join(lines)

    def sort_by_authority(self, segments: List[Dict[str, Any]]) -> List[Dict[str, Any]]:
        """Sort segments by Islamic authority order.

        Quran (1) > Hadith (2) > Tafseer (3) > Fiqh (4) > Others (5+)
        """
        def sort_key(seg: Dict[str, Any]) -> int:
            metadata = _parse_metadata(seg)
            source_type = _get_source_type(seg, metadata)
            return get_authority_order(source_type)

        return sorted(segments, key=sort_key)

    def group_by_source(self, segments: List[Dict[str, Any]]) -> Dict[str, List[Dict[str, Any]]]:
        """Group segments by source type for structured display."""
        groups: Dict[str, List[Dict[str, Any]]] = {}
        for seg in segments:
            metadata = _parse_metadata(seg)
            source_type = _get_source_type(seg, metadata)
            groups.setdefault(source_type, []).append(seg)
        return groups

    def enrich_results_with_citations(
        self, results: List[Dict[str, Any]]
    ) -> List[Dict[str, Any]]:
        """Enrich retrieval results with citation information.

        Adds citation_text, source_type to results that don't already have them.
        Returns results sorted by authority order.
        """
        for result in results:
            if not result.get("citation_text"):
                result["citation_text"] = self.format_citation(result)
            if not result.get("source_type"):
                metadata = _parse_metadata(result)
                result["source_type"] = _get_source_type(result, metadata)

        return self.sort_by_authority(results)
