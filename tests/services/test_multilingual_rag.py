"""
Test suite for Multilingual RAG improvements.

Covers:
- Arabic token calibration
- RRF fusion
- Cross-language query expansion
- Multilingual embedding
- Multilingual reranking
"""

import math
from unittest.mock import patch

import pytest

# =============================================================================
# Test: Arabic Token Calibration
# =============================================================================


class TestArabicTokenCalibration:
    """Test Arabic-aware token counting and chunk sizing."""

    @pytest.fixture
    def sample_arabic_text(self) -> str:
        return """
        بِسْمِ اللَّهِ الرَّحْمَٰنِ الرَّحِيمِ
        الْحَمْدُ لِلَّهِ رَبِّ الْعَالَمِينَ
        الرَّحْمَٰنِ الرَّحِيمِ
        مَالِكِ يَوْمِ الدِّينِ
        إِيَّاكَ نَعْبُدُ وَإِيَّاكَ نَسْتَعِينُ
        """

    @pytest.fixture
    def sample_english_text(self) -> str:
        return """
        In the name of God, the Most Gracious, the Most Merciful.
        All praise is due to God, Lord of the Worlds.
        The Most Gracious, the Most Merciful.
        Master of the Day of Judgment.
        You alone we worship, and You alone we ask for help.
        """

    @pytest.fixture
    def sample_mixed_text(self) -> str:
        return """
        The Quran (القرآن الكريم) begins with Surah Al-Fatiha.
        بِسْمِ اللَّهِ الرَّحْمَٰنِ الرَّحِيمِ
        This is the opening chapter of the Holy Quran.
        الْحَمْدُ لِلَّهِ رَبِّ الْعَالَمِينَ
        """

    def test_detect_language_arabic(self, sample_arabic_text):
        """Detect Arabic as primary language."""
        from knowledge_service.services.knowledge.chunking import detect_text_language

        lang, confidence = detect_text_language(sample_arabic_text)
        assert lang == "ar"
        assert confidence > 0.8

    def test_detect_language_english(self, sample_english_text):
        """Detect English as primary language."""
        from knowledge_service.services.knowledge.chunking import detect_text_language

        lang, confidence = detect_text_language(sample_english_text)
        assert lang == "en"
        assert confidence > 0.8

    def test_detect_language_mixed(self, sample_mixed_text):
        """Detect mixed content."""
        from knowledge_service.services.knowledge.chunking import detect_text_language

        lang, _ = detect_text_language(sample_mixed_text)
        assert lang in ("ar", "en", "mixed")

    def test_arabic_token_multiplier(self):
        """Arabic should have higher token multiplier."""
        from knowledge_service.services.knowledge.chunking import get_chunk_size_for_language

        ar_size = get_chunk_size_for_language("ar", base_chunk_size=1000)
        en_size = get_chunk_size_for_language("en", base_chunk_size=1000)

        # Arabic needs ~15% smaller chunks (1.15x token ratio)
        assert ar_size < en_size
        assert ar_size == pytest.approx(870, rel=0.05)  # 1000 / 1.15

    def test_chunk_arabic_respects_boundaries(self, sample_arabic_text):
        """Chunking should respect Arabic sentence boundaries."""
        from knowledge_service.services.knowledge.chunking import chunk_text

        chunks = chunk_text(sample_arabic_text, chunk_size=100, chunk_overlap=20, language="ar")

        # Should have multiple chunks
        assert len(chunks) > 0

        # Each chunk should be non-empty
        for chunk in chunks:
            assert len(chunk.strip()) > 0


# =============================================================================
# Test: RRF Fusion
# =============================================================================


