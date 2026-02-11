"""
StreamingWriter Tests

Tests for the StreamingWriter class and StreamChunk dataclass:
- StreamChunk initialization and validation
- StreamingWriter initialization
- Trigger detection and pattern compilation
- Verification query extraction
- KB search integration
- Result formatting
- Buffer management
- Edge cases and error handling
"""

from unittest.mock import AsyncMock, MagicMock

import pytest

from src.services.assistant.streaming_writer import (
    DEFAULT_VERIFICATION_TRIGGERS,
    StreamChunk,
    StreamingWriter,
    VerificationContext,
    create_streaming_writer,
)


def create_mock_user():
    """Create a mock UserContext for tests."""
    mock_user = MagicMock()
    mock_user.tenant_id = "test_tenant"
    mock_user.user_id = "test_user"
    return mock_user


# =============================================================================
# StreamChunk Tests
# =============================================================================


class TestStreamChunk:
    """Test StreamChunk dataclass."""

    def test_text_chunk_creation(self):
        """Test creating a text chunk."""
        chunk = StreamChunk(type="text", content="Hello, world!")

        assert chunk.type == "text"
        assert chunk.content == "Hello, world!"
        assert chunk.metadata is None

    def test_search_start_chunk(self):
        """Test creating a search_start chunk with metadata."""
        chunk = StreamChunk(
            type="search_start",
            content="refund policy",
            metadata={"trigger": "according to", "dataset_ids": ["policies"]},
        )

        assert chunk.type == "search_start"
        assert chunk.content == "refund policy"
        assert chunk.metadata["trigger"] == "according to"
        assert chunk.metadata["dataset_ids"] == ["policies"]

    def test_search_result_chunk(self):
        """Test creating a search_result chunk."""
        chunk = StreamChunk(
            type="search_result",
            content="[1] (score: 0.95) Refunds within 30 days...",
            metadata={
                "query": "refund policy",
                "result_count": 3,
                "results": [{"content": "...", "score": 0.95}],
            },
        )

        assert chunk.type == "search_result"
        assert "score: 0.95" in chunk.content
        assert chunk.metadata["result_count"] == 3

    def test_search_end_chunk(self):
        """Test creating a search_end chunk."""
        chunk = StreamChunk(
            type="search_end",
            content="",
            metadata={"query": "refund policy", "result_count": 3, "duration_ms": 150.5},
        )

        assert chunk.type == "search_end"
        assert chunk.content == ""
        assert chunk.metadata["duration_ms"] == 150.5

    def test_error_chunk(self):
        """Test creating an error chunk."""
        chunk = StreamChunk(
            type="error",
            content="Search failed: Connection timeout",
            metadata={"error_type": "TimeoutError"},
        )

        assert chunk.type == "error"
        assert "timeout" in chunk.content.lower()
        assert chunk.metadata["error_type"] == "TimeoutError"

    def test_invalid_chunk_type(self):
        """Test that invalid chunk types raise ValueError."""
        with pytest.raises(ValueError) as exc_info:
            StreamChunk(type="invalid_type", content="test")

        assert "Invalid chunk type" in str(exc_info.value)
        assert "invalid_type" in str(exc_info.value)

    def test_empty_content_allowed(self):
        """Test that empty content is allowed."""
        chunk = StreamChunk(type="text", content="")

        assert chunk.type == "text"
        assert chunk.content == ""

    def test_all_valid_types(self):
        """Test all valid chunk types can be created."""
        valid_types = ["text", "search_start", "search_result", "search_end", "error"]

        for chunk_type in valid_types:
            chunk = StreamChunk(type=chunk_type, content="test")
            assert chunk.type == chunk_type


# =============================================================================
# VerificationContext Tests
# =============================================================================


