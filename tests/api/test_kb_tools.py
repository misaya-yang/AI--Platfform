"""
KB Tools API Integration Tests

Tests for the Knowledge Base Tools API endpoints designed for LangGraph agent integration.

Endpoints tested:
- POST /kb/search - Single dataset search
- POST /kb/multi-search - Multi-dataset search
- GET /kb/datasets - List available datasets
- GET /kb/tool-definition/{dataset_id} - Get OpenAI function definition
- GET /kb/tool-definitions - Get all tool definitions
- GET /kb/multi-tool-definition - Get multi-search tool definition
"""

from unittest.mock import AsyncMock, patch

import pytest
from fastapi import HTTPException

from src.api.schemas.kb_tools import (
    KBMultiSearchRequest,
    KBMultiSearchResponse,
    KBSearchRequest,
    KBSearchResponse,
    KBSearchResult,
    get_kb_search_tool_definition,
    get_multi_kb_search_tool_definition,
)
from src.api.v1.kb_tools import (
    _convert_retrieve_result_to_search_result,
    _format_context_for_llm,
    _resolve_mode,
)
from src.core.auth.user_resolver import UserContext

# ============================================================
# Test Fixtures
# ============================================================


@pytest.fixture
def authenticated_user():
    """Create an authenticated user context."""
    return UserContext(
        user_id="test_user_123",
        tenant_id="tenant_001",
        is_authenticated=True,
        roles=["user"],
    )


@pytest.fixture
def admin_user():
    """Create an admin user context."""
    return UserContext(
        user_id="admin_001",
        tenant_id="tenant_001",
        is_authenticated=True,
        roles=["user", "admin"],
    )


@pytest.fixture
def mock_retrieve_result():
    """Create a mock RetrieveResult."""

    class MockResult:
        def __init__(
            self, text, score, segment_id, document_id, content_type="text", metadata=None
        ):
            self.text = text
            self.score = score
            self.segment_id = segment_id
            self.document_id = document_id
            self.content_type = content_type
            self.metadata = metadata or {}
            self.image_url = None
            self.vlm_description = None
            self.associated_images = []

    return MockResult


@pytest.fixture
def sample_retrieve_results(mock_retrieve_result):
    """Create sample retrieve results for testing."""
    return [
        mock_retrieve_result(
            text="Our refund policy allows returns within 30 days.",
            score=0.85,
            segment_id="seg_001",
            document_id="doc_001",
        ),
        mock_retrieve_result(
            text="For refunds, please contact support@example.com.",
            score=0.72,
            segment_id="seg_002",
            document_id="doc_001",
        ),
        mock_retrieve_result(
            text="Shipping costs are non-refundable.",
            score=0.65,
            segment_id="seg_003",
            document_id="doc_002",
        ),
    ]


@pytest.fixture
def mock_knowledge_service(sample_retrieve_results):
    """Create a mock KnowledgeService."""
    service = AsyncMock()

    # Mock require_dataset_access
    service.require_dataset_access = AsyncMock(
        return_value={
            "dataset_id": "test_dataset",
            "name": "Test Dataset",
            "description": "A test dataset",
            "tenant_id": "tenant_001",
        }
    )

    # Mock retrieve
    service.retrieve = AsyncMock(return_value=(sample_retrieve_results, {"took_ms": 50}))

    # Mock retrieve_with_images (for multimodal queries)
    service.retrieve_with_images = AsyncMock(
        return_value=(sample_retrieve_results, {"took_ms": 50, "multimodal": True})
    )

    # Mock list_datasets
    service.list_datasets = AsyncMock(
        return_value=[
            {
                "dataset_id": "docs",
                "name": "Documentation",
                "description": "Product documentation",
                "embedding_provider": "dashscope",
                "embedding_model": "text-embedding-v3",
            },
            {
                "dataset_id": "faq",
                "name": "FAQ",
                "description": "Frequently asked questions",
                "embedding_provider": "dashscope",
                "embedding_model": "text-embedding-v3",
            },
        ]
    )

    # Mock get_dataset_statistics
    service.get_dataset_statistics = AsyncMock(
        return_value={
            "document_count": 10,
            "segment_count": 150,
        }
    )

    return service


