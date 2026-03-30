"""
Test all 9 chunking modes to verify they work correctly with user config.
"""

from src.services.knowledge.chunking import (
    ChunkingConfig,
    ChunkingMode,
    create_chunker,
    flatten_chunks,
)

# Sample text for testing
SAMPLE_TEXT = """
# Chapter 1: Introduction

This is the first paragraph of the introduction. It contains some basic information about the topic.

This is the second paragraph. It provides additional context and background information.

# Chapter 2: Main Content

The main content starts here. This section covers the core concepts and ideas.

## Section 2.1: Details

More detailed information is provided in this subsection.

## Section 2.2: Examples

Here are some examples to illustrate the concepts.

# Chapter 3: Conclusion

In conclusion, we have covered the main topics.

Final thoughts and future directions are discussed here.
"""


def _run_chunking_mode(mode: ChunkingMode, config: ChunkingConfig):
    """Test a single chunking mode."""
    print(f"\n{'=' * 60}")
    print(f"Testing: {mode.value}")
    print(f"Config: token_limit={config.token_limit}, use_token_count={config.use_token_count}")
    print("=" * 60)

    try:
        # Set the mode
        config.mode = mode

        # Create chunker
        chunker = create_chunker(config)
        print(f"Chunker: {chunker.__class__.__name__}")

        # Process text
        chunks = chunker.chunk(SAMPLE_TEXT)
        flat_chunks = flatten_chunks(chunks)

        print(f"Generated {len(flat_chunks)} chunks")

        # Show chunk stats
        if flat_chunks:
            token_counts = [c.token_count for c in flat_chunks]
            char_counts = [len(c.text) for c in flat_chunks]
            print(
                f"Token counts: min={min(token_counts)}, max={max(token_counts)}, avg={sum(token_counts) / len(token_counts):.1f}"
            )
            print(
                f"Char counts: min={min(char_counts)}, max={max(char_counts)}, avg={sum(char_counts) / len(char_counts):.1f}"
            )

            # Check if respecting token_limit
            if config.use_token_count:
                exceeding = sum(
                    1 for t in token_counts if t > config.token_limit * 1.2
                )  # 20% tolerance
                if exceeding > 0:
                    print(f"⚠️  WARNING: {exceeding} chunks exceed token_limit by >20%")
                else:
                    print(f"✅ All chunks within token_limit ({config.token_limit})")

        return True, len(flat_chunks)

    except Exception as e:
        print(f"❌ ERROR: {e}")
        import traceback

        traceback.print_exc()
        return False, 0


def main():
    print("\n" + "=" * 70)
    print("TESTING ALL CHUNKING MODES WITH USER CONFIG (400 tokens)")
    print("=" * 70)

    # User config: 400 tokens
    user_config = ChunkingConfig(
        token_limit=400,
        use_token_count=True,
        min_chunk_tokens=80,
        max_chunk_tokens=400,
        chunk_overlap=50,
    )

    modes_to_test = [
        ChunkingMode.AUTOMATIC,
        ChunkingMode.FIXED_SIZE,
        ChunkingMode.PARAGRAPH,
        ChunkingMode.PAGE,
        ChunkingMode.HEADING,
        ChunkingMode.REGEX,
        ChunkingMode.SEPARATOR,
        ChunkingMode.RECURSIVE,
        ChunkingMode.HIERARCHICAL,
        ChunkingMode.QA,
        # Note: ISLAMIC mode requires special import
    ]

    results = []
    for mode in modes_to_test:
        success, count = _run_chunking_mode(mode, user_config)
        results.append((mode.value, success, count))

    # Summary
    print("\n" + "=" * 70)
    print("SUMMARY")
    print("=" * 70)
    print(f"{'Mode':<20} {'Status':<10} {'Chunks':<10}")
    print("-" * 40)
    for mode, success, count in results:
        status = "✅ PASS" if success else "❌ FAIL"
        print(f"{mode:<20} {status:<10} {count:<10}")

    passed = sum(1 for _, s, _ in results if s)
    total = len(results)
    print(f"\nTotal: {passed}/{total} modes passed")

    # Check Islamic mode separately
    print("\n" + "=" * 70)
    print("ISLAMIC MODE (requires islamic_chunking module)")
    print("=" * 70)
    try:
        islamic_config = ChunkingConfig(
            mode=ChunkingMode.ISLAMIC,
            token_limit=400,
            use_token_count=True,
        )
        chunker = create_chunker(islamic_config)
        print(f"✅ Islamic chunker loaded: {chunker.__class__.__name__}")
    except Exception as e:
        print(f"⚠️  Islamic chunker not available: {e}")


import pytest

_MODES = [
    ChunkingMode.AUTOMATIC,
    ChunkingMode.FIXED_SIZE,
    ChunkingMode.PARAGRAPH,
    ChunkingMode.PAGE,
    ChunkingMode.HEADING,
    ChunkingMode.REGEX,
    ChunkingMode.SEPARATOR,
    ChunkingMode.RECURSIVE,
    ChunkingMode.HIERARCHICAL,
]


@pytest.mark.parametrize("mode", _MODES, ids=lambda m: m.value)
def test_chunking_mode(mode):
    """Each standard chunking mode should produce at least one chunk."""
    config = ChunkingConfig(
        token_limit=400,
        use_token_count=True,
        min_chunk_tokens=80,
        max_chunk_tokens=400,
        chunk_overlap=50,
    )
    success, count = _run_chunking_mode(mode, config)
    assert success, f"Chunking mode {mode.value} failed"
    assert count > 0


if __name__ == "__main__":
    main()