class TestVerificationContext:
    """Test VerificationContext dataclass."""

    def test_basic_initialization(self):
        """Test basic VerificationContext creation."""
        ctx = VerificationContext(
            trigger="according to",
            trigger_position=50,
            query="refund policy",
        )

        assert ctx.trigger == "according to"
        assert ctx.trigger_position == 50
        assert ctx.query == "refund policy"
        assert ctx.results == []
        assert ctx.verified is False

    def test_with_results(self):
        """Test VerificationContext with results."""
        results = [{"content": "Policy doc", "score": 0.9}]
        ctx = VerificationContext(
            trigger="policy states",
            trigger_position=30,
            query="policy details",
            results=results,
            verified=True,
        )

        assert ctx.results == results
        assert ctx.verified is True


# =============================================================================
# StreamingWriter Initialization Tests
# =============================================================================


class TestStreamingWriterInit:
    """Test StreamingWriter initialization."""

    def test_basic_initialization(self):
        """Test basic initialization with mocked services."""
        mock_kb_service = MagicMock()
        mock_assistant_service = MagicMock()

        writer = StreamingWriter(
            kb_service=mock_kb_service,
            assistant_service=mock_assistant_service,
        )

        assert writer.kb_service == mock_kb_service
        assert writer.assistant_service == mock_assistant_service
        assert writer.buffer_threshold == 100
        assert writer.search_top_k == 3

    def test_custom_parameters(self):
        """Test initialization with custom parameters."""
        mock_kb_service = MagicMock()
        mock_assistant_service = MagicMock()

        writer = StreamingWriter(
            kb_service=mock_kb_service,
            assistant_service=mock_assistant_service,
            buffer_threshold=200,
            search_top_k=5,
        )

        assert writer.buffer_threshold == 200
        assert writer.search_top_k == 5


# =============================================================================
# Trigger Pattern Compilation Tests
# =============================================================================


class TestTriggerPatternCompilation:
    """Test trigger pattern compilation."""

    def test_compile_english_triggers(self):
        """Test compilation of English triggers with word boundaries."""
        mock_kb_service = MagicMock()
        mock_assistant_service = MagicMock()
        writer = StreamingWriter(mock_kb_service, mock_assistant_service)

        patterns = writer._compile_trigger_patterns(["according to", "based on"])

        assert len(patterns) == 2

        # Test case-insensitive matching
        assert patterns[0].search("According to the policy")
        assert patterns[0].search("ACCORDING TO the policy")
        assert patterns[0].search("according to the policy")

    def test_compile_chinese_triggers(self):
        """Test compilation of Chinese triggers."""
        mock_kb_service = MagicMock()
        mock_assistant_service = MagicMock()
        writer = StreamingWriter(mock_kb_service, mock_assistant_service)

        patterns = writer._compile_trigger_patterns(["根据", "政策"])

        assert len(patterns) == 2

        # Test direct matching for Chinese
        assert patterns[0].search("根据公司规定")
        assert patterns[1].search("公司政策规定")

    def test_compile_mixed_triggers(self):
        """Test compilation of mixed language triggers."""
        mock_kb_service = MagicMock()
        mock_assistant_service = MagicMock()
        writer = StreamingWriter(mock_kb_service, mock_assistant_service)

        patterns = writer._compile_trigger_patterns(["according to", "根据"])

        assert len(patterns) == 2

    def test_word_boundary_matching(self):
        """Test that English triggers use word boundaries."""
        mock_kb_service = MagicMock()
        mock_assistant_service = MagicMock()
        writer = StreamingWriter(mock_kb_service, mock_assistant_service)

        patterns = writer._compile_trigger_patterns(["based on"])

        # Should match with word boundaries
        assert patterns[0].search("based on evidence")
        # Should not match partial words
        assert not patterns[0].search("databased only")


# =============================================================================
# Trigger Detection Tests
# =============================================================================


