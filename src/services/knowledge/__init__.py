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

Quick Start for LangGraph Agents:

    from agent_gateway.services.knowledge import create_kb_tool

    # Create a KB tool for your agent
    kb_tool = create_kb_tool(kb_service, "my_dataset", user_context)

    # Use with LangGraph ToolNode
    from langgraph.prebuilt import ToolNode
    tool_node = ToolNode([kb_tool])
"""

from .chunking import (
    AutomaticChunker,
    BaseChunker,
    Chunk,
    ChunkingConfig,
    ChunkingMode,
    FixedSizeChunker,
    HeadingChunker,
    HierarchicalChunker,
    PageChunker,
    ParagraphChunker,
    RecursiveChunker,
    RegexChunker,
    SeparatorChunker,
    TextPreprocessor,
    chunk_text,
    create_chunker,
    flatten_chunks,
    process_document,
)

# LangGraph Tools - Primary API for agent integration
from .langgraph_tools import (
    # Dify compatibility
    DifyCompatibleKBAPI,
    KBSearchResult,
    # Configuration
    KBToolConfig,
    # Tool classes
    KnowledgeBaseTool,
    KnowledgeRetriever,
    MultiDatasetRetriever,
    MultiKnowledgeBaseTool,
    # Factory functions (recommended)
    create_kb_tool,
    create_langchain_kb_tool,
    create_multi_kb_tool,
    create_retrieval_tool,
)
from .retrieval_config import (
    DEFAULT_CONFIGS,
    DatasetIndexConfig,
    FusionConfig,
    FusionStrategy,
    KeywordRetrievalConfig,
    MMRConfig,
    RerankConfig,
    RerankProvider,
    RetrievalConfig,
    RetrievalMode,
    VectorRetrievalConfig,
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
    # LangGraph Tools
    "create_kb_tool",
    "create_multi_kb_tool",
    "create_langchain_kb_tool",
    "create_retrieval_tool",
    "KnowledgeBaseTool",
    "MultiKnowledgeBaseTool",
    "KnowledgeRetriever",
    "MultiDatasetRetriever",
    "KBToolConfig",
    "KBSearchResult",
    "DifyCompatibleKBAPI",
]