# ============================================================
# Helper Function Tests
# ============================================================


class TestResolveMode:
    """Tests for _resolve_mode helper."""

    def test_returns_specified_mode_when_not_auto(self):
        """Should return the specified mode when not 'auto'."""
        assert _resolve_mode("hybrid", "some query") == "hybrid"
        assert _resolve_mode("dense", "some query") == "dense"
        assert _resolve_mode("bm25", "some query") == "bm25"

    def test_auto_mode_with_short_query(self):
        """Should return hybrid for short queries in auto mode."""
        assert _resolve_mode("auto", "refund") == "hybrid"
        assert _resolve_mode("auto", "api key") == "hybrid"

    def test_auto_mode_with_long_query(self):
        """Should return hybrid for longer queries in auto mode."""
        assert _resolve_mode("auto", "How do I reset my password in the admin panel?") == "hybrid"


class TestFormatContextForLLM:
    """Tests for _format_context_for_llm helper."""

    def test_empty_results_returns_no_results_message(self):
        """Should return a 'no results' message for empty results."""
        result = _format_context_for_llm([], "test query")
        assert "No relevant information found" in result

    def test_formats_single_result_correctly(self):
        """Should format a single result with score and content."""
        results = [
            KBSearchResult(
                content="Test content here",
                score=0.85,
                segment_id="seg_001",
                document_id="doc_001",
                dataset_id="test_ds",
                content_type="text",
                metadata={},
            )
        ]
        formatted = _format_context_for_llm(results, "test query")

        assert "[1]" in formatted
        assert "0.850" in formatted
        assert "Test content here" in formatted

    def test_formats_multiple_results_with_separator(self):
        """Should separate multiple results with dividers."""
        results = [
            KBSearchResult(
                content=f"Content {i}",
                score=0.9 - i * 0.1,
                segment_id=f"seg_{i}",
                document_id="doc_001",
                dataset_id="test_ds",
                content_type="text",
                metadata={},
            )
            for i in range(3)
        ]
        formatted = _format_context_for_llm(results, "test query")

        assert "[1]" in formatted
        assert "[2]" in formatted
        assert "[3]" in formatted
        assert "---" in formatted  # Separator

    def test_truncates_when_exceeding_max_length(self):
        """Should truncate results when exceeding max length."""
        long_content = "x" * 5000
        results = [
            KBSearchResult(
                content=long_content,
                score=0.9 - i * 0.1,
                segment_id=f"seg_{i}",
                document_id="doc_001",
                dataset_id="test_ds",
                content_type="text",
                metadata={},
            )
            for i in range(5)
        ]
        formatted = _format_context_for_llm(results, "test query", max_length=8000)

        assert "truncated" in formatted

    def test_formats_image_result_with_vlm_description(self):
        """Should use VLM description for image content."""
        results = [
            KBSearchResult(
                content="",
                score=0.85,
                segment_id="seg_001",
                document_id="doc_001",
                dataset_id="test_ds",
                content_type="image",
                metadata={},
                vlm_description="A diagram showing the architecture",
            )
        ]
        formatted = _format_context_for_llm(results, "test query")

        assert "[Image:" in formatted
        assert "architecture" in formatted


