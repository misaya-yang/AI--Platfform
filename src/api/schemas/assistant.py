"""
API schemas for Assistant service.

Phase 1: Unified session + message + streaming protocol.
- Server as single source of truth for session/history
- Unified SSE event schema
- Enhanced message with tool_calls/tool_results/citations/attachments
"""

from typing import Any, Dict, List, Optional, Union
from pydantic import BaseModel, Field

from ...services.assistant.model_registry import ModelProvider


# =============================================================================
# Tool Call Structures (for Agentic workflows)
# =============================================================================

class ToolCallArgument(BaseModel):
    """A single argument in a tool call."""
    name: str = Field(..., description="Argument name")
    value: Any = Field(..., description="Argument value")


class ToolCall(BaseModel):
    """A tool call made by the assistant."""
    id: str = Field(..., description="Unique tool call ID")
    name: str = Field(..., description="Tool name")
    arguments: Dict[str, Any] = Field(default_factory=dict, description="Tool arguments as key-value pairs")
    status: str = Field(default="pending", description="Status: pending, running, completed, error")


class ToolResult(BaseModel):
    """Result from a tool execution."""
    tool_call_id: str = Field(..., description="ID of the tool call this result belongs to")
    name: str = Field(..., description="Tool name")
    result: Any = Field(default=None, description="Tool execution result")
    error: Optional[str] = Field(default=None, description="Error message if execution failed")
    duration_ms: Optional[float] = Field(default=None, description="Execution time in milliseconds")


# =============================================================================
# Citation Structures (for RAG traceability)
# =============================================================================

class Citation(BaseModel):
    """A citation linking content to source."""
    dataset_id: str = Field(..., description="Source dataset ID")
    document_id: Optional[str] = Field(default=None, description="Source document ID")
    chunk_id: Optional[str] = Field(default=None, description="Source chunk ID")
    source_url: Optional[str] = Field(default=None, description="URL to source if available")
    title: Optional[str] = Field(default=None, description="Source title")
    score: Optional[float] = Field(default=None, description="Relevance score")
    content_preview: Optional[str] = Field(default=None, description="Preview of cited content")


# =============================================================================
# Attachment Structures (for multimodal input)
# =============================================================================

class Attachment(BaseModel):
    """An attachment in a message."""
    id: str = Field(..., description="Attachment ID")
    type: str = Field(..., description="Attachment type: image, file, audio")
    filename: Optional[str] = Field(default=None, description="Original filename")
    url: Optional[str] = Field(default=None, description="URL to access the attachment")
    mime_type: Optional[str] = Field(default=None, description="MIME type")
    size_bytes: Optional[int] = Field(default=None, description="File size in bytes")


# =============================================================================
# Enhanced Message Structure
# =============================================================================

class AssistantMessage(BaseModel):
    """
    A message in the conversation with full agentic support.

    Supports:
    - Tool calls and results
    - Citations from RAG
    - File/image attachments
    - Metadata for tracing
    """
    role: str = Field(..., description="Message role: user, assistant, system, or tool")
    content: str = Field(default="", description="Message text content")

    # Agentic extensions
    tool_calls: Optional[List[ToolCall]] = Field(default=None, description="Tool calls made in this message")
    tool_results: Optional[List[ToolResult]] = Field(default=None, description="Tool results for tool messages")

    # RAG traceability
    citations: Optional[List[Citation]] = Field(default=None, description="Citations linking to sources")

    # Multimodal support
    attachments: Optional[List[Attachment]] = Field(default=None, description="File/image attachments")

    # Metadata
    message_id: Optional[str] = Field(default=None, description="Unique message ID")
    timestamp: Optional[str] = Field(default=None, description="ISO timestamp")
    metadata: Optional[Dict[str, Any]] = Field(default=None, description="Additional metadata")


