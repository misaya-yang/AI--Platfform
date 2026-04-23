"""
Tests for Strict Section Traceability Feature

Tests the section extraction, assignment, and citation formatting
for Imam-type datasets requiring chapter/section citations.
"""

import pytest

from knowledge_service.services.knowledge.chunking import Chunk, ChunkingConfig
from knowledge_service.services.knowledge.islamic_chunking import IslamicTextChunker
from knowledge_service.services.knowledge.section_extractor import (
    SectionExtractor,
    get_section_aware_citation,
)


class TestSectionExtractor:
    """Test section extraction functionality."""

    def test_extract_arabic_sections(self):
        """Test extraction of Arabic section markers."""
        text = """
كتاب الصلاة

باب الوضوء
Content about wudu goes here.

باب الصلاة
Content about salah goes here.

فصل في أركان الصلاة
More detailed content.
"""
        extractor = SectionExtractor()
        sections = extractor.extract_sections(text)

        assert len(sections) >= 3
        # Check that Arabic sections were found
        titles = [s.title for s in sections]
        assert any("الصلاة" in t for t in titles)

    def test_extract_english_sections(self):
        """Test extraction of English section markers."""
        text = """
Book I: The Book of Prayer

Chapter 1: Purification
Content about purification.

Chapter 2: The Prayer
Content about prayer.

Section 1.1: Wudu
Detailed content about wudu.
"""
        extractor = SectionExtractor(include_arabic=False, include_english=True)
        sections = extractor.extract_sections(text)

        assert len(sections) >= 3
        # Check that sections were extracted
        titles = [s.title for s in sections]
        assert any("Prayer" in t or "Purification" in t or "Wudu" in t for t in titles)

    def test_section_hierarchy(self):
        """Test that section hierarchy is correctly built."""
        text = """
# Book of Worship

## Chapter 1: Prayer
Content about prayer.

### Section 1.1: Wudu
Content about wudu.

### Section 1.2: Salah
Content about salah.

## Chapter 2: Fasting
Content about fasting.
"""
        extractor = SectionExtractor(include_arabic=False, include_markdown=True)
        sections = extractor.extract_sections(text)

        # Find sections
        ch1 = next((s for s in sections if "Prayer" in s.title), None)
        sec11 = next((s for s in sections if "Wudu" in s.title), None)

        if ch1:
            assert ch1.level == 1
        if sec11:
            assert sec11.level == 2

    def test_get_section_for_position(self):
        """Test finding section for a character position."""
        text = """
# Chapter 1
Content in chapter 1.
More content.

# Chapter 2
Content in chapter 2.
"""
        extractor = SectionExtractor(include_arabic=False, include_markdown=True)
        sections = extractor.extract_sections(text)

        # Position in chapter 1
        pos_ch1 = text.find("Content in chapter 1")
        section = extractor.get_section_for_position(sections, pos_ch1)
        assert section is not None
        # Section title should contain "1" (the chapter number)
        assert "1" in section.title

    def test_assign_section_to_chunks(self):
        """Test assigning sections to chunks."""
        text = """
# Chapter 1: Introduction
This is the introduction content.

# Chapter 2: Main Content
This is the main content.
"""
        chunks = [
            Chunk(text="This is the introduction content.", index=0),
            Chunk(text="This is the main content.", index=1),
        ]

        extractor = SectionExtractor(include_arabic=False, include_markdown=True)
        metadata_list = extractor.assign_section_to_chunks(text, chunks)

        assert len(metadata_list) == 2
        # Chunks should have section info
        assert metadata_list[0].get("section_title") is not None

    def test_force_section_title(self):
        """Test forcing section titles on all chunks."""
        chunks = [
            Chunk(text="Some content without section.", index=0),
            Chunk(text="More content.", index=1),
        ]

        extractor = SectionExtractor()
        metadata_list = extractor.force_section_title(
            chunks, document_title="Test Document", default_section="General"
        )

        assert len(metadata_list) == 2
        # Both chunks should have section_title
        assert metadata_list[0].get("section_title") is not None
        assert metadata_list[1].get("section_title") is not None
        assert metadata_list[0].get("section_enforced") is True


class TestSectionAwareCitation:
    """Test section-aware citation formatting."""

    def test_citation_with_section(self):
        """Test building citation with section info."""
        metadata = {
            "section_title": "Chapter 1: Prayer",
            "section_level": 1,
        }
        citation = get_section_aware_citation(metadata, document_name="Book of Worship", position=5)

        assert "Book of Worship" in citation
        assert "5" in citation

    def test_citation_without_section(self):
        """Test building citation without section info."""
        metadata = {}
        citation = get_section_aware_citation(metadata, document_name="Book of Worship", position=3)

        assert "Book of Worship" in citation
        assert "3" in citation


class TestIslamicChunkerWithTraceability:
    """Test Islamic text chunker with strict section traceability."""

    def test_islamic_chunker_applies_traceability(self):
        """Test that Islamic chunker applies section traceability when enabled."""
        text = """
باب الوضوء

Chapter of Wudu

The Prophet (peace be upon him) said about wudu...
More content here.

باب الصلاة

Chapter of Prayer

Content about prayer.
"""
        config = ChunkingConfig(
            mode="islamic",
            strict_section_traceability=True,
            token_limit=500,
        )
        chunker = IslamicTextChunker(config)
        chunks = chunker.chunk(text)

        # All chunks should have section_title
        for chunk in chunks:
            assert "section_title" in chunk.metadata
            assert chunk.metadata["section_title"] is not None

    def test_islamic_chunker_without_traceability(self):
        """Test that Islamic chunker works normally without traceability."""
        text = "Some general Islamic content about prayer and fasting."
        config = ChunkingConfig(
            mode="islamic",
            strict_section_traceability=False,
            token_limit=500,
        )
        chunker = IslamicTextChunker(config)
        chunks = chunker.chunk(text)

        # Should produce chunks without forced section traceability
        assert len(chunks) > 0


class TestChunkingConfig:
    """Test ChunkingConfig with strict_section_traceability."""

    def test_config_has_traceability_field(self):
        """Test that config has the new field."""
        config = ChunkingConfig(strict_section_traceability=True)
        assert config.strict_section_traceability is True

    def test_config_from_dict_with_traceability(self):
        """Test loading config from dict with traceability."""
        data = {
            "mode": "islamic",
            "strict_section_traceability": True,
            "token_limit": 500,
        }
        config = ChunkingConfig.from_dict(data)
        assert config.strict_section_traceability is True
        assert config.mode.value == "islamic"

    def test_config_to_dict_with_traceability(self):
        """Test saving config to dict with traceability."""
        config = ChunkingConfig(strict_section_traceability=True)
        data = config.to_dict()
        assert data["strict_section_traceability"] is True


class TestRetrievalConfig:
    """Test retrieval config with section traceability."""

    def test_islamic_strict_traceability_preset(self):
        """Test the islamic_strict_traceability preset."""
        from knowledge_service.services.knowledge.retrieval_config import get_preset_config

        config = get_preset_config("islamic_strict_traceability")
        assert config.islamic.strict_section_traceability is True
        assert config.islamic.citation_format is True
        assert config.islamic.authority_sort is True


if __name__ == "__main__":
    pytest.main([__file__, "-v"])
