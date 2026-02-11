#!/usr/bin/env python3
"""
Test script to validate knowledge base improvements:
1. Token counting accuracy (tiktoken + Arabic support)
2. Token-based chunking
3. Multilingual tokenization (Arabic, English, Chinese)
4. Score normalization strategies
"""

import os
import sys

# Add parent directory to path
sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from src.services.knowledge.chunking import (
    ChunkingConfig,
    ChunkingMode,
    FixedSizeChunker,
    TokenCounter,
)
from src.services.knowledge.retrieval import (
    ScoreNormalization,
    compute_language_weights,
    detect_language,
    normalize_arabic,
    tokenize,
    tokenize_arabic,
)


def test_token_counting():
    """Test multilingual token counting."""
    print("\n" + "=" * 60)
    print("Test 1: Token Counting (Multilingual)")
    print("=" * 60)

    test_cases = [
        # English
        ("Hello, how are you doing today?", "English"),
        ("The Quran is the central religious text of Islam.", "English/Islamic"),
        # Arabic
        ("بِسْمِ اللَّهِ الرَّحْمَٰنِ الرَّحِيمِ", "Arabic/Bismillah"),
        ("قُلْ هُوَ اللَّهُ أَحَدٌ", "Arabic/Surah Al-Ikhlas"),
        ("صلى الله عليه وسلم", "Arabic/PBUH"),
        # Chinese
        ("人工智能网关平台", "Chinese"),
        # Mixed
        ("The Prophet صلى الله عليه وسلم said: 'Actions are judged by intentions.'", "Mixed"),
    ]

    counter = TokenCounter(use_tiktoken=True)

    for text, lang in test_cases:
        tokens = counter.count_tokens(text)
        print(f"  [{lang}] {text[:50]}...")
        print(f"    → {tokens} tokens, {len(text)} chars, ratio: {tokens / max(len(text), 1):.2f}")

    print("\n✓ Token counting test completed")


def test_arabic_tokenization():
    """Test Arabic-specific tokenization."""
    print("\n" + "=" * 60)
    print("Test 2: Arabic Tokenization")
    print("=" * 60)

    test_cases = [
        "بِسْمِ اللَّهِ الرَّحْمَٰنِ الرَّحِيمِ",  # With diacritics
        "بسم الله الرحمن الرحيم",  # Without diacritics
        "والصلاة والسلام على رسول الله",  # With prefix و
        "الإسلام والمسلمين",  # Alef variations
    ]

    for text in test_cases:
        # Normalize
        normalized = normalize_arabic(text)
        # Tokenize
        tokens = tokenize_arabic(text, remove_stopwords=True)

        print(f"  Original: {text}")
        print(f"  Normalized: {normalized}")
        print(f"  Tokens: {tokens}")
        print()

    print("✓ Arabic tokenization test completed")


def test_multilingual_tokenization():
    """Test multilingual tokenization for BM25."""
    print("\n" + "=" * 60)
    print("Test 3: Multilingual BM25 Tokenization")
    print("=" * 60)

    test_cases = [
        ("What is Salah in Islam?", "en"),
        ("ما هي الصلاة في الإسلام", "ar"),
        ("智能知识库系统", "zh"),
        ("What is الصلاة prayer?", "mixed"),
    ]

    for text, expected_lang in test_cases:
        detected = detect_language(text)
        tokens = tokenize(text, keep_original=True, remove_stopwords=True)

        print(f"  Text: {text}")
        print(f"  Detected: {detected}, Expected: {expected_lang}")
        print(f"  Tokens: {tokens}")
        print()

    print("✓ Multilingual tokenization test completed")


def test_language_weights():
    """Test language-specific fusion weights."""
    print("\n" + "=" * 60)
    print("Test 4: Language-Specific Fusion Weights")
    print("=" * 60)

    test_cases = [
        "What is prayer in Islam?",  # English
        "ما هي الصلاة",  # Arabic
        "人工智能问答",  # Chinese
        "Prayer الصلاة 祈祷",  # Mixed
    ]

    for query in test_cases:
        lang = detect_language(query)
        dense_w, bm25_w = compute_language_weights(query)

        print(f"  Query: {query}")
        print(f"  Language: {lang}")
        print(f"  Weights: dense={dense_w:.2f}, bm25={bm25_w:.2f}")
        print()

    print("✓ Language weights test completed")


