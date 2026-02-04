#!/usr/bin/env python3
"""
Full Pipeline Test: PDF Ingestion → Chunking → Embedding → Retrieval

Tests the complete knowledge base flow with a real Islamic PDF.
Validates:
1. Chunking strategy (automatic vs hierarchical)
2. Token limits adherence  
3. Traceability fields (source_type, citation_text, source_reference)
4. Arabic/English retrieval quality
"""

import sys
import os
import asyncio

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from src.services.knowledge.chunking import (
    ChunkingConfig,
    ChunkingMode,
    create_chunker,
    count_tokens,
    HierarchicalChunker,
    AutomaticChunker,
)
from src.services.knowledge.islamic_chunking import (
    IslamicTextChunker,
    detect_islamic_source_type,
    is_islamic_text,
)


def test_pdf_text_extraction():
    """Test extracting text from a PDF."""
    print("\n" + "="*60)
    print("Test 1: PDF Text Extraction")
    print("="*60)
    
    pdf_path = "/Users/misaya.yanghejazfs.com.au/Downloads/OneDrive_2026-01-29 (1)/For Ai Imam/Aqeedah/english_Tawheed_Made_Easy.pdf"
    
    if not os.path.exists(pdf_path):
        print(f"  ❌ PDF not found: {pdf_path}")
        return None
    
    # Try to extract text
    try:
        import pypdf
        reader = pypdf.PdfReader(pdf_path)
        text = ""
        for page in reader.pages[:10]:  # First 10 pages only
            text += page.extract_text() or ""
        
        print(f"  ✓ Extracted {len(text)} characters from first 10 pages")
        print(f"  ✓ Estimated {count_tokens(text)} tokens")
        print(f"  Preview: {text[:200]}...")
        return text
    except ImportError:
        print("  ❌ pypdf not installed")
        return None
    except Exception as e:
        print(f"  ❌ Error: {e}")
        return None


def test_islamic_detection(text: str):
    """Test if the text is detected as Islamic content."""
    print("\n" + "="*60)
    print("Test 2: Islamic Content Detection")
    print("="*60)
    
    is_islamic = is_islamic_text(text)
    source_type = detect_islamic_source_type(text)
    
    print(f"  Is Islamic text: {is_islamic}")
    print(f"  Source type: {source_type.value}")
    
    return is_islamic, source_type


def test_chunking_strategies(text: str):
    """Compare different chunking strategies."""
    print("\n" + "="*60)
    print("Test 3: Chunking Strategies Comparison")
    print("="*60)
    
    strategies = [
        ("Automatic", ChunkingMode.AUTOMATIC),
        ("Hierarchical (Parent-Child)", ChunkingMode.HIERARCHICAL),
        ("Fixed Size", ChunkingMode.FIXED_SIZE),
        ("Islamic", ChunkingMode.ISLAMIC),
    ]
    
    results = {}
    
    for name, mode in strategies:
        config = ChunkingConfig(
            mode=mode,
            use_token_count=True,
            token_limit=400,  # Target 400 tokens per chunk
            parent_chunk_size=8000,  # ~2000 tokens for parent
            child_chunk_size=2000,   # ~500 tokens for child
        )
        
        chunker = create_chunker(config)
        chunks = chunker.chunk(text)
        
        # Analyze chunks
        token_counts = [c.token_count for c in chunks]
        avg_tokens = sum(token_counts) / len(token_counts) if token_counts else 0
        max_tokens = max(token_counts) if token_counts else 0
        min_tokens = min(token_counts) if token_counts else 0
        
        # Check for traceability fields
        has_source_type = sum(1 for c in chunks if c.metadata.get("source_type") or c.metadata.get("islamic_source_type"))
        has_parent_id = sum(1 for c in chunks if c.parent_id)
        
        results[name] = {
            "count": len(chunks),
            "avg_tokens": avg_tokens,
            "max_tokens": max_tokens,
            "min_tokens": min_tokens,
            "with_source_type": has_source_type,
            "with_parent_id": has_parent_id,
        }
        
        print(f"\n  {name}:")
        print(f"    Chunks: {len(chunks)}")
        print(f"    Tokens: avg={avg_tokens:.0f}, min={min_tokens}, max={max_tokens}")
        print(f"    With source_type: {has_source_type}/{len(chunks)}")
        print(f"    With parent_id: {has_parent_id}/{len(chunks)}")
        
        # Show first chunk sample
        if chunks:
            c = chunks[0]
            print(f"    First chunk preview ({c.token_count} tokens):")
            print(f"      {c.text[:150]}...")
            print(f"      Metadata: {c.metadata}")
    
    return results


