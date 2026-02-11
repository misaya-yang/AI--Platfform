"""
Multilingual Embedding with BGE-M3 Support

BGE-M3 Features:
- Dense retrieval: Standard cosine similarity
- Sparse retrieval: Learned token importance weights (like BM25 but learned)
- Multi-vector (ColBERT): Fine-grained token-level matching

For Arabic-English RAG, this module provides:
- Unified embedding interface for multiple providers
- BGE-M3 integration with dense + sparse outputs
- Cross-modal scoring for hybrid search

Best Practice (2025):
- BGE-M3 achieves state-of-the-art on MIRACL Arabic benchmark
- arabic-english-bge-m3 is optimized specifically for AR-EN retrieval
- Sparse weights from BGE-M3 can replace traditional BM25
"""

from __future__ import annotations

import asyncio
import logging
from abc import ABC, abstractmethod
from dataclasses import dataclass, field
from typing import Any

import httpx

logger = logging.getLogger(__name__)


# =============================================================================
# Data Classes
# =============================================================================


@dataclass
class MultilingualEmbeddingResult:
    """Result from multilingual embedding with multiple retrieval paradigms."""

    # Dense vector (always present)
    dense_vector: list[float]

    # Sparse weights: token -> importance weight (for hybrid search)
    # This replaces BM25 with learned lexical importance
    sparse_weights: dict[str, float] = field(default_factory=dict)

    # ColBERT multi-vectors (optional, for fine-grained matching)
    colbert_vectors: list[list[float]] | None = None

    # Metadata
    model: str = ""
    dimension: int = 0

    def __post_init__(self):
        if self.dense_vector:
            self.dimension = len(self.dense_vector)


@dataclass
class MultilingualEmbeddingConfig:
    """Configuration for multilingual embedding."""

    # Model selection
    provider: str = "bge-m3"  # "bge-m3", "multilingual-e5", "local"
    model: str = "BAAI/bge-m3"  # or "sayed0am/arabic-english-bge-m3"

    # API configuration (for remote inference)
    api_url: str | None = None
    api_key: str | None = None
    timeout_seconds: float = 60.0

    # Feature flags
    return_sparse: bool = True  # Return sparse weights for hybrid search
    return_colbert: bool = False  # Return ColBERT vectors (expensive)
    use_fp16: bool = True  # Use half-precision for speed

    # Dimension (auto-detected if not specified)
    dimension: int = 1024

    # Batch settings
    max_batch_size: int = 32
    max_concurrent: int = 5


# =============================================================================
# Base Embedding Interface
# =============================================================================


class BaseMultilingualEmbedding(ABC):
    """Abstract base class for multilingual embedding providers."""

    def __init__(self, config: MultilingualEmbeddingConfig):
        self.config = config
        self._dimension: int | None = config.dimension

    @property
    def dimension(self) -> int:
        return self._dimension or 1024

    @abstractmethod
    async def embed_texts(
        self,
        texts: list[str],
        text_type: str | None = None,
    ) -> list[MultilingualEmbeddingResult]:
        """Embed texts and return multi-paradigm results."""
        raise NotImplementedError

    async def embed_query(self, query: str) -> MultilingualEmbeddingResult:
        """Embed a single query."""
        results = await self.embed_texts([query], text_type="query")
        return results[0]

    async def embed_documents(self, texts: list[str]) -> list[list[float]]:
        """Compatibility method: return only dense vectors."""
        results = await self.embed_texts(texts, text_type="document")
        return [r.dense_vector for r in results]

    async def close(self) -> None:
        """Clean up resources."""
        pass


# =============================================================================
# BGE-M3 Implementation
# =============================================================================