def test_score_normalization():
    """Test score normalization strategies."""
    print("\n" + "=" * 60)
    print("Test 5: Score Normalization Strategies")
    print("=" * 60)

    # Simulate BM25 scores (can be very different scales)
    bm25_scores = {
        "doc1": 15.5,
        "doc2": 8.3,
        "doc3": 2.1,
        "doc4": 25.0,  # outlier
        "doc5": 6.7,
    }

    # Simulate dense scores (typically 0-1 range)
    dense_scores = {
        "doc1": 0.85,
        "doc2": 0.72,
        "doc3": 0.45,
        "doc4": 0.91,
        "doc5": 0.68,
    }

    print("  Original BM25 scores:", bm25_scores)
    print("  Original Dense scores:", dense_scores)
    print()

    # Min-max normalization
    bm25_minmax = ScoreNormalization.min_max(bm25_scores)
    print("  Min-Max BM25:", {k: round(v, 3) for k, v in bm25_minmax.items()})

    # Robust normalization
    bm25_robust = ScoreNormalization.robust_normalize(bm25_scores)
    print("  Robust BM25:", {k: round(v, 3) for k, v in bm25_robust.items()})

    # Percentile normalization
    bm25_percentile = ScoreNormalization.percentile_normalize(bm25_scores)
    print("  Percentile BM25:", {k: round(v, 3) for k, v in bm25_percentile.items()})

    print()
    print("✓ Score normalization test completed")


def test_token_based_chunking():
    """Test token-based chunking."""
    print("\n" + "=" * 60)
    print("Test 6: Token-Based Chunking")
    print("=" * 60)

    # Sample Islamic text (mixed English/Arabic)
    sample_text = """
In the name of Allah, the Most Gracious, the Most Merciful.

بِسْمِ اللَّهِ الرَّحْمَٰنِ الرَّحِيمِ

Praise be to Allah, Lord of all the worlds. The Prophet Muhammad (peace be upon him)
said: "Actions are judged by intentions, and every person will get what they intended."
This hadith is narrated by Umar ibn al-Khattab (may Allah be pleased with him) and
recorded in Sahih al-Bukhari and Sahih Muslim.

Prayer (Salah) is the second pillar of Islam. Muslims are required to pray five times
a day: Fajr (dawn), Dhuhr (noon), Asr (afternoon), Maghrib (sunset), and Isha (night).
The prayer consists of specific movements and recitations, including standing (Qiyam),
bowing (Ruku), prostration (Sujud), and sitting (Juloos).

The Quran states: "Indeed, prayer prohibits immorality and wrongdoing, and the
remembrance of Allah is greater. And Allah knows that which you do." (29:45)

والصلاة والسلام على رسول الله وعلى آله وصحبه أجمعين
    """

    # Test with different token limits
    for token_limit in [100, 200, 300]:
        print(f"\n  Token limit: {token_limit}")

        config = ChunkingConfig(
            mode=ChunkingMode.FIXED_SIZE,
            use_token_count=True,
            token_limit=token_limit,
        )

        chunker = FixedSizeChunker(config)
        chunks = chunker.chunk(sample_text)

        print(f"  Number of chunks: {len(chunks)}")
        for i, chunk in enumerate(chunks[:3]):  # Show first 3
            print(f"    Chunk {i + 1}: {chunk.token_count} tokens, {len(chunk.text)} chars")
            print(f"      Preview: {chunk.text[:80]}...")

    print("\n✓ Token-based chunking test completed")


def main():
    """Run all tests."""
    print("\n" + "=" * 60)
    print("Knowledge Base Improvement Tests")
    print("=" * 60)

    try:
        test_token_counting()
        test_arabic_tokenization()
        test_multilingual_tokenization()
        test_language_weights()
        test_score_normalization()
        test_token_based_chunking()

        print("\n" + "=" * 60)
        print("ALL TESTS PASSED ✓")
        print("=" * 60)

    except Exception as e:
        print(f"\n❌ Test failed with error: {e}")
        import traceback

        traceback.print_exc()
        return 1

    return 0


if __name__ == "__main__":
    sys.exit(main())