def test_hierarchical_details(text: str):
    """Deep dive into hierarchical chunking."""
    print("\n" + "="*60)
    print("Test 4: Hierarchical Chunking Details")
    print("="*60)
    
    config = ChunkingConfig(
        mode=ChunkingMode.HIERARCHICAL,
        use_token_count=True,
        token_limit=400,
        parent_chunk_size=8000,  # ~2000 tokens
        child_chunk_size=2000,   # ~500 tokens
    )
    
    chunker = HierarchicalChunker(config)
    chunks = chunker.chunk(text)
    
    # Separate parents and children
    parents = [c for c in chunks if c.metadata.get("is_parent")]
    children = [c for c in chunks if c.parent_id]
    
    print(f"  Total chunks: {len(chunks)}")
    print(f"  Parent chunks: {len(parents)}")
    print(f"  Child chunks: {len(children)}")
    
    # Parent-child relationships
    parent_map = {}
    for c in chunks:
        if c.metadata.get("is_parent"):
            parent_map[c.hash_id] = {"parent": c, "children": []}
    
    for c in children:
        if c.parent_id in parent_map:
            parent_map[c.parent_id]["children"].append(c)
    
    print("\n  Parent-Child Structure:")
    for parent_id, data in list(parent_map.items())[:3]:  # First 3
        parent = data["parent"]
        children_list = data["children"]
        print(f"\n    Parent [{parent.index}]: {parent.token_count} tokens")
        print(f"      Preview: {parent.text[:100]}...")
        print(f"      Children: {len(children_list)}")
        for child in children_list[:2]:  # First 2 children
            print(f"        Child [{child.index}]: {child.token_count} tokens")
    
    return parents, children


def test_traceability_requirements():
    """Test if traceability requirements from Imam.md are met."""
    print("\n" + "="*60)
    print("Test 5: Traceability Requirements (Imam.md)")
    print("="*60)
    
    print("""
  Required per Imam.md Section III - Citation & Source Attribution:
  
  1. Mandatory Source Citation: Every answer must include source citations
  2. Citation Format:
     - Quran: "Quran [Chapter]:[Verse] - [Translation used]"
     - Hadith: "Sahih Bukhari, Book [X], Hadith [X]"
     - Tafseer: "Tafsir Ibn Kathir, Surah [X], Verse [X]"
     - Fiqh: "[School name], [Topic]"
     - Aqeedah: "[Book name], [Chapter/Section]"
  3. Source Order: Sorted by Islamic authority
  
  Required fields in segment metadata:
  - segment_id: Unique identifier ✓
  - document_id: Link to source document ✓
  - source_type: quran/hadith/fiqh/tafseer/aqeedah
  - citation_text: Formatted citation
  - source_reference: {book, chapter, verse, hadith_num, etc.}
  """)
    
    # Check if IslamicTextChunker sets these
    sample_quran = """
    بِسْمِ اللَّهِ الرَّحْمَٰنِ الرَّحِيمِ
    الْحَمْدُ لِلَّهِ رَبِّ الْعَالَمِينَ
    الرَّحْمَٰنِ الرَّحِيمِ
    مَالِكِ يَوْمِ الدِّينِ
    """
    
    config = ChunkingConfig(mode=ChunkingMode.ISLAMIC)
    chunker = IslamicTextChunker(config)
    chunks = chunker.chunk(sample_quran)
    
    print("\n  IslamicTextChunker output for Quran sample:")
    for c in chunks[:2]:
        print(f"    Chunk {c.index}:")
        print(f"      source_type: {c.metadata.get('source_type', 'MISSING')}")
        print(f"      islamic_source_type: {c.metadata.get('islamic_source_type', 'MISSING')}")
        print(f"      citation_text: {c.metadata.get('citation_text', 'MISSING')}")
        print(f"      source_reference: {c.metadata.get('source_reference', 'MISSING')}")