class BGEM3Embedding(BaseMultilingualEmbedding):
    """
    BGE-M3 Multilingual Embedding Adapter.

    Supports three retrieval paradigms:
    1. Dense: Standard embedding vector (1024D)
    2. Sparse: Learned lexical weights (for hybrid search)
    3. ColBERT: Multi-vector for fine-grained matching

    Models:
    - BAAI/bge-m3: General multilingual (100+ languages)
    - sayed0am/arabic-english-bge-m3: Optimized for Arabic-English

    Usage:
        config = MultilingualEmbeddingConfig(model="BAAI/bge-m3")
        embedder = BGEM3Embedding(config)

        # For indexing: get all representations
        results = await embedder.embed_texts(texts)

        # For query: use dense + sparse for hybrid
        query_result = await embedder.embed_query(query)
    """

    MODEL_DIMENSIONS = {
        "BAAI/bge-m3": 1024,
        "sayed0am/arabic-english-bge-m3": 1024,
    }

    def __init__(self, config: MultilingualEmbeddingConfig):
        super().__init__(config)
        self._model_instance = None
        self._initialized = False
        self._http_client: httpx.AsyncClient | None = None

    async def _ensure_initialized(self):
        """Lazy initialization of model."""
        if self._initialized:
            return

        # If API URL is provided, use remote inference
        if self.config.api_url:
            self._http_client = httpx.AsyncClient(
                timeout=httpx.Timeout(self.config.timeout_seconds)
            )
            self._initialized = True
            return

        # Otherwise, load local model
        try:
            from FlagEmbedding import BGEM3FlagModel

            logger.info(f"Loading BGE-M3 model: {self.config.model}")
            self._model_instance = BGEM3FlagModel(
                self.config.model,
                use_fp16=self.config.use_fp16,
            )
            self._dimension = self.MODEL_DIMENSIONS.get(self.config.model, 1024)
            self._initialized = True
            logger.info(f"BGE-M3 model loaded: dimension={self._dimension}")

        except ImportError:
            raise RuntimeError(
                "FlagEmbedding package required for local BGE-M3: pip install FlagEmbedding"
            )

    async def embed_texts(
        self,
        texts: list[str],
        text_type: str | None = None,
    ) -> list[MultilingualEmbeddingResult]:
        """Embed texts with BGE-M3 multi-paradigm output."""
        if not texts:
            return []

        await self._ensure_initialized()

        # Use API if configured
        if self._http_client and self.config.api_url:
            return await self._embed_via_api(texts)

        # Local model inference
        return await self._embed_local(texts)

    async def _embed_local(self, texts: list[str]) -> list[MultilingualEmbeddingResult]:
        """Embed using local model."""

        def _encode():
            return self._model_instance.encode(
                texts,
                return_dense=True,
                return_sparse=self.config.return_sparse,
                return_colbert_vecs=self.config.return_colbert,
            )

        # Run in thread to avoid blocking
        output = await asyncio.to_thread(_encode)

        results = []
        for i in range(len(texts)):
            # Extract dense vector
            dense_vec = output["dense_vecs"][i]
            if hasattr(dense_vec, "tolist"):
                dense_vec = dense_vec.tolist()

            # Extract sparse weights
            sparse_weights = {}
            if self.config.return_sparse and "lexical_weights" in output:
                sparse_data = output["lexical_weights"][i]
                if isinstance(sparse_data, dict):
                    sparse_weights = sparse_data

            # Extract ColBERT vectors
            colbert_vecs = None
            if self.config.return_colbert and "colbert_vecs" in output:
                cv = output["colbert_vecs"][i]
                if hasattr(cv, "tolist"):
                    colbert_vecs = cv.tolist()
                elif isinstance(cv, list):
                    colbert_vecs = cv

            results.append(
                MultilingualEmbeddingResult(
                    dense_vector=dense_vec,
                    sparse_weights=sparse_weights,
                    colbert_vectors=colbert_vecs,
                    model=self.config.model,
                )
            )

        return results

    async def _embed_via_api(self, texts: list[str]) -> list[MultilingualEmbeddingResult]:
        """Embed using remote API."""
        headers = {}
        if self.config.api_key:
            headers["Authorization"] = f"Bearer {self.config.api_key}"

        resp = await self._http_client.post(
            f"{self.config.api_url}/embed",
            json={
                "texts": texts,
                "return_sparse": self.config.return_sparse,
                "return_colbert": self.config.return_colbert,
            },
            headers=headers,
        )
        resp.raise_for_status()
        data = resp.json()

        results = []
        for item in data.get("embeddings", []):
            results.append(
                MultilingualEmbeddingResult(
                    dense_vector=item.get("dense", []),
                    sparse_weights=item.get("sparse", {}),
                    colbert_vectors=item.get("colbert"),
                    model=self.config.model,
                )
            )

        return results

    async def close(self) -> None:
        """Clean up resources."""
        if self._http_client:
            await self._http_client.aclose()
            self._http_client = None


