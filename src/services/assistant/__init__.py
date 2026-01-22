"""
Assistant service module.

Provides a GPT-like assistant experience with:
- Multi-model support (OpenAI, Claude, DeepSeek, Qwen)
- Knowledge base integration
- Streaming responses
- RAG evaluation and citation tracking (Phase 3)
- Structured output and validation (Phase 4)
- Write-while-search capability (Phase 2.3)
"""

from .assistant_service import AssistantService, AssistantConfig, RAGEvaluation
from .model_registry import ModelRegistry, ModelProvider, ModelInfo
from .streaming_writer import (
    StreamChunk,
    StreamingWriter,
    create_streaming_writer,
    DEFAULT_VERIFICATION_TRIGGERS,
)
from .rag_metrics import (
    RAGEvaluator,
    RAGMetrics,
    Citation,
    CitationStatus,
    ContextChunkMetrics,
    get_rag_evaluator,
    evaluate_rag,
    extract_citations,
)
from .structured_output import (
    OutputFormat,
    OutputGuardrail,
    StructuredOutputParser,
    StructuredOutputResult,
    AnswerWithCitations,
    StepByStepAnswer,
    FactCheckResult,
    ExtractedEntities,
    ClassificationResult,
    parse_structured_output,
    validate_output,
    create_json_prompt,
)
from .cache_optimizer import (
    CacheConfig,
    CacheMetrics,
    CacheBreakpoint,
    ContextCacheOptimizer,
)
from .document_parser import DocumentParser, DocumentParseError, parse_document
from .file_processor import (
    FileProcessor,
    ProcessedFiles,
    ImageContent,
    FileProcessError,
    create_file_processor,
)
from .guardrails import (
    DocumentType,
    IssueSeverity,
    QualityIssue,
    ValidationResult,
    QualityGuardrails,
    ToolCallValidation,
    ToolConstraintValidator,
    QUALITY_THRESHOLDS,
    BANNED_PHRASES,
    TOOL_CONSTRAINTS,
)
from .content_generator import (
    GenerationPhase,
    ContentSection,
    ContentOutline,
    GeneratedContent,
    StreamEvent,
    DeepContentGenerator,
    create_content_generator,
)
from .task_planner import IntentType, TaskStrategy
from .agui_protocol import (
    AGUIEventEmitter,
    BaseEvent,
    RunLifecycleEvent,
    StepEvent,
    TextMessageEvent,
    ToolCallEvent,
    StateEvent,
    ArtifactEvent,
    StatusEvent,
    create_agui_emitter,
)
# Enterprise Agent Loop Components
from .tool_invoker import (
    ToolInvoker,
    ToolInvocationContext,
    RegistryToolInvoker,
    create_tool_invoker,
)
from .task_manager import (
    TaskManager,
    SessionResources,
    TaskContext,
    get_task_manager,
    init_task_manager,
    shutdown_task_manager,
)
from .agent_loop import (
    AgentLoop,
    AgentLoopConfig,
    AgentLoopContext,
    AgentLoopEvent,
    AgentLoopPhase,
    ErrorSeverity,
    StructuredError,
    PHASE_DISPLAY_NAMES,
    PHASE_INDEX,
    TOTAL_PHASES,
    create_agent_loop,
)
from .rag_metrics import (
    RetrievalMetrics,
    RAGMetricsCollector,
    get_rag_metrics_collector,
)
from .context_metrics import (
    ContextMetrics,
    ContextMetricsBuilder,
    ContextMetricsCollector,
    MetricLayer,
    LayerMetrics,
    CompressionMetrics,
    CacheMetrics as KVCacheMetrics,  # Renamed to avoid conflict with cache_optimizer.CacheMetrics
    MemoryMetrics,
    get_context_metrics_collector,
    init_context_metrics_collector,
)
# Query Intent Analyzer (Self-RAG style adaptive retrieval)
from .query_intent_analyzer import (
    QueryType,
    RetrievalDecision,
    QueryIntent,
    QueryIntentAnalyzer,
    create_query_intent_analyzer,
)