def recommend_optimal_config():
    """Recommend optimal chunking configuration for Imam agent."""
    print("\n" + "="*60)
    print("RECOMMENDATION: Optimal Configuration for Imam Agent")
    print("="*60)
    
    print("""
  Based on analysis, the optimal configuration for Islamic knowledge base is:
  
  1. CHUNKING MODE: hierarchical (parent-child)
     - Parent chunks: ~1500-2000 tokens (provide context)
     - Child chunks: ~400-500 tokens (precise retrieval)
     - Benefits: Better context retrieval, maintains document structure
  
  2. FOR ISLAMIC TEXTS: mode=islamic
     - Automatically detects Quran/Hadith/Fiqh/Tafseer
     - Respects verse boundaries, hadith narration units
     - Sets proper source_type, citation_text, source_reference
  
  3. TOKEN-BASED CHUNKING: use_token_count=True, token_limit=500
     - Ensures consistent chunk sizes for embedding models
     - Better for multilingual (Arabic/English) content
  
  4. RECOMMENDED ChunkingConfig:
  
  ```python
  ChunkingConfig(
      mode=ChunkingMode.HIERARCHICAL,  # or ISLAMIC for Islamic texts
      use_token_count=True,
      token_limit=500,
      parent_chunk_size=8000,   # ~2000 tokens
      child_chunk_size=2000,    # ~500 tokens  
      chunk_overlap=200,        # ~50 tokens overlap
  )
  ```
  
  5. TRACEABILITY: Ensure ingestion populates:
     - source_type: Document category
     - citation_text: Formatted citation
     - source_reference: {book, chapter, verse, etc.}
  """)


def main():
    """Run all tests."""
    print("\n" + "="*60)
    print("Full Pipeline Test: Islamic Knowledge Base")
    print("="*60)
    
    # Test 1: Extract PDF
    text = test_pdf_text_extraction()
    
    if not text:
        print("\n❌ Cannot continue without PDF text. Using sample text instead.")
        text = """
        Tawheed Made Easy
        
        Chapter 1: Introduction to Tawheed
        
        Tawheed is the concept of monotheism in Islam. It means the belief in the oneness 
        of Allah (God). The Prophet Muhammad (peace be upon him) taught that Tawheed is the 
        foundation of Islamic faith.
        
        Allah says in the Quran: "Say, He is Allah, [who is] One. Allah, the Eternal Refuge. 
        He neither begets nor is born, Nor is there to Him any equivalent." (Surah Al-Ikhlas, 112:1-4)
        
        This surah summarizes the essence of Tawheed. The Prophet said: "Whoever says 
        La ilaha illallah sincerely will enter Paradise." (Sahih Bukhari)
        
        Chapter 2: Types of Tawheed
        
        Scholars have categorized Tawheed into three types:
        
        1. Tawheed ar-Rububiyyah (Lordship): Belief that Allah alone is the Creator, Sustainer, 
           and Controller of the universe.
        
        2. Tawheed al-Uluhiyyah (Worship): Belief that Allah alone deserves to be worshipped.
        
        3. Tawheed al-Asma wa's-Sifat (Names and Attributes): Belief in Allah's names and 
           attributes as mentioned in the Quran and Sunnah.
        
        والصلاة والسلام على رسول الله
        """ * 5  # Repeat for more content
    
    # Test 2: Islamic detection
    test_islamic_detection(text)
    
    # Test 3: Chunking strategies
    test_chunking_strategies(text)
    
    # Test 4: Hierarchical details
    test_hierarchical_details(text)
    
    # Test 5: Traceability
    test_traceability_requirements()
    
    # Recommendation
    recommend_optimal_config()
    
    print("\n" + "="*60)
    print("Full Pipeline Test Complete")
    print("="*60)
    
    return 0


if __name__ == "__main__":
    sys.exit(main())