# =============================================================================
# Multilingual E5 Implementation (Alternative)
# =============================================================================


class MultilingualE5Embedding(BaseMultilingualEmbedding):
    """
    Multilingual E5 Embedding (Microsoft).

    Features:
    - 1024 dimensions
    - 100+ language support
    - Good Arabic performance
    - Instruction-tuned variant available

    Note: E5 only provides dense embeddings (no sparse/ColBERT).
    """

    MODEL_DIMENSIONS = {
        "intfloat/multilingual-e5-large": 1024,
        "intfloat/multilingual-e5-base": 768,
        "intfloat/multilingual-e5-small": 384,
    }

    def __init__(self, config: MultilingualEmbeddingConfig):
        super().__init__(config)
        self._model = None
        self._tokenizer = None

    async def _ensure_initialized(self):
        if self._model is not None:
            return

        try:
            from sentence_transformers import SentenceTransformer

            logger.info(f"Loading E5 model: {self.config.model}")
            self._model = SentenceTransformer(self.config.model)
            self._dimension = self.MODEL_DIMENSIONS.get(
                self.config.model, self._model.get_sentence_embedding_dimension()
            )
            logger.info(f"E5 model loaded: dimension={self._dimension}")

        except ImportError:
            raise RuntimeError(
                "sentence-transformers package required: pip install sentence-transformers"
            )

    async def embed_texts(
        self,
        texts: list[str],
        text_type: str | None = None,
    ) -> list[MultilingualEmbeddingResult]:
        if not texts:
            return []

        await self._ensure_initialized()

        # E5 uses instruction prefixes
        prefix = "query: " if text_type == "query" else "passage: "
        prefixed_texts = [prefix + t for t in texts]

        def _encode():
            return self._model.encode(
                prefixed_texts,
                convert_to_numpy=True,
                normalize_embeddings=True,
            )

        embeddings = await asyncio.to_thread(_encode)

        results = []
        for _i, emb in enumerate(embeddings):
            results.append(
                MultilingualEmbeddingResult(
                    dense_vector=emb.tolist(),
                    sparse_weights={},  # E5 doesn't provide sparse
                    colbert_vectors=None,
                    model=self.config.model,
                )
            )

        return results


# =============================================================================
# Sparse Score Computation
# =============================================================================


def compute_sparse_score(
    query_sparse: dict[str, float],
    doc_sparse: dict[str, float],
) -> float:
    """
    Compute sparse matching score from BGE-M3 lexical weights.

    This replaces traditional BM25 with learned importance weights.
    The score is the dot product of overlapping token weights.

    Args:
        query_sparse: Query token -> weight mapping
        doc_sparse: Document token -> weight mapping

    Returns:
        Sparse matching score (higher = more relevant)
    """
    if not query_sparse or not doc_sparse:
        return 0.0

    score = 0.0
    for token, q_weight in query_sparse.items():
        if token in doc_sparse:
            score += q_weight * doc_sparse[token]

    return score