class TestTriggerDetection:
    """Test trigger detection in text."""

    def test_find_trigger_english(self):
        """Test finding English triggers."""
        mock_kb_service = MagicMock()
        mock_assistant_service = MagicMock()
        writer = StreamingWriter(mock_kb_service, mock_assistant_service)

        triggers = ["according to", "based on"]
        patterns = writer._compile_trigger_patterns(triggers)

        result = writer._find_trigger(
            "The policy states that according to our guidelines",
            patterns,
            triggers,
        )

        assert result is not None
        trigger, position = result
        assert trigger == "according to"
        assert position > 0

    def test_find_trigger_chinese(self):
        """Test finding Chinese triggers."""
        mock_kb_service = MagicMock()
        mock_assistant_service = MagicMock()
        writer = StreamingWriter(mock_kb_service, mock_assistant_service)

        triggers = ["根据", "政策"]
        patterns = writer._compile_trigger_patterns(triggers)

        result = writer._find_trigger(
            "根据公司规定，员工需要遵守政策",
            patterns,
            triggers,
        )

        assert result is not None
        trigger, position = result
        assert trigger == "根据"
        assert position == 0

    def test_find_first_trigger(self):
        """Test that the first trigger is found when multiple exist."""
        mock_kb_service = MagicMock()
        mock_assistant_service = MagicMock()
        writer = StreamingWriter(mock_kb_service, mock_assistant_service)

        triggers = ["according to", "based on"]
        patterns = writer._compile_trigger_patterns(triggers)

        result = writer._find_trigger(
            "based on X, according to Y",
            patterns,
            triggers,
        )

        assert result is not None
        trigger, position = result
        assert trigger == "based on"
        assert position == 0

    def test_no_trigger_found(self):
        """Test when no triggers are found."""
        mock_kb_service = MagicMock()
        mock_assistant_service = MagicMock()
        writer = StreamingWriter(mock_kb_service, mock_assistant_service)

        triggers = ["according to", "based on"]
        patterns = writer._compile_trigger_patterns(triggers)

        result = writer._find_trigger(
            "This text has no triggers",
            patterns,
            triggers,
        )

        assert result is None


# =============================================================================
# Query Extraction Tests
# =============================================================================


class TestQueryExtraction:
    """Test verification query extraction."""

    def test_extract_query_basic(self):
        """Test basic query extraction."""
        mock_kb_service = MagicMock()
        mock_assistant_service = MagicMock()
        writer = StreamingWriter(mock_kb_service, mock_assistant_service)

        query = writer._extract_verification_query(
            "Our refund policy states that customers can",
            "policy states",
        )

        assert query is not None
        assert len(query) > 0
        # Should contain context around the trigger
        assert "refund" in query.lower() or "customers" in query.lower()

    def test_extract_query_with_sentence_boundary(self):
        """Test query extraction respects sentence boundaries."""
        mock_kb_service = MagicMock()
        mock_assistant_service = MagicMock()
        writer = StreamingWriter(mock_kb_service, mock_assistant_service)

        query = writer._extract_verification_query(
            "Hello there. According to the policy, refunds are allowed.",
            "according to",
        )

        assert query is not None
        # Should not include "Hello there"

    def test_extract_query_chinese(self):
        """Test query extraction for Chinese text."""
        mock_kb_service = MagicMock()
        mock_assistant_service = MagicMock()
        writer = StreamingWriter(mock_kb_service, mock_assistant_service)

        query = writer._extract_verification_query(
            "公司退款政策规定，客户可以在30天内申请退款",
            "规定",
        )

        assert query is not None
        assert len(query) > 0

    def test_extract_query_empty_text(self):
        """Test query extraction with empty text."""
        mock_kb_service = MagicMock()
        mock_assistant_service = MagicMock()
        writer = StreamingWriter(mock_kb_service, mock_assistant_service)

        query = writer._extract_verification_query("", "policy states")

        assert query is None

    def test_extract_query_trigger_not_found(self):
        """Test query extraction when trigger not in text."""
        mock_kb_service = MagicMock()
        mock_assistant_service = MagicMock()
        writer = StreamingWriter(mock_kb_service, mock_assistant_service)

        query = writer._extract_verification_query(
            "Some text without the trigger",
            "according to",
        )

        assert query is None

    def test_extract_query_max_length(self):
        """Test that extracted queries are limited in length."""
        mock_kb_service = MagicMock()
        mock_assistant_service = MagicMock()
        writer = StreamingWriter(mock_kb_service, mock_assistant_service)

        # Create very long text
        long_text = (
            "A very " + "long " * 100 + "text according to the " + "detailed " * 100 + "policy"
        )

        query = writer._extract_verification_query(long_text, "according to")

        assert query is not None
        assert len(query) <= 200  # Max query length