class TestConvertRetrieveResult:
    """Tests for _convert_retrieve_result_to_search_result helper."""

    def test_converts_basic_result(self, mock_retrieve_result):
        """Should convert a basic retrieve result correctly."""
        result = mock_retrieve_result(
            text="Test content",
            score=0.85,
            segment_id="seg_001",
            document_id="doc_001",
        )

        converted = _convert_retrieve_result_to_search_result(result, "test_ds")

        assert converted.content == "Test content"
        assert converted.score == 0.85
        assert converted.segment_id == "seg_001"
        assert converted.document_id == "doc_001"
        assert converted.dataset_id == "test_ds"
        assert converted.content_type == "text"

    def test_handles_associated_images(self, mock_retrieve_result):
        """Should convert associated images correctly."""

        class MockAssociatedImage:
            def __init__(self):
                self.image_segment_id = "img_001"
                self.storage_url = "https://example.com/img.png"
                self.filename = "diagram.png"
                self.vlm_description = "A diagram"
                self.proximity_score = 0.95
                self.media_type = "image/png"

        result = mock_retrieve_result(
            text="Text with image",
            score=0.85,
            segment_id="seg_001",
            document_id="doc_001",
        )
        result.associated_images = [MockAssociatedImage()]

        converted = _convert_retrieve_result_to_search_result(result, "test_ds")

        assert len(converted.associated_images) == 1
        assert converted.associated_images[0].image_segment_id == "img_001"
        assert converted.associated_images[0].filename == "diagram.png"


# ============================================================
# Schema Tests
# ============================================================


class TestKBSearchRequest:
    """Tests for KBSearchRequest schema."""

    def test_default_values(self):
        """Should have sensible defaults."""
        request = KBSearchRequest(query="test", dataset_id="ds_001")

        assert request.top_k == 5
        assert request.mode == "auto"
        assert request.rerank is False
        assert request.mmr is False
        assert request.score_threshold is None

    def test_custom_values(self):
        """Should accept custom values."""
        request = KBSearchRequest(
            query="test query",
            dataset_id="ds_001",
            top_k=10,
            mode="hybrid",
            rerank=True,
            mmr=True,
            score_threshold=0.5,
        )

        assert request.top_k == 10
        assert request.mode == "hybrid"
        assert request.rerank is True
        assert request.mmr is True
        assert request.score_threshold == 0.5


class TestKBToolDefinition:
    """Tests for tool definition generation."""

    def test_single_dataset_tool_definition(self):
        """Should generate valid OpenAI function definition."""
        definition = get_kb_search_tool_definition("docs", "Documentation")

        assert definition.type == "function"
        assert definition.function["name"] == "search_docs"
        assert "Documentation" in definition.function["description"]
        assert "query" in definition.function["parameters"]["properties"]

    def test_multi_dataset_tool_definition(self):
        """Should generate multi-dataset tool definition."""
        dataset_ids = ["docs", "faq", "wiki"]
        dataset_names = {"docs": "Documentation", "faq": "FAQ", "wiki": "Wiki"}

        definition = get_multi_kb_search_tool_definition(dataset_ids, dataset_names)

        assert definition.type == "function"
        assert "search" in definition.function["name"]
        assert "dataset_id" in definition.function["parameters"]["properties"]


# ============================================================
# API Endpoint Tests (Unit)
# ============================================================