__all__ = [
    "AssistantService",
    "AssistantConfig",
    "RAGEvaluation",
    "ModelRegistry",
    "ModelProvider",
    "ModelInfo",
    # Phase 3: RAG Metrics
    "RAGEvaluator",
    "RAGMetrics",
    "Citation",
    "CitationStatus",
    "ContextChunkMetrics",
    "get_rag_evaluator",
    "evaluate_rag",
    "extract_citations",
    # Phase 4: Structured Output
    "OutputFormat",
    "OutputGuardrail",
    "StructuredOutputParser",
    "StructuredOutputResult",
    "AnswerWithCitations",
    "StepByStepAnswer",
    "FactCheckResult",
    "ExtractedEntities",
    "ClassificationResult",
    "parse_structured_output",
    "validate_output",
    "create_json_prompt",
    # Cache Optimization
    "CacheConfig",
    "CacheMetrics",
    "CacheBreakpoint",
    "ContextCacheOptimizer",
    # Document Parser
    "DocumentParser",
    "DocumentParseError",
    "parse_document",
    # File Processor
    "FileProcessor",
    "ProcessedFiles",
    "ImageContent",
    "FileProcessError",
    "create_file_processor",
    # StreamingWriter (Phase 2.3: Write-while-search)
    "StreamChunk",
    "StreamingWriter",
    "create_streaming_writer",
    "DEFAULT_VERIFICATION_TRIGGERS",
    # Guardrails (Quality Control)
    "DocumentType",
    "IssueSeverity",
    "QualityIssue",
    "ValidationResult",
    "QualityGuardrails",
    "ToolCallValidation",
    "ToolConstraintValidator",
    "QUALITY_THRESHOLDS",
    "BANNED_PHRASES",
    "TOOL_CONSTRAINTS",
    # Deep Content Generator (Phase 2)
    "GenerationPhase",
    "ContentSection",
    "ContentOutline",
    "GeneratedContent",
    "StreamEvent",
    "DeepContentGenerator",
    "create_content_generator",
    # Intent Analysis (Phase 2)
    "IntentType",
    "TaskStrategy",
    # AG-UI Protocol (Phase 3)
    "AGUIEventEmitter",
    "BaseEvent",
    "RunLifecycleEvent",
    "StepEvent",
    "TextMessageEvent",
    "ToolCallEvent",
    "StateEvent",
    "ArtifactEvent",
    "StatusEvent",
    "create_agui_emitter",
    # Enterprise Agent Loop Components
    "ToolInvoker",
    "ToolInvocationContext",
    "RegistryToolInvoker",
    "create_tool_invoker",
    "TaskManager",
    "SessionResources",
    "TaskContext",
    "get_task_manager",
    "init_task_manager",
    "shutdown_task_manager",
    "AgentLoop",
    "AgentLoopConfig",
    "AgentLoopContext",
    "AgentLoopEvent",
    "AgentLoopPhase",
    "ErrorSeverity",
    "StructuredError",
    "PHASE_DISPLAY_NAMES",
    "PHASE_INDEX",
    "TOTAL_PHASES",
    "create_agent_loop",
    "RetrievalMetrics",
    "RAGMetricsCollector",
    "get_rag_metrics_collector",
    # Context Metrics (Observability)
    "ContextMetrics",
    "ContextMetricsBuilder",
    "ContextMetricsCollector",
    "MetricLayer",
    "LayerMetrics",
    "CompressionMetrics",
    "KVCacheMetrics",  # Renamed from CacheMetrics to avoid conflict
    "MemoryMetrics",
    "get_context_metrics_collector",
    "init_context_metrics_collector",
    # Query Intent Analyzer (Self-RAG style)
    "QueryType",
    "RetrievalDecision",
    "QueryIntent",
    "QueryIntentAnalyzer",
    "create_query_intent_analyzer",
]