class TestRRFFusion:
    """Test Reciprocal Rank Fusion implementation."""

    def test_rrf_basic_fusion(self):
        """RRF should combine rankings correctly."""
        from knowledge_service.services.knowledge.retrieval_v2 import rrf_fusion

        # Simulate two retrieval methods
        dense_results = [
            {"id": "doc1", "score": 0.95},
            {"id": "doc2", "score": 0.85},
            {"id": "doc3", "score": 0.75},
        ]
        sparse_results = [
            {"id": "doc2", "score": 0.90},
            {"id": "doc4", "score": 0.80},
            {"id": "doc1", "score": 0.70},
        ]

        fused = rrf_fusion([dense_results, sparse_results], k=60)

        # doc1 and doc2 should rank highest (appear in both)
        fused_ids = [r["id"] for r in fused]
        assert "doc1" in fused_ids[:2]
        assert "doc2" in fused_ids[:2]

    def test_rrf_k_parameter(self):
        """K parameter should affect score smoothing."""
        from knowledge_service.services.knowledge.retrieval_v2 import rrf_fusion

        results = [[{"id": f"doc{i}", "score": 1.0 - i * 0.1} for i in range(5)]]

        # Lower k = more emphasis on top ranks
        fused_k20 = rrf_fusion(results, k=20)
        fused_k60 = rrf_fusion(results, k=60)

        # Scores should be positive and ordered
        for r in fused_k20:
            assert r["rrf_score"] > 0
        for r in fused_k60:
            assert r["rrf_score"] > 0

    def test_rrf_with_empty_results(self):
        """RRF should handle empty result lists."""
        from knowledge_service.services.knowledge.retrieval_v2 import rrf_fusion

        fused = rrf_fusion([[], [{"id": "doc1", "score": 0.9}]], k=60)

        assert len(fused) == 1
        assert fused[0]["id"] == "doc1"

    def test_rrf_score_formula(self):
        """Verify RRF score formula: 1 / (k + rank)."""
        from knowledge_service.services.knowledge.retrieval_v2 import rrf_fusion

        results = [
            [
                {"id": "doc1", "score": 0.9},
                {"id": "doc2", "score": 0.8},
            ]
        ]

        k = 60
        fused = rrf_fusion(results, k=k)

        # First document: rank=1, score = 1/(60+1) = 0.0164
        # Second document: rank=2, score = 1/(60+2) = 0.0161
        expected_score_1 = 1.0 / (k + 1)
        expected_score_2 = 1.0 / (k + 2)

        assert fused[0]["rrf_score"] == pytest.approx(expected_score_1, rel=0.01)
        assert fused[1]["rrf_score"] == pytest.approx(expected_score_2, rel=0.01)


# =============================================================================
# Test: Cross-Language Query Expansion
# =============================================================================


class TestCrossLanguageQueryExpansion:
    """Test query expansion for cross-language retrieval."""

    @pytest.fixture
    def query_expander(self):
        """Create query expander instance."""
        from knowledge_service.services.knowledge.retrieval_service import CrossLanguageQueryExpander

        return CrossLanguageQueryExpander()

    def test_detect_arabic_query(self, query_expander):
        """Detect Arabic query language."""
        query = "ما هي أركان الإسلام الخمسة؟"
        lang = query_expander.detect_query_language(query)
        assert lang == "ar"

    def test_detect_english_query(self, query_expander):
        """Detect English query language."""
        query = "What are the five pillars of Islam?"
        lang = query_expander.detect_query_language(query)
        assert lang == "en"

    @pytest.mark.asyncio
    async def test_expand_arabic_query(self, query_expander):
        """Arabic query should generate English expansion."""
        query = "الزكاة"  # Zakat

        with patch.object(query_expander, "_translate_query") as mock_translate:
            mock_translate.return_value = "zakat almsgiving charity"

            expansions = await query_expander.expand_query(query)

            assert len(expansions) > 0
            # Should include English expansion
            assert any("zakat" in e.lower() for e in expansions)

    @pytest.mark.asyncio
    async def test_expand_english_query(self, query_expander):
        """English query should generate Arabic expansion."""
        query = "prayer times"

        with patch.object(query_expander, "_translate_query") as mock_translate:
            mock_translate.return_value = "مواقيت الصلاة"

            expansions = await query_expander.expand_query(query)

            assert len(expansions) > 0

    def test_islamic_term_normalization(self, query_expander):
        """Common Islamic terms should be normalized."""
        variants = [
            "Quran",
            "Qur'an",
            "Koran",
            "القرآن",
            "Mohammed",
            "Muhammad",
            "محمد",
            "Ramadan",
            "Ramadhan",
            "رمضان",
        ]

        # These should all normalize to canonical forms
        for variant in variants:
            normalized = query_expander.normalize_islamic_term(variant)
            assert normalized is not None
            assert len(normalized) > 0


# =============================================================================
# Test: Multilingual Embedding
# =============================================================================