class TestKBSearchEndpoint:
    """Tests for POST /kb/search endpoint."""

    @pytest.mark.asyncio
    async def test_search_returns_formatted_context(
        self,
        mock_knowledge_service,
        authenticated_user,
    ):
        """Should return both structured results and formatted context."""
        from src.api.v1.kb_tools import kb_search

        request = KBSearchRequest(
            query="What is the refund policy?",
            dataset_id="docs",
            top_k=5,
        )

        # Patch dependencies
        with patch(
            "src.api.v1.kb_tools.get_knowledge_service", return_value=mock_knowledge_service
        ):
            response = await kb_search(request, mock_knowledge_service, authenticated_user)

        assert isinstance(response, KBSearchResponse)
        assert len(response.results) == 3
        assert response.formatted_context is not None
        assert "[1]" in response.formatted_context
        assert response.query == "What is the refund policy?"
        assert response.dataset_id == "docs"

    @pytest.mark.asyncio
    async def test_search_respects_top_k(
        self,
        mock_knowledge_service,
        authenticated_user,
    ):
        """Should pass top_k parameter to retrieval."""
        from src.api.v1.kb_tools import kb_search

        request = KBSearchRequest(
            query="test query",
            dataset_id="docs",
            top_k=10,
        )

        with patch(
            "src.api.v1.kb_tools.get_knowledge_service", return_value=mock_knowledge_service
        ):
            await kb_search(request, mock_knowledge_service, authenticated_user)

        # Verify top_k was passed (uses retrieve_with_images by default due to include_images=True)
        mock_knowledge_service.retrieve_with_images.assert_called_once()
        call_kwargs = mock_knowledge_service.retrieve_with_images.call_args.kwargs
        assert call_kwargs["top_k"] == 10

    @pytest.mark.asyncio
    async def test_search_handles_permission_denied(
        self,
        mock_knowledge_service,
        authenticated_user,
    ):
        """Should return 403 when user doesn't have access."""
        from src.api.v1.kb_tools import kb_search
        from src.core.exceptions import PermissionDeniedError

        mock_knowledge_service.require_dataset_access.side_effect = PermissionDeniedError(
            "No access to dataset"
        )

        request = KBSearchRequest(query="test", dataset_id="private_ds")

        with pytest.raises(HTTPException) as exc_info:
            await kb_search(request, mock_knowledge_service, authenticated_user)

        assert exc_info.value.status_code == 403

    @pytest.mark.asyncio
    async def test_search_handles_dataset_not_found(
        self,
        mock_knowledge_service,
        authenticated_user,
    ):
        """Should return 404 when dataset doesn't exist."""
        from src.api.v1.kb_tools import kb_search
        from src.core.exceptions import ValidationFailedError

        mock_knowledge_service.require_dataset_access.side_effect = ValidationFailedError(
            "Dataset not found"
        )

        request = KBSearchRequest(query="test", dataset_id="nonexistent")

        with pytest.raises(HTTPException) as exc_info:
            await kb_search(request, mock_knowledge_service, authenticated_user)

        assert exc_info.value.status_code == 404


class TestKBMultiSearchEndpoint:
    """Tests for POST /kb/multi-search endpoint."""

    @pytest.mark.asyncio
    async def test_multi_search_searches_all_datasets(
        self,
        mock_knowledge_service,
        authenticated_user,
        sample_retrieve_results,
    ):
        """Should search all specified datasets."""
        from src.api.v1.kb_tools import kb_multi_search

        request = KBMultiSearchRequest(
            query="How to reset password?",
            dataset_ids=["docs", "faq"],
            top_k=5,
        )

        with patch(
            "src.api.v1.kb_tools.get_knowledge_service", return_value=mock_knowledge_service
        ):
            response = await kb_multi_search(request, mock_knowledge_service, authenticated_user)

        assert isinstance(response, KBMultiSearchResponse)
        assert response.query == "How to reset password?"
        assert "docs" in response.dataset_ids or "faq" in response.dataset_ids

    @pytest.mark.asyncio
    async def test_multi_search_with_score_merge(
        self,
        mock_knowledge_service,
        authenticated_user,
    ):
        """Should merge results by score when specified."""
        from src.api.v1.kb_tools import kb_multi_search

        request = KBMultiSearchRequest(
            query="test",
            dataset_ids=["docs", "faq"],
            merge_strategy="score",
        )

        with patch(
            "src.api.v1.kb_tools.get_knowledge_service", return_value=mock_knowledge_service
        ):
            response = await kb_multi_search(request, mock_knowledge_service, authenticated_user)

        # Results should be sorted by score (descending)
        if len(response.results) > 1:
            for i in range(len(response.results) - 1):
                assert response.results[i].score >= response.results[i + 1].score

    @pytest.mark.asyncio
    async def test_multi_search_skips_inaccessible_datasets(
        self,
        mock_knowledge_service,
        authenticated_user,
    ):
        """Should skip datasets user can't access without failing."""
        from src.api.v1.kb_tools import kb_multi_search
        from src.core.exceptions import PermissionDeniedError

        # First dataset accessible, second denied
        access_count = [0]

        async def mock_access(user, ds_id, required):
            access_count[0] += 1
            if ds_id == "private":
                raise PermissionDeniedError("No access")
            return {"dataset_id": ds_id, "name": ds_id}

        mock_knowledge_service.require_dataset_access = mock_access

        request = KBMultiSearchRequest(
            query="test",
            dataset_ids=["docs", "private"],
        )

        with patch(
            "src.api.v1.kb_tools.get_knowledge_service", return_value=mock_knowledge_service
        ):
            response = await kb_multi_search(request, mock_knowledge_service, authenticated_user)

        # Should only include accessible dataset
        assert "docs" in response.dataset_ids
        assert "private" not in response.dataset_ids