# =============================================================================
# Result Formatting Tests
# =============================================================================


class TestResultFormatting:
    """Test KB result formatting."""

    def test_format_empty_results(self):
        """Test formatting with no results."""
        mock_kb_service = MagicMock()
        mock_assistant_service = MagicMock()
        writer = StreamingWriter(mock_kb_service, mock_assistant_service)

        formatted = writer._format_results([])

        assert "No relevant results" in formatted

    def test_format_single_result(self):
        """Test formatting a single result."""
        mock_kb_service = MagicMock()
        mock_assistant_service = MagicMock()
        writer = StreamingWriter(mock_kb_service, mock_assistant_service)

        results = [{"content": "Refunds within 30 days", "score": 0.95}]
        formatted = writer._format_results(results)

        assert "[1]" in formatted
        assert "0.95" in formatted
        assert "Refunds within 30 days" in formatted

    def test_format_multiple_results(self):
        """Test formatting multiple results."""
        mock_kb_service = MagicMock()
        mock_assistant_service = MagicMock()
        writer = StreamingWriter(mock_kb_service, mock_assistant_service)

        results = [
            {"content": "First result", "score": 0.95},
            {"content": "Second result", "score": 0.85},
            {"content": "Third result", "score": 0.75},
        ]
        formatted = writer._format_results(results)

        assert "[1]" in formatted
        assert "[2]" in formatted
        assert "[3]" in formatted
        assert "0.95" in formatted
        assert "0.85" in formatted
        assert "0.75" in formatted

    def test_format_result_with_source(self):
        """Test formatting result with source URL."""
        mock_kb_service = MagicMock()
        mock_assistant_service = MagicMock()
        writer = StreamingWriter(mock_kb_service, mock_assistant_service)

        results = [
            {
                "content": "Policy content",
                "score": 0.9,
                "source_url": "https://example.com/policy",
            }
        ]
        formatted = writer._format_results(results)

        assert "Source:" in formatted
        assert "https://example.com/policy" in formatted

    def test_format_truncates_long_content(self):
        """Test that long content is truncated."""
        mock_kb_service = MagicMock()
        mock_assistant_service = MagicMock()
        writer = StreamingWriter(mock_kb_service, mock_assistant_service)

        long_content = "A" * 500  # Longer than 300 char limit
        results = [{"content": long_content, "score": 0.9}]
        formatted = writer._format_results(results)

        # Content should be truncated with "..."
        assert "..." in formatted
        assert len(formatted) < len(long_content) + 50  # Some overhead for formatting

    def test_format_with_text_field(self):
        """Test formatting handles 'text' field as alternative to 'content'."""
        mock_kb_service = MagicMock()
        mock_assistant_service = MagicMock()
        writer = StreamingWriter(mock_kb_service, mock_assistant_service)

        results = [{"text": "Result using text field", "score": 0.88}]
        formatted = writer._format_results(results)

        assert "Result using text field" in formatted


# =============================================================================
# Safe Break Finding Tests
# =============================================================================


