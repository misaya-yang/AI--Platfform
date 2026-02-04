"""
Section Extractor for Strict Chapter Traceability

This module provides enhanced section/chapter extraction for Imam-type datasets,
ensuring every chunk has a traceable section_title in its metadata.

When strict_section_traceability is enabled:
1. Extracts section headings from document structure
2. Associates each chunk with its nearest parent section
3. Inherits section titles for chunks within sections
4. Ensures citation_text includes chapter/section information

Supports both Arabic and English Islamic texts with specialized patterns.
"""

from __future__ import annotations

import re
from dataclasses import dataclass
from typing import Any, Dict, List, Optional, Tuple


@dataclass
class Section:
    """Represents a document section with heading and content range."""
    title: str
    level: int  # Heading level (1 = top-level, 2 = subsection, etc.)
    start_pos: int  # Character position where section starts
    end_pos: Optional[int] = None  # Character position where section ends
    parent: Optional["Section"] = None
    
    @property
    def full_path(self) -> str:
        """Get full section path (e.g., 'Chapter 1 > Section 2 > Subsection A')."""
        if self.parent:
            return f"{self.parent.full_path} > {self.title}"
        return self.title


class SectionExtractor:
    """
    Extracts document sections and enables strict section traceability.
    
    Designed for Islamic/Imam-type texts with support for:
    - Arabic chapter markers (باب, كتاب, فصل, etc.)
    - English chapter/section headings
    - Markdown-style headings
    - TOC (Table of Contents) parsing
    """
    
    # Islamic text section patterns (Arabic)
    ARABIC_SECTION_PATTERNS = [
        # باب (Chapter/Bab) - most common in Islamic texts
        (r'^\s*(?:باب)\s*[:：]?(?:\s+|$)\s*(.+?)(?:\n|$)', 1),
        # كتاب (Book/Kitab) - higher level
        (r'^\s*(?:كتاب)\s*[:：]?(?:\s+|$)\s*(.+?)(?:\n|$)', 0),
        # فصل (Section/Fasl)
        (r'^\s*(?:فصل)\s*[:：]?(?:\s+|$)\s*(.+?)(?:\n|$)', 2),
        # مسألة (Issue/Mas'ala)
        (r'^\s*(?:مسألة|مسائل)\s*[:：]?(?:\s+|$)\s*(.+?)(?:\n|$)', 3),
        # فرع (Branch/Far')
        (r'^\s*(?:فرع)\s*[:：]?(?:\s+|$)\s*(.+?)(?:\n|$)', 4),
    ]
    
    # English section patterns
    ENGLISH_SECTION_PATTERNS = [
        # Book X / Chapter X
        (r'^[\s]*(?:Book|Volume)\s+(?:\d+|[IVX]+)[:：.]?\s*(.*?)(?:\n|$)', 0),
        (r'^[\s]*(?:Chapter)\s+(?:\d+|[A-Z])[:：.]?\s*(.*?)(?:\n|$)', 1),
        # Section X / Part X
        (r'^[\s]*(?:Section|Part)\s+(?:\d+|[A-Z])[:：.]?\s*(.*?)(?:\n|$)', 2),
        # Numbered subsections
        (r'^[\s]*(?:\d+\.\d+)[:：.]?\s*(.*?)(?:\n|$)', 3),
        (r'^[\s]*(?:\d+\.\d+\.\d+)[:：.]?\s*(.*?)(?:\n|$)', 4),
    ]
    
    # Markdown heading patterns
    MARKDOWN_PATTERNS = [
        (r'^#{1}\s+(.+?)(?:\n|$)', 0),   # H1 - Book/Title level
        (r'^#{2}\s+(.+?)(?:\n|$)', 1),   # H2 - Chapter level
        (r'^#{3}\s+(.+?)(?:\n|$)', 2),   # H3 - Section level
        (r'^#{4}\s+(.+?)(?:\n|$)', 3),   # H4 - Subsection level
    ]
    
    # ALL CAPS headings (5+ chars)
    ALL_CAPS_PATTERN = (r'^[\s]*([A-Z][A-Z\s]{4,}[A-Z])[:：]?\s*(?:\n|$)', 1)
    
    def __init__(self, 
                 include_arabic: bool = True,
                 include_english: bool = True,
                 include_markdown: bool = True):
        """
        Initialize section extractor.
        
        Args:
            include_arabic: Enable Arabic Islamic text patterns
            include_english: Enable English section patterns
            include_markdown: Enable Markdown heading patterns
        """
        self.patterns: List[Tuple[re.Pattern, int]] = []
        
        if include_arabic:
            for pattern, level in self.ARABIC_SECTION_PATTERNS:
                self.patterns.append((re.compile(pattern, re.MULTILINE | re.UNICODE), level))
        
        if include_english:
            for pattern, level in self.ENGLISH_SECTION_PATTERNS:
                self.patterns.append((re.compile(pattern, re.MULTILINE | re.IGNORECASE), level))
            self.patterns.append((re.compile(self.ALL_CAPS_PATTERN[0], re.MULTILINE), self.ALL_CAPS_PATTERN[1]))
        
        if include_markdown:
            for pattern, level in self.MARKDOWN_PATTERNS:
                self.patterns.append((re.compile(pattern, re.MULTILINE), level))
    
    def extract_sections(self, text: str) -> List[Section]:
        """
        Extract all sections from document text.
        
        Args:
            text: Document text
            
        Returns:
            List of Section objects with positions
        """
        if not text:
            return []
        
        # Find all section headings
        matches: List[Tuple[int, int, str, int]] = []  # (start, end, title, level)
        
        for pattern, level in self.patterns:
            for match in pattern.finditer(text):
                title = match.group(1).strip() if match.lastindex and match.group(1) else match.group(0).strip()
                # Clean up title
                title = self._clean_title(title)
                if title:
                    matches.append((match.start(), match.end(), title, level))
        
        # Sort by position
        matches.sort(key=lambda x: x[0])
        
        # Build section hierarchy
        sections: List[Section] = []
        section_stack: List[Section] = []
        
        for i, (start, end, title, level) in enumerate(matches):
            # Determine section end (start of next section or end of text)
            section_end = matches[i + 1][0] if i + 1 < len(matches) else len(text)
            
            # Find parent section
            parent = None
            while section_stack and section_stack[-1].level >= level:
                section_stack.pop()
            if section_stack:
                parent = section_stack[-1]
            
            section = Section(
                title=title,
                level=level,
                start_pos=start,
                end_pos=section_end,
                parent=parent
            )
            
            sections.append(section)
            section_stack.append(section)
        
        return sections
    
    def get_section_for_position(self, sections: List[Section], pos: int) -> Optional[Section]:
        """
        Find the section that contains a given character position.
        
        Args:
            sections: List of extracted sections
            pos: Character position in document
            
        Returns:
            Section containing the position, or None
        """
        containing = None
        for section in sections:
            if section.start_pos <= pos < (section.end_pos or float('inf')):
                # Prefer deeper (more specific) sections
                if containing is None or section.level > containing.level:
                    containing = section
        return containing
    
    def assign_section_to_chunks(self, 
                                  text: str, 
                                  chunks: List[Any], 
                                  chunk_positions: Optional[List[int]] = None) -> List[Dict[str, Any]]:
        """
        Assign section information to chunks.
        
        Args:
            text: Original document text
            chunks: List of chunk objects (must have 'text' attribute)
            chunk_positions: Optional list of character positions for each chunk
            
        Returns:
            List of metadata dicts with section_title for each chunk
        """
        sections = self.extract_sections(text)
        
        if not sections:
            # No sections found - return empty metadata for each chunk
            return [{} for _ in chunks]
        
        # Calculate chunk positions if not provided
        if chunk_positions is None:
            chunk_positions = self._estimate_chunk_positions(text, chunks)
        
        # Assign sections
        metadata_list: List[Dict[str, Any]] = []
        
        for i, (chunk, pos) in enumerate(zip(chunks, chunk_positions)):
            section = self.get_section_for_position(sections, pos)
            
            metadata: Dict[str, Any] = {}
            if section:
                metadata["section_title"] = section.title
                metadata["section_level"] = section.level
                metadata["section_full_path"] = section.full_path
                
                # For traceability, also track section position
                metadata["section_start"] = section.start_pos
            else:
                # No section found - use nearest previous section
                nearest = self._find_nearest_section(sections, pos)
                if nearest:
                    metadata["section_title"] = nearest.title
                    metadata["section_level"] = nearest.level
                    metadata["section_full_path"] = nearest.full_path
                    metadata["section_inherited"] = True
            
            metadata_list.append(metadata)
        
        return metadata_list
    
    def _estimate_chunk_positions(self, text: str, chunks: List[Any]) -> List[int]:
        """
        Estimate character positions of chunks in original text.
        
        Uses a sliding window approach to find approximate positions.
        """
        positions = []
        search_start = 0
        
        for chunk in chunks:
            chunk_text = chunk.text if hasattr(chunk, 'text') else str(chunk)
            # Find this chunk text in original document
            # Use first 100 chars for matching to handle preprocessing differences
            search_text = chunk_text[:min(100, len(chunk_text))].strip()
            
            pos = text.find(search_text, search_start)
            if pos == -1:
                # Try fuzzy match - search for a substring
                words = search_text.split()[:5]  # First 5 words
                if words:
                    fuzzy_text = ' '.join(words)
                    pos = text.find(fuzzy_text, search_start)
            
            if pos == -1:
                # Fallback to estimated position
                pos = search_start
            
            positions.append(pos)
            search_start = pos + len(chunk_text) if pos >= 0 else search_start
        
        return positions
    
    def _find_nearest_section(self, sections: List[Section], pos: int) -> Optional[Section]:
        """Find the nearest section before the given position."""
        nearest = None
        nearest_distance = float('inf')
        
        for section in sections:
            if section.start_pos <= pos:
                distance = pos - section.start_pos
                if distance < nearest_distance:
                    nearest_distance = distance
                    nearest = section
        
        return nearest
    
    def _clean_title(self, title: str) -> str:
        """Clean up extracted section title."""
        # Remove leading markers
        title = re.sub(r'^(?:باب|كتاب|فصل|مسألة|فرع|Chapter|Section|Book|Part)\s*[:：.]?\s*', '', title, flags=re.IGNORECASE)
        # Remove trailing punctuation
        title = re.sub(r'[:：.]+$', '', title)
        # Normalize whitespace
        title = ' '.join(title.split())
        return title.strip()
    
    def force_section_title(self, 
                           chunks: List[Any], 
                           document_title: Optional[str] = None,
                           default_section: str = "Main Content") -> List[Dict[str, Any]]:
        """
        Ensure every chunk has a section_title, using fallbacks if necessary.
        
        This is the strict mode enforcement - every chunk MUST have section_title.
        
        Args:
            chunks: List of chunk objects
            document_title: Document title for fallback
            default_section: Default section name if nothing else available
            
        Returns:
            List of metadata dicts guaranteed to have section_title
        """
        metadata_list: List[Dict[str, Any]] = []
        last_valid_section = default_section
        
        # Try to get document-level section from first chunk's existing metadata
        for chunk in chunks:
            if hasattr(chunk, 'metadata') and chunk.metadata:
                existing_section = chunk.metadata.get('section_title') or chunk.metadata.get('heading')
                if existing_section:
                    last_valid_section = existing_section
                    break
        
        # Assign section to each chunk
        for i, chunk in enumerate(chunks):
            metadata: Dict[str, Any] = {}
            
            # Try to get section from chunk metadata
            section = None
            if hasattr(chunk, 'metadata') and chunk.metadata:
                section = chunk.metadata.get('section_title') or chunk.metadata.get('heading')
            
            # Try to extract from chunk text
            if not section and hasattr(chunk, 'text'):
                section = self._extract_section_from_text(chunk.text)
            
            # Use fallback chain
            if section:
                last_valid_section = section
                metadata["section_title"] = section
            elif document_title:
                metadata["section_title"] = f"{document_title} - {last_valid_section}"
                metadata["section_inherited"] = True
            else:
                metadata["section_title"] = last_valid_section
                metadata["section_inherited"] = True
            
            metadata["section_enforced"] = True
            metadata_list.append(metadata)
        
        return metadata_list
    
    def _extract_section_from_text(self, text: str) -> Optional[str]:
        """Try to extract section title from chunk text itself."""
        lines = text.split('\n')
        for line in lines[:3]:  # Check first 3 lines
            line = line.strip()
            for pattern, _ in self.patterns:
                match = pattern.match(line)
                if match:
                    title = match.group(1).strip() if match.lastindex and match.group(1) else match.group(0).strip()
                    return self._clean_title(title)
        return None