class TestMultilingualEmbedding:
    """Test multilingual embedding with BGE-M3."""

    @pytest.fixture
    def embedding_config(self):
        from knowledge_service.services.knowledge.multilingual_embedding import MultilingualEmbeddingConfig

        return MultilingualEmbeddingConfig(
            provider="bge-m3",
            model="BAAI/bge-m3",
            return_sparse=True,
            return_colbert=False,
        )

    def test_config_defaults(self, embedding_config):
        """Config should have sensible defaults."""
        assert embedding_config.dimension == 1024
        assert embedding_config.return_sparse is True
        assert embedding_config.max_batch_size == 32

    def test_embedding_result_structure(self):
        """Embedding result should have correct structure."""
        from knowledge_service.services.knowledge.multilingual_embedding import MultilingualEmbeddingResult

        result = MultilingualEmbeddingResult(
            dense_vector=[0.1] * 1024,
            sparse_weights={"hello": 0.5, "world": 0.3},
            colbert_vectors=None,
            model="test-model",
        )

        assert result.dimension == 1024
        assert len(result.sparse_weights) == 2
        assert result.colbert_vectors is None

    def test_sparse_score_computation(self):
        """Sparse score should be dot product of overlapping tokens."""
        from knowledge_service.services.knowledge.multilingual_embedding import compute_sparse_score

        query_sparse = {"hello": 0.5, "world": 0.3, "test": 0.2}
        doc_sparse = {"hello": 0.8, "world": 0.4, "other": 0.1}

        score = compute_sparse_score(query_sparse, doc_sparse)

        # Expected: 0.5*0.8 + 0.3*0.4 = 0.4 + 0.12 = 0.52
        expected = 0.5 * 0.8 + 0.3 * 0.4
        assert score == pytest.approx(expected, rel=0.01)

    def test_sparse_score_no_overlap(self):
        """No overlap should give zero score."""
        from knowledge_service.services.knowledge.multilingual_embedding import compute_sparse_score

        query_sparse = {"a": 0.5, "b": 0.3}
        doc_sparse = {"c": 0.8, "d": 0.4}

        score = compute_sparse_score(query_sparse, doc_sparse)
        assert score == 0.0

    def test_hybrid_score_computation(self):
        """Hybrid score should combine dense and sparse."""
        from knowledge_service.services.knowledge.multilingual_embedding import (
            MultilingualEmbeddingResult,
            compute_hybrid_score,
        )

        # Create two normalized vectors (cosine sim = 1.0)
        vec = [0.5] * 4
        norm = math.sqrt(sum(x * x for x in vec))
        normalized_vec = [x / norm for x in vec]

        query = MultilingualEmbeddingResult(
            dense_vector=normalized_vec,
            sparse_weights={"term": 1.0},
        )
        doc = MultilingualEmbeddingResult(
            dense_vector=normalized_vec,
            sparse_weights={"term": 1.0},
        )

        score = compute_hybrid_score(query, doc, dense_weight=0.6, sparse_weight=0.4)

        # Dense similarity = 1.0, sparse score normalized
        assert score > 0.5

    def test_factory_creates_bge_embedder(self, embedding_config):
        """Factory should create BGE-M3 embedder."""
        from knowledge_service.services.knowledge.multilingual_embedding import (
            BGEM3Embedding,
            create_multilingual_embedding,
        )

        embedder = create_multilingual_embedding(embedding_config)
        assert isinstance(embedder, BGEM3Embedding)

    def test_factory_creates_e5_embedder(self):
        """Factory should create E5 embedder for e5 provider."""
        from knowledge_service.services.knowledge.multilingual_embedding import (
            MultilingualE5Embedding,
            MultilingualEmbeddingConfig,
            create_multilingual_embedding,
        )

        config = MultilingualEmbeddingConfig(
            provider="multilingual-e5",
            model="intfloat/multilingual-e5-large",
        )

        embedder = create_multilingual_embedding(config)
        assert isinstance(embedder, MultilingualE5Embedding)


# =============================================================================
# Test: Multilingual Reranker
# =============================================================================