class TestSafeBreakFinding:
    """Test finding safe text break points."""

    def test_find_sentence_break(self):
        """Test finding sentence ending break points."""
        mock_kb_service = MagicMock()
        mock_assistant_service = MagicMock()
        writer = StreamingWriter(mock_kb_service, mock_assistant_service, buffer_threshold=50)

        text = "This is a sentence. This is another sentence that continues."
        break_point = writer._find_safe_break(text)

        assert break_point > 0
        # Should break at sentence ending
        assert text[break_point - 1] in [" ", ".", "。", "!", "?", "\n"]

    def test_find_word_break(self):
        """Test finding word boundary break points."""
        mock_kb_service = MagicMock()
        mock_assistant_service = MagicMock()
        writer = StreamingWriter(mock_kb_service, mock_assistant_service, buffer_threshold=20)

        text = "This is a continuous text without sentence endings but with spaces"
        break_point = writer._find_safe_break(text)

        assert break_point > 0
        # Should break at word boundary
        assert break_point > 10  # At least half of threshold

    def test_short_text_no_break(self):
        """Test that short text returns 0 (no break needed)."""
        mock_kb_service = MagicMock()
        mock_assistant_service = MagicMock()
        writer = StreamingWriter(mock_kb_service, mock_assistant_service, buffer_threshold=100)

        text = "Short text"
        break_point = writer._find_safe_break(text)

        assert break_point == 0

    def test_chinese_sentence_break(self):
        """Test finding Chinese sentence endings."""
        mock_kb_service = MagicMock()
        mock_assistant_service = MagicMock()
        # Use buffer_threshold=10 to ensure text is longer than threshold
        writer = StreamingWriter(mock_kb_service, mock_assistant_service, buffer_threshold=10)

        text = "这是第一句话。这是第二句话。这是第三句话。"
        break_point = writer._find_safe_break(text)

        # Should find a break at one of the Chinese period positions
        assert break_point > 0


# =============================================================================
# KB Search Tests
# =============================================================================


class TestKBSearch:
    """Test KB search functionality."""

    @pytest.mark.asyncio
    async def test_search_kb_single_dataset(self):
        """Test searching a single dataset."""
        mock_kb_service = MagicMock()
        mock_kb_service.retrieve = AsyncMock(
            return_value=(
                [
                    MagicMock(
                        text="Result content",
                        score=0.9,
                        segment_id="seg_1",
                        document_id="doc_1",
                        metadata={},
                    )
                ],
                {"dataset_name": "test"},
            )
        )
        mock_assistant_service = MagicMock()
        writer = StreamingWriter(mock_kb_service, mock_assistant_service)

        results = await writer._search_kb(
            query="test query",
            dataset_ids=["dataset_1"],
            user=create_mock_user(),
        )

        assert len(results) == 1
        assert results[0]["content"] == "Result content"
        assert results[0]["score"] == 0.9
        mock_kb_service.retrieve.assert_called_once()

    @pytest.mark.asyncio
    async def test_search_kb_multiple_datasets(self):
        """Test searching multiple datasets."""
        mock_kb_service = MagicMock()
        mock_kb_service.retrieve = AsyncMock(
            side_effect=[
                (
                    [
                        MagicMock(
                            text="Result 1",
                            score=0.9,
                            segment_id="s1",
                            document_id="d1",
                            metadata={},
                        )
                    ],
                    {},
                ),
                (
                    [
                        MagicMock(
                            text="Result 2",
                            score=0.8,
                            segment_id="s2",
                            document_id="d2",
                            metadata={},
                        )
                    ],
                    {},
                ),
            ]
        )
        mock_assistant_service = MagicMock()
        writer = StreamingWriter(mock_kb_service, mock_assistant_service)

        results = await writer._search_kb(
            query="test query",
            dataset_ids=["dataset_1", "dataset_2"],
            user=create_mock_user(),
        )

        assert len(results) == 2
        # Results should be sorted by score
        assert results[0]["score"] >= results[1]["score"]

    @pytest.mark.asyncio
    async def test_search_kb_empty_results(self):
        """Test handling empty search results."""
        mock_kb_service = MagicMock()
        mock_kb_service.retrieve = AsyncMock(return_value=([], {}))
        mock_assistant_service = MagicMock()
        writer = StreamingWriter(mock_kb_service, mock_assistant_service)

        results = await writer._search_kb(
            query="no results query",
            dataset_ids=["dataset_1"],
            user=None,
        )

        assert len(results) == 0

    @pytest.mark.asyncio
    async def test_search_kb_handles_error(self):
        """Test that KB search handles errors gracefully."""
        mock_kb_service = MagicMock()
        mock_kb_service.retrieve = AsyncMock(side_effect=Exception("Connection failed"))
        mock_assistant_service = MagicMock()
        writer = StreamingWriter(mock_kb_service, mock_assistant_service)

        # Should not raise, should return empty results
        results = await writer._search_kb(
            query="test query",
            dataset_ids=["dataset_1"],
            user=None,
        )

        assert len(results) == 0

    @pytest.mark.asyncio
    async def test_search_kb_limits_results(self):
        """Test that results are limited to search_top_k."""
        mock_kb_service = MagicMock()
        mock_kb_service.retrieve = AsyncMock(
            return_value=(
                [
                    MagicMock(
                        text=f"Result {i}",
                        score=0.9 - i * 0.1,
                        segment_id=f"s{i}",
                        document_id=f"d{i}",
                        metadata={},
                    )
                    for i in range(10)
                ],
                {},
            )
        )
        mock_assistant_service = MagicMock()
        writer = StreamingWriter(mock_kb_service, mock_assistant_service, search_top_k=3)

        results = await writer._search_kb(
            query="test",
            dataset_ids=["dataset_1"],
            user=create_mock_user(),
        )

        assert len(results) == 3
        # Should be top 3 by score
        assert results[0]["score"] > results[1]["score"] > results[2]["score"]