class TestListDatasetsEndpoint:
    """Tests for GET /kb/datasets endpoint."""

    @pytest.mark.asyncio
    async def test_list_datasets_returns_available_datasets(
        self,
        mock_knowledge_service,
        authenticated_user,
    ):
        """Should return list of datasets available to user."""
        from src.api.v1.kb_tools import list_datasets

        with patch(
            "src.api.v1.kb_tools.get_knowledge_service", return_value=mock_knowledge_service
        ):
            response = await list_datasets(
                include_stats=False,
                svc=mock_knowledge_service,
                user=authenticated_user,
            )

        assert response.total == 2
        assert len(response.datasets) == 2
        assert response.datasets[0].dataset_id == "docs"
        assert response.datasets[1].dataset_id == "faq"

    @pytest.mark.asyncio
    async def test_list_datasets_with_stats(
        self,
        mock_knowledge_service,
        authenticated_user,
    ):
        """Should include stats when requested."""
        from src.api.v1.kb_tools import list_datasets

        with patch(
            "src.api.v1.kb_tools.get_knowledge_service", return_value=mock_knowledge_service
        ):
            response = await list_datasets(
                include_stats=True,
                svc=mock_knowledge_service,
                user=authenticated_user,
            )

        # Stats should be populated
        assert response.datasets[0].document_count == 10
        assert response.datasets[0].segment_count == 150


class TestToolDefinitionEndpoint:
    """Tests for GET /kb/tool-definition/{dataset_id} endpoint."""

    @pytest.mark.asyncio
    async def test_get_tool_definition(
        self,
        mock_knowledge_service,
        authenticated_user,
    ):
        """Should return valid OpenAI function definition."""
        from src.api.v1.kb_tools import get_tool_definition

        mock_knowledge_service.require_dataset_access = AsyncMock(
            return_value={
                "dataset_id": "docs",
                "name": "Documentation",
            }
        )

        with patch(
            "src.api.v1.kb_tools.get_knowledge_service", return_value=mock_knowledge_service
        ):
            response = await get_tool_definition(
                dataset_id="docs",
                svc=mock_knowledge_service,
                user=authenticated_user,
            )

        assert response.type == "function"
        assert response.function["name"] == "search_docs"

    @pytest.mark.asyncio
    async def test_get_tool_definition_permission_denied(
        self,
        mock_knowledge_service,
        authenticated_user,
    ):
        """Should return 403 when user can't access dataset."""
        from src.api.v1.kb_tools import get_tool_definition
        from src.core.exceptions import PermissionDeniedError

        mock_knowledge_service.require_dataset_access.side_effect = PermissionDeniedError(
            "No access"
        )

        with pytest.raises(HTTPException) as exc_info:
            await get_tool_definition(
                dataset_id="private",
                svc=mock_knowledge_service,
                user=authenticated_user,
            )

        assert exc_info.value.status_code == 403


class TestGetAllToolDefinitions:
    """Tests for GET /kb/tool-definitions endpoint."""

    @pytest.mark.asyncio
    async def test_returns_definitions_for_all_datasets(
        self,
        mock_knowledge_service,
        authenticated_user,
    ):
        """Should return tool definitions for all accessible datasets."""
        from src.api.v1.kb_tools import get_all_tool_definitions

        with patch(
            "src.api.v1.kb_tools.get_knowledge_service", return_value=mock_knowledge_service
        ):
            response = await get_all_tool_definitions(
                svc=mock_knowledge_service,
                user=authenticated_user,
            )

        assert len(response) == 2
        assert all(d.type == "function" for d in response)


