"""
Knowledge Base (KBMS) service package.

Provides a production-grade RAG system with:
- Configurable document chunking strategies (9 modes)
- Hybrid retrieval (vector + keyword)
- Fusion strategies (RRF, weighted)
- Reranking (DashScope, Cohere, etc.)
- MMR diversity
- LLM-based QA testing
- LangGraph-compatible tools
"""

from .chunking import (
    ChunkingConfig,
    ChunkingMode,
    Chunk,
    TextPreprocessor,
    BaseChunker,
    FixedSizeChunker,
    ParagraphChunker,
    PageChunker,
    HeadingChunker,
    RegexChunker,
    SeparatorChunker,
    RecursiveChunker,
    HierarchicalChunker,
    AutomaticChunker,
    create_chunker,
    process_document,
    flatten_chunks,
    chunk_text,
)

from .retrieval_config import (
    RetrievalConfig,
    RetrievalMode,
    FusionStrategy,
    FusionConfig,
    VectorRetrievalConfig,
    KeywordRetrievalConfig,
    RerankConfig,
    RerankProvider,
    MMRConfig,
    DatasetIndexConfig,
    DEFAULT_CONFIGS,
    get_preset_config,
)

__all__ = [
    # Chunking
    "ChunkingConfig",
    "ChunkingMode", 
    "Chunk",
    "TextPreprocessor",
    "BaseChunker",
    "FixedSizeChunker",
    "ParagraphChunker",
    "PageChunker",
    "HeadingChunker",
    "RegexChunker",
    "SeparatorChunker",
    "RecursiveChunker",
    "HierarchicalChunker",
    "AutomaticChunker",
    "create_chunker",
    "process_document",
    "flatten_chunks",
    "chunk_text",
    # Retrieval Config
    "RetrievalConfig",
    "RetrievalMode",
    "FusionStrategy",
    "FusionConfig",
    "VectorRetrievalConfig",
    "KeywordRetrievalConfig",
    "RerankConfig",
    "RerankProvider",
    "MMRConfig",
    "DatasetIndexConfig",
    "DEFAULT_CONFIGS",
    "get_preset_config",
]