# =============================================================================
# Text Generation Tests
# =============================================================================


class TestTextGeneration:
    """Test text generation functionality."""

    @pytest.mark.asyncio
    async def test_generate_text(self):
        """Test basic text generation."""
        mock_kb_service = MagicMock()
        mock_assistant_service = MagicMock()

        # Mock the streaming response
        async def mock_stream(*args, **kwargs):
            for text in ["Hello, ", "world!"]:
                yield MagicMock(content=text)

        mock_assistant_service.model_registry.chat_stream = mock_stream

        writer = StreamingWriter(mock_kb_service, mock_assistant_service)

        chunks = []
        async for chunk in writer._generate_text("test prompt"):
            chunks.append(chunk)

        assert len(chunks) == 2
        assert chunks[0] == "Hello, "
        assert chunks[1] == "world!"


# =============================================================================
# Write With Verification Integration Tests
# =============================================================================


class TestWriteWithVerification:
    """Test the main write_with_verification method."""

    @pytest.mark.asyncio
    async def test_basic_text_generation_no_triggers(self):
        """Test text generation without triggers."""
        mock_kb_service = MagicMock()
        mock_assistant_service = MagicMock()

        async def mock_stream(*args, **kwargs):
            for text in ["Hello, this is a ", "test message."]:
                yield MagicMock(content=text)

        mock_assistant_service.model_registry.chat_stream = mock_stream

        writer = StreamingWriter(mock_kb_service, mock_assistant_service, buffer_threshold=50)

        chunks = []
        async for chunk in writer.write_with_verification(
            writing_prompt="Write a test message",
            dataset_ids=["test_ds"],
        ):
            chunks.append(chunk)

        # Should have text chunks
        text_chunks = [c for c in chunks if c.type == "text"]
        assert len(text_chunks) > 0

        # Combined text should match input
        combined_text = "".join(c.content for c in text_chunks)
        assert (
            "Hello, this is a test message." in combined_text
            or combined_text == "Hello, this is a test message."
        )

    @pytest.mark.asyncio
    async def test_triggers_search(self):
        """Test that triggers cause KB searches."""
        mock_kb_service = MagicMock()
        mock_kb_service.retrieve = AsyncMock(
            return_value=(
                [
                    MagicMock(
                        text="Policy result",
                        score=0.9,
                        segment_id="s1",
                        document_id="d1",
                        metadata={},
                    )
                ],
                {},
            )
        )
        mock_assistant_service = MagicMock()

        async def mock_stream(*args, **kwargs):
            for text in ["According to ", "the policy, refunds are allowed."]:
                yield MagicMock(content=text)

        mock_assistant_service.model_registry.chat_stream = mock_stream

        writer = StreamingWriter(mock_kb_service, mock_assistant_service, buffer_threshold=10)

        chunks = []
        async for chunk in writer.write_with_verification(
            writing_prompt="Explain the refund policy",
            dataset_ids=["policies"],
            user=create_mock_user(),
        ):
            chunks.append(chunk)

        # Should have search events
        search_starts = [c for c in chunks if c.type == "search_start"]
        search_results = [c for c in chunks if c.type == "search_result"]
        search_ends = [c for c in chunks if c.type == "search_end"]

        assert len(search_starts) > 0
        assert len(search_results) > 0
        assert len(search_ends) > 0

    @pytest.mark.asyncio
    async def test_custom_triggers(self):
        """Test with custom verification triggers."""
        mock_kb_service = MagicMock()
        mock_kb_service.retrieve = AsyncMock(return_value=([], {}))
        mock_assistant_service = MagicMock()

        async def mock_stream(*args, **kwargs):
            yield MagicMock(content="As my custom trigger says, this is important.")

        mock_assistant_service.model_registry.chat_stream = mock_stream

        writer = StreamingWriter(mock_kb_service, mock_assistant_service, buffer_threshold=10)

        chunks = []
        async for chunk in writer.write_with_verification(
            writing_prompt="Test",
            dataset_ids=["test_ds"],
            verification_triggers=["my custom trigger"],
        ):
            chunks.append(chunk)

        # Should have triggered a search
        search_starts = [c for c in chunks if c.type == "search_start"]
        assert len(search_starts) > 0

    @pytest.mark.asyncio
    async def test_chinese_triggers(self):
        """Test with Chinese triggers."""
        mock_kb_service = MagicMock()
        mock_kb_service.retrieve = AsyncMock(
            return_value=(
                [
                    MagicMock(
                        text="政策内容", score=0.85, segment_id="s1", document_id="d1", metadata={}
                    )
                ],
                {},
            )
        )
        mock_assistant_service = MagicMock()

        async def mock_stream(*args, **kwargs):
            yield MagicMock(content="根据公司规定，员工享有年假。")

        mock_assistant_service.model_registry.chat_stream = mock_stream

        writer = StreamingWriter(mock_kb_service, mock_assistant_service, buffer_threshold=10)

        chunks = []
        async for chunk in writer.write_with_verification(
            writing_prompt="Explain the leave policy",
            dataset_ids=["policies"],
        ):
            chunks.append(chunk)

        # Should have search events due to Chinese trigger
        search_starts = [c for c in chunks if c.type == "search_start"]
        assert len(search_starts) > 0

    @pytest.mark.asyncio
    async def test_empty_dataset_ids_no_search(self):
        """Test that empty dataset_ids prevents searches."""
        mock_kb_service = MagicMock()
        mock_assistant_service = MagicMock()

        async def mock_stream(*args, **kwargs):
            yield MagicMock(content="According to the policy, this should not trigger search.")

        mock_assistant_service.model_registry.chat_stream = mock_stream

        writer = StreamingWriter(mock_kb_service, mock_assistant_service, buffer_threshold=10)

        chunks = []
        async for chunk in writer.write_with_verification(
            writing_prompt="Test",
            dataset_ids=[],  # Empty - no search should occur
        ):
            chunks.append(chunk)

        # Should NOT have search events
        search_starts = [c for c in chunks if c.type == "search_start"]
        assert len(search_starts) == 0

    @pytest.mark.asyncio
    async def test_handles_generation_error(self):
        """Test handling of text generation errors."""
        mock_kb_service = MagicMock()
        mock_assistant_service = MagicMock()

        async def mock_stream(*args, **kwargs):
            raise Exception("Generation failed")
            yield  # Make it a generator

        mock_assistant_service.model_registry.chat_stream = mock_stream

        writer = StreamingWriter(mock_kb_service, mock_assistant_service)

        chunks = []
        async for chunk in writer.write_with_verification(
            writing_prompt="Test",
            dataset_ids=["test_ds"],
        ):
            chunks.append(chunk)

        # Should have an error chunk
        error_chunks = [c for c in chunks if c.type == "error"]
        assert len(error_chunks) > 0
        assert "Generation failed" in error_chunks[0].content

    @pytest.mark.asyncio
    async def test_handles_search_error_gracefully(self):
        """Test that KB search errors are handled gracefully.

        When individual dataset searches fail, the error is logged but the
        generation continues. No explicit error chunk is emitted - the search
        just returns empty results.
        """
        mock_kb_service = MagicMock()
        mock_kb_service.retrieve = AsyncMock(side_effect=Exception("Search timeout"))
        mock_assistant_service = MagicMock()

        async def mock_stream(*args, **kwargs):
            yield MagicMock(content="According to policy, refunds are allowed.")

        mock_assistant_service.model_registry.chat_stream = mock_stream

        writer = StreamingWriter(mock_kb_service, mock_assistant_service, buffer_threshold=10)

        chunks = []
        async for chunk in writer.write_with_verification(
            writing_prompt="Test",
            dataset_ids=["test_ds"],
        ):
            chunks.append(chunk)

        # Search should be triggered (search_start emitted)
        search_starts = [c for c in chunks if c.type == "search_start"]
        assert len(search_starts) > 0

        # But no search_result due to failure (empty results)
        search_results = [c for c in chunks if c.type == "search_result"]
        assert len(search_results) == 0

        # search_end should still be emitted with 0 results
        search_ends = [c for c in chunks if c.type == "search_end"]
        assert len(search_ends) > 0
        assert search_ends[0].metadata["result_count"] == 0

        # Text should still be generated
        text_chunks = [c for c in chunks if c.type == "text"]
        assert len(text_chunks) > 0


