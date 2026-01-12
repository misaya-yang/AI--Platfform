"""
KB Tools API Schemas

Optimized request/response schemas for LangGraph agent consumption.
These schemas provide a clean, minimal API surface for knowledge base operations.
"""

from __future__ import annotations

from typing import Any, Dict, List, Literal, Optional

from pydantic import BaseModel, ConfigDict, Field


# ============================================================
# Request Schemas
# ============================================================

class KBSearchRequest(BaseModel):
    """
    Universal KB search request for LangGraph agents.

    Designed for simplicity - most parameters have sensible defaults.

    Example:
        {
            "query": "What is our refund policy?",
            "dataset_id": "company-docs",
            "top_k": 5
        }
    """
    model_config = ConfigDict(extra="forbid")

    # Required
    query: str = Field(..., description="Natural language search query")
    dataset_id: str = Field(..., description="ID of the dataset to search")

    # Optional with sensible defaults
    top_k: int = Field(default=5, ge=1, le=50, description="Number of results to return")
    mode: Literal["auto", "hybrid", "dense", "bm25"] = Field(
        default="auto",
        description="Retrieval mode. 'auto' selects best mode based on query characteristics"
    )

    # Advanced options (usually not needed)
    rerank: bool = Field(default=False, description="Enable reranking for improved relevance")
    mmr: bool = Field(default=False, description="Enable MMR for result diversity")
    score_threshold: Optional[float] = Field(
        default=None,
        ge=0.0,
        le=1.0,
        description="Minimum score threshold (0.0-1.0). Results below this are filtered out"
    )

    # Multimodal options
    include_images: bool = Field(default=True, description="Include image segments in results")
    include_associated_images: bool = Field(
        default=True,
        description="Attach associated images to text segments"
    )

    # Filter options
    document_id: Optional[str] = Field(default=None, description="Filter to specific document")
    content_type_filter: Optional[Literal["text", "image"]] = Field(
        default=None,
        description="Filter by content type"
    )

    # Cross-modal search (Phase 2)
    query_image_url: Optional[str] = Field(
        default=None,
        description="URL of image to use as query (for image-to-text search). "
                    "When provided, searches for text/images similar to this image."
    )
    cross_modal: bool = Field(
        default=True,
        description="Enable cross-modal retrieval (text queries can find images and vice versa). "
                    "Requires unified multimodal embedding model."
    )


class KBMultiSearchRequest(BaseModel):
    """
    Search across multiple datasets.

    Example:
        {
            "query": "How do I reset my password?",
            "dataset_ids": ["docs", "wiki", "faq"],
            "top_k": 5
        }
    """
    model_config = ConfigDict(extra="forbid")

    query: str = Field(..., description="Natural language search query")
    dataset_ids: List[str] = Field(
        ...,
        min_length=1,
        max_length=10,
        description="List of dataset IDs to search (max 10)"
    )

    top_k: int = Field(default=5, ge=1, le=50, description="Total results to return after merging")
    mode: Literal["auto", "hybrid", "dense", "bm25"] = Field(default="auto")
    rerank: bool = Field(default=False)
    mmr: bool = Field(default=False)
    score_threshold: Optional[float] = Field(default=None, ge=0.0, le=1.0)

    # Merge strategy
    merge_strategy: Literal["score", "round_robin", "rrf"] = Field(
        default="score",
        description="How to merge results from multiple datasets"
    )


class KBImageSearchRequest(BaseModel):
    """
    Search by image - find text/images similar to a query image.

    This enables cross-modal retrieval where an image query can find:
    - Text segments describing similar content
    - Other images with similar visual content

    Example:
        {
            "image_url": "https://storage.example.com/diagram.png",
            "dataset_id": "docs",
            "top_k": 5
        }
    """
    model_config = ConfigDict(extra="forbid")

    # Query image (one of these required)
    image_url: Optional[str] = Field(
        default=None,
        description="URL of image to search for. Must be accessible by the server."
    )
    image_base64: Optional[str] = Field(
        default=None,
        description="Base64-encoded image data (alternative to image_url)"
    )

    # Dataset
    dataset_id: str = Field(..., description="Dataset to search")

    # Options
    top_k: int = Field(default=5, ge=1, le=50, description="Number of results")
    include_text_results: bool = Field(
        default=True,
        description="Include text segments in results (cross-modal)"
    )
    include_image_results: bool = Field(
        default=True,
        description="Include image segments in results (visual similarity)"
    )
    score_threshold: Optional[float] = Field(default=None, ge=0.0, le=1.0)

    # Optional text context for hybrid image+text query
    text_context: Optional[str] = Field(
        default=None,
        description="Optional text to combine with image for hybrid query"
    )


class KBListDatasetsRequest(BaseModel):
    """Request to list available datasets."""
    model_config = ConfigDict(extra="forbid")

    include_stats: bool = Field(default=False, description="Include document/segment counts")


# ============================================================
# Response Schemas
# ============================================================