class AssistantChatRequest(BaseModel):
    """Request body for chat endpoint."""
    message: str = Field(..., description="User's message", min_length=1)
    session_id: Optional[str] = Field(default=None, description="Session ID for conversation tracking")
    history: Optional[List[AssistantMessage]] = Field(default=None, description="Previous conversation history. If None and session_id provided, auto-loads from session.")

    # Model settings
    model_id: str = Field(default="gemini-3-flash-preview", description="Model ID to use")
    temperature: float = Field(default=0.7, ge=0.0, le=2.0, description="Sampling temperature")
    max_tokens: Optional[int] = Field(default=None, description="Maximum tokens to generate")

    # Knowledge base settings
    kb_dataset_ids: List[str] = Field(default_factory=list, description="Dataset IDs to search for context")
    kb_mode: str = Field(default="auto", description="RAG mode: auto, tool, or off")
    kb_top_k: int = Field(default=5, ge=1, le=20, description="Number of KB results to retrieve")
    kb_score_threshold: float = Field(default=0.0, ge=0.0, le=1.0, description="Minimum relevance score (0.0 = no filtering)")
    kb_include_images: bool = Field(default=False, description="Include image results from KB")

    # Web search settings
    web_search_enabled: bool = Field(default=False, description="Enable web search via Tavily")
    web_search_max_results: int = Field(default=5, ge=1, le=10, description="Maximum web search results")

    # File attachments
    file_paths: List[str] = Field(default_factory=list, description="Paths to uploaded files for multimodal input")

    # System prompt
    system_prompt: Optional[str] = Field(default=None, description="Custom system prompt")

    # Task planning settings
    enable_task_planning: bool = Field(default=False, description="Enable task planning and tool orchestration")
    confirm_plan: bool = Field(default=False, description="Confirm execution plan before running tools")


class AssistantChatResponse(BaseModel):
    """Response body for non-streaming chat."""
    content: str = Field(..., description="Assistant's response")
    usage: Dict[str, int] = Field(default_factory=dict, description="Token usage statistics")
    contexts: List[Dict[str, Any]] = Field(default_factory=list, description="Retrieved KB contexts")
    duration_ms: float = Field(..., description="Total processing time in milliseconds")
    model_id: str = Field(..., description="Model ID used")
    session_id: Optional[str] = Field(default=None, description="Session ID")


class ModelInfoResponse(BaseModel):
    """Model information."""
    id: str = Field(..., description="Model ID")
    name: str = Field(..., description="Display name")
    provider: str = Field(..., description="Provider name")
    context_window: int = Field(..., description="Context window size in tokens")
    max_output_tokens: int = Field(..., description="Maximum output tokens")
    supports_vision: bool = Field(..., description="Whether model supports vision")
    supports_tools: bool = Field(..., description="Whether model supports tool calling")
    access_level: str = Field(default="public", description="Access level: public, premium, admin")
    input_price_per_1k: float = Field(default=0.0, description="Price per 1K input tokens (USD)")
    output_price_per_1k: float = Field(default=0.0, description="Price per 1K output tokens (USD)")


class ModelsListResponse(BaseModel):
    """Response for listing available models."""
    models: List[ModelInfoResponse] = Field(..., description="List of available models")


class DatasetInfoResponse(BaseModel):
    """Dataset information for KB selection."""
    dataset_id: str = Field(..., description="Dataset ID")
    name: str = Field(..., description="Dataset name")
    description: Optional[str] = Field(default=None, description="Dataset description")
    document_count: int = Field(default=0, description="Number of documents")
    chunk_count: int = Field(default=0, description="Number of chunks")
    embedding_model: Optional[str] = Field(default=None, description="Embedding model used")
    is_multimodal: bool = Field(default=False, description="Whether KB supports multimodal (image) retrieval")


class DatasetsListResponse(BaseModel):
    """Response for listing available datasets."""
    datasets: List[DatasetInfoResponse] = Field(..., description="List of available datasets")


# =============================================================================
# Unified SSE Event Schema (Phase 1)
# =============================================================================