def compute_hybrid_score(
    query_result: MultilingualEmbeddingResult,
    doc_result: MultilingualEmbeddingResult,
    dense_weight: float = 0.6,
    sparse_weight: float = 0.4,
) -> float:
    """
    Compute hybrid score combining dense and sparse.

    Args:
        query_result: Query embedding result
        doc_result: Document embedding result
        dense_weight: Weight for dense similarity
        sparse_weight: Weight for sparse score

    Returns:
        Combined hybrid score
    """
    import math

    # Dense cosine similarity
    q_vec = query_result.dense_vector
    d_vec = doc_result.dense_vector

    if not q_vec or not d_vec or len(q_vec) != len(d_vec):
        dense_score = 0.0
    else:
        dot = sum(a * b for a, b in zip(q_vec, d_vec, strict=False))
        norm_q = math.sqrt(sum(a * a for a in q_vec))
        norm_d = math.sqrt(sum(a * a for a in d_vec))
        dense_score = dot / (norm_q * norm_d) if norm_q > 0 and norm_d > 0 else 0.0

    # Sparse score
    sparse_score = compute_sparse_score(
        query_result.sparse_weights,
        doc_result.sparse_weights,
    )

    # Normalize sparse score (typically in [0, 10] range)
    # Apply sigmoid-like normalization to [0, 1]
    if sparse_score > 0:
        sparse_score = sparse_score / (sparse_score + 1)

    # Combined score
    return dense_weight * dense_score + sparse_weight * sparse_score


# =============================================================================
# Factory Function
# =============================================================================


def create_multilingual_embedding(
    config: MultilingualEmbeddingConfig | None = None,
    provider: str | None = None,
    model: str | None = None,
    api_url: str | None = None,
) -> BaseMultilingualEmbedding:
    """
    Factory function to create multilingual embedding instance.

    Args:
        config: Full configuration object
        provider: Provider name ("bge-m3", "multilingual-e5")
        model: Model name/path
        api_url: Optional API URL for remote inference

    Returns:
        Multilingual embedding instance

    Example:
        # Use BGE-M3 for Arabic-English
        embedder = create_multilingual_embedding(
            provider="bge-m3",
            model="sayed0am/arabic-english-bge-m3"
        )

        # Use E5 multilingual
        embedder = create_multilingual_embedding(
            provider="multilingual-e5",
            model="intfloat/multilingual-e5-large"
        )
    """
    if config is None:
        config = MultilingualEmbeddingConfig(
            provider=provider or "bge-m3",
            model=model or "BAAI/bge-m3",
            api_url=api_url,
        )

    provider = config.provider.lower()

    if provider in ("bge-m3", "bge", "flagembedding"):
        return BGEM3Embedding(config)
    elif provider in ("multilingual-e5", "e5", "intfloat"):
        return MultilingualE5Embedding(config)
    else:
        # Default to BGE-M3
        logger.warning(f"Unknown provider '{provider}', defaulting to BGE-M3")
        return BGEM3Embedding(config)


# =============================================================================
# Integration Helper for Knowledge Service
# =============================================================================


async def get_multilingual_embedder(
    settings: Any,
    model_override: str | None = None,
) -> BaseMultilingualEmbedding:
    """
    Get or create multilingual embedder from settings.

    This integrates with the existing knowledge service settings.
    Falls back to the default embedding if BGE-M3 is not configured.

    Args:
        settings: Application settings
        model_override: Optional model override

    Returns:
        Multilingual embedding instance
    """
    # Check if multilingual embedding is configured
    knowledge_settings = getattr(settings, "knowledge", None)
    if not knowledge_settings:
        return create_multilingual_embedding()

    # Try to get multilingual-specific settings
    ml_provider = getattr(knowledge_settings, "multilingual_embedding_provider", "bge-m3")
    ml_model = model_override or getattr(
        knowledge_settings, "multilingual_embedding_model", "BAAI/bge-m3"
    )

    config = MultilingualEmbeddingConfig(
        provider=ml_provider,
        model=ml_model,
        return_sparse=True,
        return_colbert=False,
    )

    return create_multilingual_embedding(config)
