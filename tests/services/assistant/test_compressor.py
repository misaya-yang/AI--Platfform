"""
Context Compressor Tests

Tests for the context compression functionality:
- CompressedContext: Data container for compressed context
- ContextCompressor: Intelligent context compression for conversations
"""

from __future__ import annotations

import importlib.util
import sys
from typing import Any

import pytest

# Load the compressor module directly without going through __init__.py
# This avoids importing the entire assistant package which has many dependencies
_spec = importlib.util.spec_from_file_location(
    "src.services.assistant.memory.compressor", "src/services/assistant/memory/compressor.py"
)
_compressor_module = importlib.util.module_from_spec(_spec)
sys.modules["src.services.assistant.memory.compressor"] = _compressor_module
_spec.loader.exec_module(_compressor_module)

CompressedContext = _compressor_module.CompressedContext
ContextCompressor = _compressor_module.ContextCompressor
LLMService = _compressor_module.LLMService
PRESERVE_PATTERNS = _compressor_module.PRESERVE_PATTERNS
ARTIFACT_PATTERN = _compressor_module.ARTIFACT_PATTERN
MAX_PRESERVED_URLS = _compressor_module.MAX_PRESERVED_URLS
MAX_PRESERVED_CODE_BLOCKS = _compressor_module.MAX_PRESERVED_CODE_BLOCKS


# =============================================================================
# Test Fixtures
# =============================================================================


class MockLLMService:
    """Mock LLM service for testing."""

    def __init__(self, response: str = "Test summary of the conversation."):
        self.response = response
        self.calls: list[dict[str, Any]] = []

    async def complete(self, prompt: str, max_tokens: int = 200) -> str:
        """Record the call and return mock response."""
        self.calls.append({"prompt": prompt, "max_tokens": max_tokens})
        return self.response


@pytest.fixture
def mock_llm() -> MockLLMService:
    """Create a mock LLM service."""
    return MockLLMService()


@pytest.fixture
def compressor(mock_llm: MockLLMService) -> ContextCompressor:
    """Create a ContextCompressor with mock LLM."""
    return ContextCompressor(llm_service=mock_llm, max_summary_tokens=500)


# =============================================================================
# CompressedContext Tests
# =============================================================================


class TestCompressedContextDataclass:
    """Test CompressedContext dataclass."""

    def test_default_values(self):
        """Test CompressedContext has correct default values."""
        context = CompressedContext(summary="Test summary")

        assert context.summary == "Test summary"
        assert context.preserved_urls == []
        assert context.preserved_code_blocks == []
        assert context.key_artifacts == []
        assert context.recent_messages == []
        assert context.token_count == 0

    def test_full_initialization(self):
        """Test CompressedContext with all fields."""
        context = CompressedContext(
            summary="Summary text",
            preserved_urls=["https://example.com"],
            preserved_code_blocks=["```python\ncode\n```"],
            key_artifacts=["artifact_1"],
            recent_messages=[{"role": "user", "content": "Hello"}],
            token_count=100,
        )

        assert context.summary == "Summary text"
        assert len(context.preserved_urls) == 1
        assert len(context.preserved_code_blocks) == 1
        assert len(context.key_artifacts) == 1
        assert len(context.recent_messages) == 1
        assert context.token_count == 100

    def test_mutable_default_lists_are_independent(self):
        """Test that default lists are independent across instances."""
        context1 = CompressedContext(summary="Summary 1")
        context2 = CompressedContext(summary="Summary 2")

        context1.preserved_urls.append("https://test.com")

        assert len(context1.preserved_urls) == 1
        assert len(context2.preserved_urls) == 0


# =============================================================================
# ContextCompressor Initialization Tests
# =============================================================================


class TestContextCompressorInit:
    """Test ContextCompressor initialization."""

    def test_init_with_defaults(self, mock_llm: MockLLMService):
        """Test initialization with default max_summary_tokens."""
        compressor = ContextCompressor(llm_service=mock_llm)

        assert compressor.llm_service == mock_llm
        assert compressor.max_summary_tokens == 500

    def test_init_with_custom_max_tokens(self, mock_llm: MockLLMService):
        """Test initialization with custom max_summary_tokens."""
        compressor = ContextCompressor(llm_service=mock_llm, max_summary_tokens=200)

        assert compressor.max_summary_tokens == 200


# =============================================================================
# ContextCompressor.compress() Tests
# =============================================================================