class SSEEventType:
    """Standard SSE event types for assistant streaming."""
    # Content streaming
    TEXT_DELTA = "text_delta"           # Incremental text content
    THINKING_DELTA = "thinking_delta"   # Model reasoning (for CoT display)

    # Tool execution
    TOOL_CALL = "tool_call"             # Tool invocation started
    TOOL_RESULT = "tool_result"         # Tool execution completed

    # RAG context
    CONTEXT_RETRIEVED = "context_retrieved"  # KB search results
    WEB_SEARCH_RESULTS = "web_search_results"  # Web search results

    # Phase 3: RAG evaluation
    RAG_EVALUATION = "rag_evaluation"   # RAG quality metrics and citations

    # KV-Cache metrics
    CACHE_METRICS = "cache_metrics"       # Cache performance metrics

    # Session/state
    SESSION_CREATED = "session_created"  # New session created
    SESSION_UPDATED = "session_updated"  # Session metadata updated

    # Completion
    USAGE = "usage"                     # Token usage statistics
    FINISH = "finish"                   # Generation finished (may continue with tools)
    DONE = "done"                       # Complete response done
    ERROR = "error"                     # Error occurred


class ContextRetrievedEvent(BaseModel):
    """Data for context_retrieved event."""
    dataset_id: str
    dataset_name: str
    chunks: List[Dict[str, Any]]
    query: str
    took_ms: float


class WebSearchResultsEvent(BaseModel):
    """Data for web_search_results event."""
    query: str
    results: List[Dict[str, Any]]
    answer: Optional[str] = None
    took_ms: float


class ToolCallEvent(BaseModel):
    """Data for tool_call event."""
    id: str
    name: str
    arguments: Dict[str, Any]
    status: str = "pending"


class ToolResultEvent(BaseModel):
    """Data for tool_result event."""
    tool_call_id: str
    name: str
    result: Any
    error: Optional[str] = None
    duration_ms: Optional[float] = None


class UsageEvent(BaseModel):
    """Data for usage event."""
    input_tokens: Optional[int] = None
    output_tokens: Optional[int] = None
    total_tokens: Optional[int] = None
    cached_tokens: Optional[int] = None


class DoneEvent(BaseModel):
    """Data for done event."""
    session_id: str
    total_length: int
    duration_ms: float
    model_id: str
    kb_datasets_used: List[str] = Field(default_factory=list)
    context_truncated: bool = False
    # Phase 3: RAG quality summary
    rag_quality: Optional[float] = Field(default=None, description="RAG quality score 0-100")
    citations_count: int = Field(default=0, description="Number of citations generated")


# =============================================================================
# Phase 3: RAG Evaluation Event
# =============================================================================

class RAGCitation(BaseModel):
    """A citation linking response to source (Phase 3)."""
    citation_id: str = Field(..., description="Unique citation ID")
    chunk_id: str = Field(..., description="Source chunk ID")
    dataset_id: str = Field(..., description="Source dataset ID")
    dataset_name: str = Field(..., description="Dataset display name")
    source_url: Optional[str] = Field(default=None, description="URL to source")
    source_title: Optional[str] = Field(default=None, description="Source title")
    cited_text: str = Field(default="", description="Text that was cited")
    context_preview: str = Field(default="", description="Preview of source context")
    relevance_score: float = Field(default=0.0, description="Relevance score")
    status: str = Field(default="implicit", description="Citation status: used, implicit, unused")


class RAGQualityBreakdown(BaseModel):
    """Breakdown of RAG quality score components."""
    relevance: float = Field(default=0.0, description="Relevance score (0-25)")
    coverage: float = Field(default=0.0, description="Coverage score (0-25)")
    usage: float = Field(default=0.0, description="Usage score (0-25)")
    citations: float = Field(default=0.0, description="Citation score (0-25)")


class RAGEvaluationEvent(BaseModel):
    """Data for rag_evaluation event (Phase 3)."""
    quality_score: float = Field(..., description="Overall quality score 0-100")
    quality_breakdown: RAGQualityBreakdown = Field(..., description="Score breakdown by component")
    chunks_retrieved: int = Field(..., description="Total chunks retrieved")
    chunks_used: int = Field(..., description="Chunks used in response")
    response_grounding: float = Field(..., description="What % of response is grounded (0-1)")
    citations: List[RAGCitation] = Field(default_factory=list, description="Citations extracted")
    evaluation_time_ms: float = Field(..., description="Time taken for evaluation")


# =============================================================================
# KV-Cache Metrics Event
# =============================================================================