class KBAssociatedImage(BaseModel):
    """Associated image attached to a text segment."""
    image_segment_id: str
    storage_url: str
    filename: str = ""
    vlm_description: Optional[str] = None
    proximity_score: float = Field(default=1.0, description="Relevance to parent text (0-1)")
    media_type: str = "image/png"


class KBSearchResult(BaseModel):
    """Single search result optimized for agent consumption."""
    model_config = ConfigDict(extra="ignore")

    # Core fields
    content: str = Field(..., description="The retrieved text content")
    score: float = Field(..., description="Relevance score (0-1)")

    # Identifiers
    segment_id: str
    document_id: str
    dataset_id: str

    # Content type
    content_type: Literal["text", "image"] = Field(default="text")

    # Metadata
    metadata: Dict[str, Any] = Field(default_factory=dict)

    # Image-specific fields (for image segments)
    image_url: Optional[str] = Field(default=None, description="Storage URL for image segments")
    vlm_description: Optional[str] = Field(
        default=None,
        description="VLM-generated description for images"
    )

    # Associated images (for text segments with nearby images)
    associated_images: List[KBAssociatedImage] = Field(default_factory=list)


class KBSearchResponse(BaseModel):
    """
    Agent-friendly search response.

    Includes both structured results and a pre-formatted context string
    ready for LLM consumption.
    """
    model_config = ConfigDict(extra="ignore")

    # Structured results for programmatic access
    results: List[KBSearchResult] = Field(default_factory=list)

    # Pre-formatted context string for direct LLM injection
    formatted_context: str = Field(
        default="",
        description="Pre-formatted text ready for LLM context window"
    )

    # Metadata
    query: str
    dataset_id: str
    total_results: int = 0

    # Timing and debug info (optional)
    metadata: Dict[str, Any] = Field(default_factory=dict)


class KBMultiSearchResponse(BaseModel):
    """Response for multi-dataset search."""
    model_config = ConfigDict(extra="ignore")

    results: List[KBSearchResult] = Field(default_factory=list)
    formatted_context: str = ""

    query: str
    dataset_ids: List[str]
    total_results: int = 0

    # Per-dataset result counts
    results_per_dataset: Dict[str, int] = Field(default_factory=dict)

    metadata: Dict[str, Any] = Field(default_factory=dict)


class KBDatasetInfo(BaseModel):
    """Dataset information for listing."""
    dataset_id: str
    name: str
    description: str = ""

    # Stats (if requested)
    document_count: Optional[int] = None
    segment_count: Optional[int] = None

    # Config summary
    embedding_provider: str = ""
    embedding_model: str = ""


class KBListDatasetsResponse(BaseModel):
    """Response for listing available datasets."""
    datasets: List[KBDatasetInfo] = Field(default_factory=list)
    total: int = 0


# ============================================================
# Tool Definition Schemas (for LangChain/OpenAI function calling)
# ============================================================

class KBToolDefinition(BaseModel):
    """
    Tool definition in OpenAI function calling format.

    Can be used to expose KB search as a tool to LLMs.
    """
    type: Literal["function"] = "function"
    function: Dict[str, Any]


def get_kb_search_tool_definition(
    dataset_id: str,
    dataset_name: Optional[str] = None,
) -> KBToolDefinition:
    """
    Generate OpenAI function calling definition for KB search.

    Usage:
        tool_def = get_kb_search_tool_definition("company-docs", "Company Documentation")
        tools = [tool_def.model_dump()]
    """
    name = dataset_name or dataset_id

    return KBToolDefinition(
        function={
            "name": f"search_{dataset_id.replace('-', '_')}",
            "description": f"Search the '{name}' knowledge base for relevant information. Use this tool when you need to find specific facts, documentation, or context from {name}.",
            "parameters": {
                "type": "object",
                "properties": {
                    "query": {
                        "type": "string",
                        "description": "Natural language search query"
                    },
                    "top_k": {
                        "type": "integer",
                        "description": "Number of results to return (default: 5)",
                        "default": 5
                    }
                },
                "required": ["query"]
            }
        }
    )


def get_multi_kb_search_tool_definition(
    dataset_ids: List[str],
    dataset_names: Optional[Dict[str, str]] = None,
) -> KBToolDefinition:
    """
    Generate tool definition for searching multiple KBs.
    """
    names = dataset_names or {}
    dataset_desc = ", ".join(
        f"'{names.get(d, d)}' ({d})" for d in dataset_ids
    )

    return KBToolDefinition(
        function={
            "name": "search_knowledge_bases",
            "description": f"Search across multiple knowledge bases for relevant information. Available datasets: {dataset_desc}",
            "parameters": {
                "type": "object",
                "properties": {
                    "query": {
                        "type": "string",
                        "description": "Natural language search query"
                    },
                    "dataset_id": {
                        "type": "string",
                        "description": f"Optional: specific dataset to search. If not provided, searches all datasets.",
                        "enum": dataset_ids
                    },
                    "top_k": {
                        "type": "integer",
                        "description": "Number of results to return (default: 5)",
                        "default": 5
                    }
                },
                "required": ["query"]
            }
        }
    )