class TestContextCompressorCompress:
    """Test ContextCompressor.compress() method."""

    @pytest.mark.asyncio
    async def test_compress_empty_messages(self, compressor: ContextCompressor):
        """Test compressing empty message list."""
        result = await compressor.compress(messages=[], target_tokens=4000)

        assert result.summary == ""
        assert result.preserved_urls == []
        assert result.preserved_code_blocks == []
        assert result.key_artifacts == []
        assert result.recent_messages == []
        assert result.token_count == 0

    @pytest.mark.asyncio
    async def test_compress_fewer_messages_than_preserve_recent(
        self, compressor: ContextCompressor
    ):
        """Test when message count is less than preserve_recent."""
        messages = [
            {"role": "user", "content": "Hello"},
            {"role": "assistant", "content": "Hi there!"},
        ]

        result = await compressor.compress(messages=messages, target_tokens=4000, preserve_recent=6)

        assert result.summary == ""
        assert len(result.recent_messages) == 2
        assert result.recent_messages[0]["content"] == "Hello"

    @pytest.mark.asyncio
    async def test_compress_exact_preserve_recent_count(self, compressor: ContextCompressor):
        """Test when message count equals preserve_recent."""
        messages = [{"role": "user", "content": f"Message {i}"} for i in range(6)]

        result = await compressor.compress(messages=messages, target_tokens=4000, preserve_recent=6)

        assert result.summary == ""
        assert len(result.recent_messages) == 6

    @pytest.mark.asyncio
    async def test_compress_more_messages_than_preserve_recent(self, mock_llm: MockLLMService):
        """Test compressing when there are more messages than preserve_recent."""
        mock_llm.response = "User discussed tasks 1-4 with the assistant."
        compressor = ContextCompressor(llm_service=mock_llm)

        messages = [{"role": "user", "content": f"Message {i}"} for i in range(10)]

        result = await compressor.compress(messages=messages, target_tokens=4000, preserve_recent=6)

        assert result.summary != ""
        assert len(result.recent_messages) == 6
        # Should preserve the last 6 messages
        assert result.recent_messages[0]["content"] == "Message 4"
        assert result.recent_messages[5]["content"] == "Message 9"

    @pytest.mark.asyncio
    async def test_compress_preserves_urls(self, mock_llm: MockLLMService):
        """Test that URLs are extracted and preserved."""
        compressor = ContextCompressor(llm_service=mock_llm)

        messages = [
            {"role": "user", "content": "Check out https://example.com and https://test.org"},
            {"role": "assistant", "content": "I found info at https://docs.example.com"},
            {"role": "user", "content": "Message 3"},
            {"role": "user", "content": "Message 4"},
            {"role": "user", "content": "Message 5"},
            {"role": "user", "content": "Message 6"},
            {"role": "user", "content": "Message 7"},
            {"role": "user", "content": "Message 8"},
        ]

        result = await compressor.compress(messages=messages, target_tokens=4000, preserve_recent=4)

        assert "https://example.com" in result.preserved_urls
        assert "https://test.org" in result.preserved_urls
        assert "https://docs.example.com" in result.preserved_urls

    @pytest.mark.asyncio
    async def test_compress_preserves_code_blocks(self, mock_llm: MockLLMService):
        """Test that code blocks are extracted and preserved."""
        compressor = ContextCompressor(llm_service=mock_llm)

        messages = [
            {
                "role": "user",
                "content": "Here is my code:\n```python\ndef hello():\n    print('world')\n```",
            },
            {
                "role": "assistant",
                "content": "Fixed:\n```python\ndef hello():\n    return 'world'\n```",
            },
            {"role": "user", "content": "Message 3"},
            {"role": "user", "content": "Message 4"},
            {"role": "user", "content": "Message 5"},
            {"role": "user", "content": "Message 6"},
            {"role": "user", "content": "Message 7"},
            {"role": "user", "content": "Message 8"},
        ]

        result = await compressor.compress(messages=messages, target_tokens=4000, preserve_recent=4)

        assert len(result.preserved_code_blocks) == 2

    @pytest.mark.asyncio
    async def test_compress_limits_preserved_urls(self, mock_llm: MockLLMService):
        """Test that preserved URLs are limited to MAX_PRESERVED_URLS."""
        compressor = ContextCompressor(llm_service=mock_llm)

        # Create messages with many URLs
        urls = " ".join([f"https://url{i}.com" for i in range(30)])
        messages = [
            {"role": "user", "content": urls},
        ] + [{"role": "user", "content": f"Message {i}"} for i in range(7)]

        result = await compressor.compress(messages=messages, target_tokens=4000, preserve_recent=4)

        assert len(result.preserved_urls) <= MAX_PRESERVED_URLS

    @pytest.mark.asyncio
    async def test_compress_limits_preserved_code_blocks(self, mock_llm: MockLLMService):
        """Test that preserved code blocks are limited to MAX_PRESERVED_CODE_BLOCKS."""
        compressor = ContextCompressor(llm_service=mock_llm)

        # Create messages with many code blocks
        code_blocks = "\n\n".join([f"```python\ncode_{i}\n```" for i in range(10)])
        messages = [
            {"role": "user", "content": code_blocks},
        ] + [{"role": "user", "content": f"Message {i}"} for i in range(7)]

        result = await compressor.compress(messages=messages, target_tokens=4000, preserve_recent=4)

        assert len(result.preserved_code_blocks) <= MAX_PRESERVED_CODE_BLOCKS

    @pytest.mark.asyncio
    async def test_compress_extracts_artifacts(self, mock_llm: MockLLMService):
        """Test that artifact references are extracted."""
        compressor = ContextCompressor(llm_service=mock_llm)

        messages = [
            {"role": "assistant", "content": "Created artifact_id: my_chart_v1"},
            {"role": "assistant", "content": "Updated artifact-id: my_chart_v2"},
            {"role": "user", "content": "Message 3"},
            {"role": "user", "content": "Message 4"},
            {"role": "user", "content": "Message 5"},
            {"role": "user", "content": "Message 6"},
            {"role": "user", "content": "Message 7"},
            {"role": "user", "content": "Message 8"},
        ]

        result = await compressor.compress(messages=messages, target_tokens=4000, preserve_recent=4)

        assert "my_chart_v1" in result.key_artifacts
        assert "my_chart_v2" in result.key_artifacts

    @pytest.mark.asyncio
    async def test_compress_calculates_token_count(self, mock_llm: MockLLMService):
        """Test that token count is calculated."""
        mock_llm.response = "Short summary."
        compressor = ContextCompressor(llm_service=mock_llm)

        messages = [
            {"role": "user", "content": f"Message {i} with some content"} for i in range(10)
        ]

        result = await compressor.compress(messages=messages, target_tokens=4000, preserve_recent=4)

        assert result.token_count > 0