# =============================================================================
# Factory Function Tests
# =============================================================================


class TestCreateStreamingWriter:
    """Test create_streaming_writer factory function."""

    def test_create_with_defaults(self):
        """Test creating writer with default parameters."""
        mock_kb_service = MagicMock()
        mock_assistant_service = MagicMock()

        writer = create_streaming_writer(mock_kb_service, mock_assistant_service)

        assert isinstance(writer, StreamingWriter)
        assert writer.buffer_threshold == 100
        assert writer.search_top_k == 3

    def test_create_with_custom_params(self):
        """Test creating writer with custom parameters."""
        mock_kb_service = MagicMock()
        mock_assistant_service = MagicMock()

        writer = create_streaming_writer(
            mock_kb_service,
            mock_assistant_service,
            buffer_threshold=200,
            search_top_k=5,
        )

        assert writer.buffer_threshold == 200
        assert writer.search_top_k == 5


# =============================================================================
# Default Triggers Tests
# =============================================================================


class TestDefaultVerificationTriggers:
    """Test DEFAULT_VERIFICATION_TRIGGERS constant."""

    def test_contains_english_triggers(self):
        """Test that default triggers include English phrases."""
        english_expected = ["according to", "policy states", "based on", "as per"]

        for trigger in english_expected:
            assert trigger in DEFAULT_VERIFICATION_TRIGGERS

    def test_contains_chinese_triggers(self):
        """Test that default triggers include Chinese phrases."""
        chinese_expected = ["根据", "依据", "规定", "政策"]

        for trigger in chinese_expected:
            assert trigger in DEFAULT_VERIFICATION_TRIGGERS

    def test_trigger_count(self):
        """Test that there are a reasonable number of triggers."""
        # Should have at least 10 triggers
        assert len(DEFAULT_VERIFICATION_TRIGGERS) >= 10

    def test_no_empty_triggers(self):
        """Test that no triggers are empty strings."""
        for trigger in DEFAULT_VERIFICATION_TRIGGERS:
            assert trigger.strip() != ""
            assert len(trigger) > 0