class TestMultiToolDefinition:
    """Tests for GET /kb/multi-tool-definition endpoint."""

    @pytest.mark.asyncio
    async def test_returns_combined_tool_definition(
        self,
        mock_knowledge_service,
        authenticated_user,
    ):
        """Should return a combined tool definition for multiple datasets."""
        from src.api.v1.kb_tools import get_multi_search_tool_definition

        mock_knowledge_service.require_dataset_access = AsyncMock(
            return_value={
                "dataset_id": "docs",
                "name": "Documentation",
            }
        )

        with patch(
            "src.api.v1.kb_tools.get_knowledge_service", return_value=mock_knowledge_service
        ):
            response = await get_multi_search_tool_definition(
                dataset_ids="docs,faq",
                svc=mock_knowledge_service,
                user=authenticated_user,
            )

        assert response.type == "function"
        assert "dataset_id" in response.function["parameters"]["properties"]


# ============================================================
# Integration Simulation Tests
# ============================================================


class TestLangGraphAgentSimulation:
    """Simulated tests for LangGraph agent integration patterns."""

    def test_tool_definition_matches_openai_format(self):
        """Tool definition should match OpenAI function calling format."""
        definition = get_kb_search_tool_definition("docs", "Documentation")

        # Verify OpenAI function calling structure
        assert definition.type == "function"
        assert "name" in definition.function
        assert "description" in definition.function
        assert "parameters" in definition.function
        assert definition.function["parameters"]["type"] == "object"
        assert "query" in definition.function["parameters"]["properties"]
        assert "required" in definition.function["parameters"]
        assert "query" in definition.function["parameters"]["required"]

    def test_formatted_context_suitable_for_llm(self):
        """Formatted context should be immediately usable in LLM prompts."""
        results = [
            KBSearchResult(
                content="Refund policy: Returns accepted within 30 days.",
                score=0.85,
                segment_id="seg_001",
                document_id="doc_001",
                dataset_id="docs",
                content_type="text",
                metadata={},
            ),
            KBSearchResult(
                content="Contact support@example.com for refund requests.",
                score=0.72,
                segment_id="seg_002",
                document_id="doc_001",
                dataset_id="docs",
                content_type="text",
                metadata={},
            ),
        ]

        formatted = _format_context_for_llm(results, "refund policy")

        # Should be human-readable and LLM-friendly
        assert "[1]" in formatted
        assert "[2]" in formatted
        assert "Refund policy" in formatted
        assert "support@example.com" in formatted

        # Should have reasonable length
        assert len(formatted) < 8000

    def test_search_request_schema_validates_correctly(self):
        """Search request schema should validate agent inputs."""
        # Valid request
        valid_request = KBSearchRequest(
            query="How do I reset my password?",
            dataset_id="docs",
            top_k=5,
        )
        assert valid_request.query == "How do I reset my password?"

        # Invalid request (missing required fields) should raise
        with pytest.raises(ValueError):
            KBSearchRequest(query="test")  # Missing dataset_id

    def test_response_schema_provides_complete_information(self):
        """Response schema should provide all info an agent needs."""
        results = [
            KBSearchResult(
                content="Test content",
                score=0.85,
                segment_id="seg_001",
                document_id="doc_001",
                dataset_id="docs",
                content_type="text",
                metadata={"source": "manual"},
            )
        ]

        # Create response
        response = KBSearchResponse(
            results=results,
            formatted_context="[1] Test content",
            query="test",
            dataset_id="docs",
            total_results=1,
            metadata={"mode": "hybrid"},
        )

        # Agent should have access to:
        # 1. Structured results for programmatic access
        assert len(response.results) == 1
        assert response.results[0].score == 0.85

        # 2. Formatted context for direct LLM injection
        assert response.formatted_context is not None

        # 3. Metadata for debugging/logging
        assert response.metadata["mode"] == "hybrid"

        # 4. Query echo for verification
        assert response.query == "test"