# =============================================================================
# ContextCompressor._extract_all() Tests
# =============================================================================


class TestContextCompressorExtractAll:
    """Test ContextCompressor._extract_all() method."""

    def test_extract_urls(self, compressor: ContextCompressor):
        """Test extracting URLs from messages."""
        messages = [
            {"role": "user", "content": "Visit https://example.com"},
            {"role": "assistant", "content": "See also http://test.org/path?q=1"},
        ]

        urls = compressor._extract_all(messages, "urls")

        assert "https://example.com" in urls
        assert "http://test.org/path?q=1" in urls

    def test_extract_code_blocks(self, compressor: ContextCompressor):
        """Test extracting code blocks from messages."""
        messages = [
            {"role": "user", "content": "```python\nprint('hello')\n```"},
            {"role": "assistant", "content": "```javascript\nconsole.log('hi');\n```"},
        ]

        blocks = compressor._extract_all(messages, "code_blocks")

        assert len(blocks) == 2
        assert "python" in blocks[0]

    def test_extract_deduplicates_results(self, compressor: ContextCompressor):
        """Test that extraction deduplicates results."""
        messages = [
            {"role": "user", "content": "https://example.com is good"},
            {"role": "assistant", "content": "Yes, https://example.com is great"},
        ]

        urls = compressor._extract_all(messages, "urls")

        assert urls.count("https://example.com") == 1

    def test_extract_invalid_pattern_returns_empty(self, compressor: ContextCompressor):
        """Test that invalid pattern name returns empty list."""
        messages = [{"role": "user", "content": "Some content"}]

        result = compressor._extract_all(messages, "invalid_pattern")

        assert result == []

    def test_extract_empty_messages(self, compressor: ContextCompressor):
        """Test extraction from empty messages list."""
        result = compressor._extract_all([], "urls")

        assert result == []

    def test_extract_handles_complex_content(self, compressor: ContextCompressor):
        """Test extraction from complex content structures."""
        messages = [
            {
                "role": "user",
                "content": [
                    {"type": "text", "text": "Check https://text-block.com"},
                ],
            },
        ]

        urls = compressor._extract_all(messages, "urls")

        assert "https://text-block.com" in urls