# Convenience functions for integration

def extract_sections_with_traceability(text: str, 
                                        chunks: List[Any],
                                        strict: bool = True,
                                        document_title: Optional[str] = None) -> List[Dict[str, Any]]:
    """
    Extract sections and assign to chunks with strict traceability.
    
    Args:
        text: Original document text
        chunks: List of chunk objects
        strict: If True, forces every chunk to have section_title
        document_title: Document title for fallback
        
    Returns:
        List of metadata dicts with section information
    """
    extractor = SectionExtractor()
    
    # First try position-based assignment
    metadata_list = extractor.assign_section_to_chunks(text, chunks)
    
    if strict:
        # Ensure every chunk has section_title
        for i, metadata in enumerate(metadata_list):
            if not metadata.get('section_title'):
                # Use force assignment
                forced = extractor.force_section_title([chunks[i]], document_title)
                if forced:
                    metadata.update(forced[0])
    
    return metadata_list


def get_section_aware_citation(chunk_metadata: Dict[str, Any], 
                                document_name: Optional[str] = None,
                                position: Optional[int] = None) -> str:
    """
    Build citation text with section information.
    
    Args:
        chunk_metadata: Chunk metadata with section info
        document_name: Document name for citation
        position: Position/paragraph index
        
    Returns:
        Formatted citation string with section
    """
    parts = []
    
    if document_name:
        parts.append(str(document_name))
    
    section_title = chunk_metadata.get('section_title') or chunk_metadata.get('section_full_path')
    if section_title:
        parts.append(f"Section: {section_title}")
    
    if position is not None:
        parts.append(f"Paragraph {position}")
    
    return ", ".join(parts) if parts else "Unknown Source"