class TestMultilingualReranker:
    """Test multilingual reranker implementations."""

    def test_rerank_result_structure(self):
        """RerankResult should have correct structure."""
        from knowledge_service.services.knowledge.text_reranker import RerankResult

        result = RerankResult(index=0, relevance_score=0.95)
        assert result.index == 0
        assert result.relevance_score == 0.95

    def test_create_dashscope_reranker(self):
        """Factory should create DashScope reranker."""
        from knowledge_service.services.knowledge.text_reranker import (
            AsyncTextReranker,
            create_reranker,
        )

        reranker = create_reranker(
            provider="dashscope",
            api_key="test_key",
            model="gte-rerank",
        )

        assert isinstance(reranker, AsyncTextReranker)

    def test_create_bge_reranker(self):
        """Factory should create BGE reranker."""
        from knowledge_service.services.knowledge.text_reranker import (
            BGEReranker,
            create_reranker,
        )

        reranker = create_reranker(
            provider="bge",
            model="BAAI/bge-reranker-v2-m3",
        )

        assert isinstance(reranker, BGEReranker)

    def test_create_cohere_reranker(self):
        """Factory should create Cohere reranker."""
        from knowledge_service.services.knowledge.text_reranker import (
            CohereReranker,
            create_reranker,
        )

        reranker = create_reranker(
            provider="cohere",
            api_key="test_key",
            model="rerank-multilingual-v3.0",
        )

        assert isinstance(reranker, CohereReranker)

    def test_create_local_reranker(self):
        """Factory should create local cross-encoder reranker."""
        from knowledge_service.services.knowledge.text_reranker import (
            LocalCrossEncoderReranker,
            create_reranker,
        )

        reranker = create_reranker(
            provider="local",
            model="cross-encoder/ms-marco-MiniLM-L-12-v2",
        )

        assert isinstance(reranker, LocalCrossEncoderReranker)

    def test_factory_requires_api_key_for_cloud(self):
        """Cloud providers should require API key."""
        from knowledge_service.services.knowledge.text_reranker import create_reranker

        with pytest.raises(ValueError, match="API key required"):
            create_reranker(provider="dashscope")

        with pytest.raises(ValueError, match="API key required"):
            create_reranker(provider="cohere")

    @pytest.mark.asyncio
    async def test_dashscope_rerank_empty_docs(self):
        """Empty document list should return empty results."""
        from knowledge_service.services.knowledge.text_reranker import AsyncTextReranker

        reranker = AsyncTextReranker(api_key="test_key")
        results = await reranker.rerank("test query", [])

        assert results == []

    @pytest.mark.asyncio
    async def test_bge_rerank_empty_docs(self):
        """Empty document list should return empty results."""
        from knowledge_service.services.knowledge.text_reranker import BGEReranker

        reranker = BGEReranker()
        results = await reranker.rerank("test query", [])

        assert results == []


# =============================================================================
# Integration Tests (Require External Services)
# =============================================================================


@pytest.mark.integration
class TestMultilingualRAGIntegration:
    """Integration tests for full multilingual RAG pipeline."""

    @pytest.mark.asyncio
    async def test_arabic_document_ingestion(self):
        """Test ingesting Arabic document."""
        # This would require actual service instances
        pytest.skip("Requires running services")

    @pytest.mark.asyncio
    async def test_cross_language_retrieval(self):
        """Test retrieving Arabic content with English query."""
        # This would require actual service instances
        pytest.skip("Requires running services")

    @pytest.mark.asyncio
    async def test_mixed_language_reranking(self):
        """Test reranking mixed Arabic-English results."""
        # This would require actual service instances
        pytest.skip("Requires running services")


# =============================================================================
# Benchmark Tests
# =============================================================================


@pytest.mark.benchmark
class TestMultilingualRAGBenchmarks:
    """Performance benchmarks for multilingual RAG."""

    @pytest.mark.asyncio
    async def test_rrf_fusion_performance(self):
        """RRF fusion should be fast even with many results."""
        import time

        from knowledge_service.services.knowledge.retrieval_v2 import rrf_fusion

        # Create large result sets
        results_a = [{"id": f"doc_a_{i}", "score": 1.0 - i * 0.001} for i in range(1000)]
        results_b = [{"id": f"doc_b_{i}", "score": 1.0 - i * 0.001} for i in range(1000)]
        results_c = [{"id": f"doc_c_{i}", "score": 1.0 - i * 0.001} for i in range(1000)]

        start = time.perf_counter()
        fused = rrf_fusion([results_a, results_b, results_c], k=60)
        elapsed = time.perf_counter() - start

        # Should complete in under 100ms
        assert elapsed < 0.1
        assert len(fused) == 3000  # All unique docs

    def test_sparse_score_performance(self):
        """Sparse score computation should be fast."""
        import time

        from knowledge_service.services.knowledge.multilingual_embedding import compute_sparse_score

        # Create large sparse vectors
        query_sparse = {f"term_{i}": 0.5 for i in range(1000)}
        doc_sparse = {f"term_{i}": 0.3 for i in range(500, 1500)}

        start = time.perf_counter()
        for _ in range(1000):
            compute_sparse_score(query_sparse, doc_sparse)
        elapsed = time.perf_counter() - start

        # 1000 iterations should complete in under 500ms
        assert elapsed < 0.5