# =============================================================================
# ContextCompressor._extract_artifacts() Tests
# =============================================================================


class TestContextCompressorExtractArtifacts:
    """Test ContextCompressor._extract_artifacts() method."""

    def test_extract_artifact_id_format(self, compressor: ContextCompressor):
        """Test extracting artifact_id: format."""
        messages = [
            {"role": "assistant", "content": "Created artifact_id: chart_123"},
        ]

        artifacts = compressor._extract_artifacts(messages)

        assert "chart_123" in artifacts

    def test_extract_artifact_hyphen_format(self, compressor: ContextCompressor):
        """Test extracting artifact-id: format."""
        messages = [
            {"role": "assistant", "content": "Updated artifact-id: report_v2"},
        ]

        artifacts = compressor._extract_artifacts(messages)

        assert "report_v2" in artifacts

    def test_extract_artifact_space_format(self, compressor: ContextCompressor):
        """Test extracting artifact id format with space."""
        messages = [
            {"role": "assistant", "content": "Saved artifact my_doc"},
        ]

        artifacts = compressor._extract_artifacts(messages)

        assert "my_doc" in artifacts

    def test_extract_artifacts_deduplicates(self, compressor: ContextCompressor):
        """Test that artifacts are deduplicated."""
        messages = [
            {"role": "assistant", "content": "Created artifact_id: same_id"},
            {"role": "assistant", "content": "Updated artifact_id: same_id"},
        ]

        artifacts = compressor._extract_artifacts(messages)

        assert artifacts.count("same_id") == 1

    def test_extract_artifacts_empty_messages(self, compressor: ContextCompressor):
        """Test extraction from empty messages."""
        artifacts = compressor._extract_artifacts([])

        assert artifacts == []


# =============================================================================
# ContextCompressor._generate_summary() Tests
# =============================================================================


class TestContextCompressorGenerateSummary:
    """Test ContextCompressor._generate_summary() method."""

    @pytest.mark.asyncio
    async def test_generate_summary_calls_llm(self, mock_llm: MockLLMService):
        """Test that summary generation calls the LLM."""
        compressor = ContextCompressor(llm_service=mock_llm)
        messages = [
            {"role": "user", "content": "Help me with task X"},
            {"role": "assistant", "content": "Sure, here's how..."},
        ]

        await compressor._generate_summary(messages)

        assert len(mock_llm.calls) == 1
        assert "task X" in mock_llm.calls[0]["prompt"]

    @pytest.mark.asyncio
    async def test_generate_summary_returns_response(self, mock_llm: MockLLMService):
        """Test that summary returns the LLM response."""
        mock_llm.response = "User asked about X. Assistant provided solution."
        compressor = ContextCompressor(llm_service=mock_llm)
        messages = [{"role": "user", "content": "Test"}]

        result = await compressor._generate_summary(messages)

        assert result == "User asked about X. Assistant provided solution."

    @pytest.mark.asyncio
    async def test_generate_summary_empty_messages(self, compressor: ContextCompressor):
        """Test summary generation with empty messages."""
        result = await compressor._generate_summary([])

        assert result == ""

    @pytest.mark.asyncio
    async def test_generate_summary_truncates_long_messages(self, mock_llm: MockLLMService):
        """Test that long messages are truncated before summarization."""
        compressor = ContextCompressor(llm_service=mock_llm)
        long_content = "A" * 2000  # Longer than 1000 char limit
        messages = [{"role": "user", "content": long_content}]

        await compressor._generate_summary(messages)

        # The prompt should contain truncated content (1000 chars + "...")
        prompt = mock_llm.calls[0]["prompt"]
        assert "..." in prompt
        assert len(prompt) < len(long_content) + 500  # Prompt overhead

    @pytest.mark.asyncio
    async def test_generate_summary_handles_llm_error(self, mock_llm: MockLLMService):
        """Test fallback when LLM fails."""

        async def failing_complete(prompt: str, max_tokens: int = 200) -> str:
            raise Exception("LLM error")

        mock_llm.complete = failing_complete
        compressor = ContextCompressor(llm_service=mock_llm)
        messages = [
            {"role": "user", "content": "Test 1"},
            {"role": "user", "content": "Test 2"},
        ]

        result = await compressor._generate_summary(messages)

        assert "2 messages compressed" in result

    @pytest.mark.asyncio
    async def test_generate_summary_uses_max_tokens(self, mock_llm: MockLLMService):
        """Test that summary generation respects max_summary_tokens."""
        compressor = ContextCompressor(llm_service=mock_llm, max_summary_tokens=300)
        messages = [{"role": "user", "content": "Test"}]

        await compressor._generate_summary(messages)

        assert mock_llm.calls[0]["max_tokens"] == 300