class CacheMetricsEvent(BaseModel):
    """Cache performance metrics for KV-cache optimization monitoring."""
    layer1_hit: bool = Field(default=False, description="Whether Layer 1 (system prefix) cache was hit")
    layer2_hit: bool = Field(default=False, description="Whether Layer 2 (session context) cache was hit")
    total_input_tokens: int = Field(default=0, ge=0, description="Total input tokens")
    cached_tokens: int = Field(default=0, ge=0, description="Number of tokens served from cache")
    cache_hit_rate: float = Field(default=0.0, ge=0.0, le=1.0, description="Cache hit rate (0-1)")
    estimated_savings_usd: float = Field(default=0.0, ge=0.0, description="Estimated cost savings in USD")
    system_prefix_hash: str = Field(default="", description="Hash of Layer 1 cache key for debugging")


class ErrorEvent(BaseModel):
    """Data for error event."""
    message: str
    code: Optional[str] = None
    recoverable: bool = True


class StreamEventData(BaseModel):
    """
    SSE event data structure.

    Event types:
    - text_delta: Incremental text content (data: str)
    - thinking_delta: Model reasoning display (data: str)
    - tool_call: Tool invocation (data: ToolCallEvent)
    - tool_result: Tool execution result (data: ToolResultEvent)
    - context_retrieved: KB search results (data: ContextRetrievedEvent)
    - web_search_results: Web search results (data: WebSearchResultsEvent)
    - rag_evaluation: RAG quality metrics (data: RAGEvaluationEvent)
    - cache_metrics: KV-cache performance metrics (data: CacheMetricsEvent)
    - usage: Token usage (data: UsageEvent)
    - finish: Generation finished (data: {"reason": str})
    - done: Complete response (data: DoneEvent)
    - error: Error occurred (data: ErrorEvent)
    """
    event_type: str = Field(..., description="Event type from SSEEventType")
    data: Any = Field(default=None, description="Event payload (type depends on event_type)")
    timestamp: float = Field(..., description="Event timestamp (Unix epoch)")


class AssistantConfigResponse(BaseModel):
    """Response for getting assistant configuration."""
    default_model_id: str = Field(..., description="Default model ID")
    available_providers: List[str] = Field(..., description="List of configured providers")
    kb_enabled: bool = Field(..., description="Whether KB integration is enabled")
    web_search_enabled: bool = Field(default=False, description="Whether web search is available")
    tools_available: List[str] = Field(default_factory=list, description="List of available tools")


# =============================================================================
# Session History with Enhanced Messages
# =============================================================================

class EnhancedSessionMessage(BaseModel):
    """A message in session history with full context."""
    message_id: str
    role: str
    content: str
    timestamp: str

    # Agentic extensions
    tool_calls: Optional[List[ToolCall]] = None
    tool_results: Optional[List[ToolResult]] = None

    # RAG context
    citations: Optional[List[Citation]] = None
    retrieved_contexts: Optional[List[ContextRetrievedEvent]] = None

    # Attachments
    attachments: Optional[List[Attachment]] = None

    # Metadata
    model_id: Optional[str] = None
    usage: Optional[UsageEvent] = None
    metadata: Optional[Dict[str, Any]] = None


# =============================================================================
# Image Generation (Smart Routing)
# =============================================================================

class ImageGenerationRequest(BaseModel):
    """Request for image generation with smart routing."""
    prompt: str = Field(..., description="Text description of the image to generate", min_length=1)
    model_id: str = Field(..., description="Current model ID to determine provider routing")
    style: Optional[str] = Field(default="default", description="Image style (DashScope only)")
    size: Optional[str] = Field(default="1024*1024", description="Image size")
    n: int = Field(default=1, ge=1, le=4, description="Number of images to generate")


class GeneratedImage(BaseModel):
    """A generated image result."""
    url: str = Field(..., description="Image URL (data:image/png;base64,... or http)")
    width: Optional[int] = Field(default=None, description="Image width")
    height: Optional[int] = Field(default=None, description="Image height")


class ImageGenerationResponse(BaseModel):
    """Response for image generation."""
    success: bool = Field(..., description="Whether generation succeeded")
    images: List[GeneratedImage] = Field(default_factory=list, description="Generated images")
    provider: str = Field(..., description="Provider used for generation (dashscope/google)")
    duration_ms: float = Field(..., description="Generation time in milliseconds")
    error: Optional[str] = Field(default=None, description="Error message if failed")