# =============================================================================
# ContextCompressor._count_tokens() Tests
# =============================================================================


class TestContextCompressorCountTokens:
    """Test ContextCompressor._count_tokens() method."""

    def test_count_tokens_simple_message(self, compressor: ContextCompressor):
        """Test token counting for simple messages."""
        messages = [
            {"role": "user", "content": "Hello world"},  # 11 chars + 20 overhead = 31
        ]

        count = compressor._count_tokens(messages)

        # Rough estimate: (11 + 20) / 4 = 7
        assert count > 0
        assert count < 20

    def test_count_tokens_multiple_messages(self, compressor: ContextCompressor):
        """Test token counting for multiple messages."""
        messages = [
            {"role": "user", "content": "Hello"},
            {"role": "assistant", "content": "Hi there"},
            {"role": "user", "content": "How are you?"},
        ]

        count = compressor._count_tokens(messages)

        assert count > 0

    def test_count_tokens_empty_messages(self, compressor: ContextCompressor):
        """Test token counting for empty messages list."""
        count = compressor._count_tokens([])

        assert count == 0

    def test_count_tokens_complex_content(self, compressor: ContextCompressor):
        """Test token counting for complex content structures."""
        messages = [
            {
                "role": "user",
                "content": [
                    {"type": "text", "text": "Hello world"},
                ],
            },
        ]

        count = compressor._count_tokens(messages)

        assert count > 0


# =============================================================================
# ContextCompressor._get_message_content() Tests
# =============================================================================


class TestContextCompressorGetMessageContent:
    """Test ContextCompressor._get_message_content() method."""

    def test_get_string_content(self, compressor: ContextCompressor):
        """Test extracting string content."""
        message = {"role": "user", "content": "Hello world"}

        content = compressor._get_message_content(message)

        assert content == "Hello world"

    def test_get_list_content_with_text_blocks(self, compressor: ContextCompressor):
        """Test extracting content from list of text blocks."""
        message = {
            "role": "user",
            "content": [
                {"type": "text", "text": "First part"},
                {"type": "text", "text": "Second part"},
            ],
        }

        content = compressor._get_message_content(message)

        assert "First part" in content
        assert "Second part" in content

    def test_get_list_content_with_strings(self, compressor: ContextCompressor):
        """Test extracting content from list of strings."""
        message = {
            "role": "user",
            "content": ["Part one", "Part two"],
        }

        content = compressor._get_message_content(message)

        assert "Part one" in content
        assert "Part two" in content

    def test_get_list_content_with_text_key(self, compressor: ContextCompressor):
        """Test extracting content from dicts with 'text' key."""
        message = {
            "role": "user",
            "content": [
                {"text": "Content text"},
            ],
        }

        content = compressor._get_message_content(message)

        assert "Content text" in content

    def test_get_empty_content(self, compressor: ContextCompressor):
        """Test extracting from message without content."""
        message = {"role": "user"}

        content = compressor._get_message_content(message)

        assert content == ""

    def test_get_non_string_non_list_content(self, compressor: ContextCompressor):
        """Test extracting from non-string, non-list content."""
        message = {"role": "user", "content": 12345}

        content = compressor._get_message_content(message)

        assert content == ""


# =============================================================================
# Module Constants Tests
# =============================================================================


class TestModuleConstants:
    """Test module-level constants."""

    def test_preserve_patterns_has_expected_keys(self):
        """Test PRESERVE_PATTERNS has expected pattern types."""
        assert "urls" in PRESERVE_PATTERNS
        assert "code_blocks" in PRESERVE_PATTERNS
        assert "tables" in PRESERVE_PATTERNS
        assert "json" in PRESERVE_PATTERNS

    def test_preserve_patterns_url_regex(self):
        """Test URL pattern matches valid URLs."""
        import re

        pattern = PRESERVE_PATTERNS["urls"]

        assert re.search(pattern, "Visit https://example.com today")
        assert re.search(pattern, "http://test.org/path?q=1&b=2")
        assert not re.search(pattern, "ftp://not-http.com")

    def test_preserve_patterns_code_blocks_regex(self):
        """Test code blocks pattern matches fenced code."""
        import re

        pattern = PRESERVE_PATTERNS["code_blocks"]

        assert re.search(pattern, "```python\ncode\n```")
        assert re.search(pattern, "```\nplain code\n```")

    def test_artifact_pattern(self):
        """Test ARTIFACT_PATTERN matches artifact references."""
        import re

        assert re.search(ARTIFACT_PATTERN, "artifact_id: my_chart")
        assert re.search(ARTIFACT_PATTERN, "artifact-id: my_chart")
        assert re.search(ARTIFACT_PATTERN, "artifact my_doc")
        assert re.search(ARTIFACT_PATTERN, "artifact: chart_v1")

    def test_max_preserved_limits(self):
        """Test maximum preserved counts are reasonable."""
        assert MAX_PRESERVED_URLS == 20
        assert MAX_PRESERVED_CODE_BLOCKS == 5


# =============================================================================
# LLMService Protocol Tests
# =============================================================================


class TestLLMServiceProtocol:
    """Test LLMService protocol compliance."""

    def test_mock_llm_implements_protocol(self, mock_llm: MockLLMService):
        """Test that MockLLMService implements LLMService protocol."""
        assert isinstance(mock_llm, LLMService)

    def test_protocol_is_runtime_checkable(self):
        """Test that LLMService is runtime checkable."""

        # The protocol should be decorated with @runtime_checkable
        assert hasattr(LLMService, "__protocol_attrs__") or isinstance(MockLLMService(), LLMService)


# =============================================================================
# Integration Tests
# =============================================================================


class TestContextCompressorIntegration:
    """Integration tests for ContextCompressor workflow."""

    @pytest.mark.asyncio
    async def test_full_compression_workflow(self, mock_llm: MockLLMService):
        """Test a complete compression workflow."""
        mock_llm.response = "User discussed Python coding. Assistant helped fix bugs."
        compressor = ContextCompressor(llm_service=mock_llm)

        messages = [
            {
                "role": "user",
                "content": "I need help with Python. Here's my code:\n```python\ndef broken():\n    pass\n```",
            },
            {
                "role": "assistant",
                "content": "I see the issue. Check https://docs.python.org for reference.",
            },
            {"role": "user", "content": "Thanks! Created artifact_id: fixed_code_v1"},
            {"role": "assistant", "content": "Great! Let me know if you need more help."},
            {"role": "user", "content": "One more question about testing."},
            {"role": "assistant", "content": "Sure, what would you like to know?"},
            {"role": "user", "content": "How do I write unit tests?"},
            {
                "role": "assistant",
                "content": "Use pytest. Here's an example:\n```python\ndef test_example():\n    assert True\n```",
            },
            {"role": "user", "content": "That's helpful!"},
            {"role": "assistant", "content": "You're welcome!"},
        ]

        result = await compressor.compress(messages=messages, target_tokens=4000, preserve_recent=4)

        # Verify summary was generated
        assert "Python" in result.summary or "bugs" in result.summary

        # Verify URLs were preserved from compressed messages
        assert "https://docs.python.org" in result.preserved_urls

        # Verify code blocks were preserved
        assert len(result.preserved_code_blocks) > 0

        # Verify artifacts were extracted
        assert "fixed_code_v1" in result.key_artifacts

        # Verify recent messages are preserved
        assert len(result.recent_messages) == 4
        assert result.recent_messages[0]["content"] == "How do I write unit tests?"

        # Verify token count is calculated
        assert result.token_count > 0

    @pytest.mark.asyncio
    async def test_compression_with_minimal_messages(self, mock_llm: MockLLMService):
        """Test compression when barely enough messages to compress."""
        compressor = ContextCompressor(llm_service=mock_llm)

        messages = [
            {"role": "user", "content": "First message - old"},
            {"role": "assistant", "content": "Response 1 - old"},
            {"role": "user", "content": "Second message - old"},
            {"role": "assistant", "content": "Response 2 - old"},
            {"role": "user", "content": "Third message - recent"},
            {"role": "assistant", "content": "Response 3 - recent"},
            {"role": "user", "content": "Fourth message - recent"},
            {"role": "assistant", "content": "Response 4 - recent"},
        ]

        result = await compressor.compress(messages=messages, target_tokens=4000, preserve_recent=4)

        # Should compress first 4 messages (indices 0-3)
        assert result.summary != ""
        # Should preserve last 4 messages (indices 4-7)
        assert len(result.recent_messages) == 4
        # First preserved message should be "Third message - recent" (index 4)
        assert "recent" in result.recent_messages[0]["content"]
        # Last preserved message should be "Response 4 - recent" (index 7)
        assert "recent" in result.recent_messages[3]["content"]
